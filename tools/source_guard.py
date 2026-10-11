"""Capture approved source bytes and check the immutable experiment boundary.

Never writes to the source workspace. Capture is explicit and refuses a changed
source between reads. Runtime data, credentials and research reports are excluded.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs/technical-debt/source-index.json"
MANIFESTS = (".tqsdk/research/forward15m/manifest.json",
             ".tqsdk/research/decision_forward/manifest.json")
SOURCE_EXT = {".py", ".pyw", ".ps1", ".js", ".css", ".html", ".java",
              ".gradle", ".toml", ".xml", ".properties", ".txt"}


def git(base, *args):
    return subprocess.check_output(["git", "-C", str(base)] + list(args))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sources(base):
    tracked = git(base, "ls-files", "-z").decode("utf-8").split("\0")
    extra = git(base, "ls-files", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
    allowed_extra = [r for r in extra if (
        r.startswith("research/") and Path(r).suffix == ".py"
        or r.startswith("tools/") and Path(r).suffix in {".py", ".pyw", ".ps1"}
        or r in {"requirements-research.txt", "requirements-kronos-research.txt",
                 "tests/test_backtest.py", "tests/test_direction_model.py"})]
    monitored = [r for r in tracked if r and (
        Path(r).suffix in SOURCE_EXT or r in {"VERSION", "README.md", "electron/package.json",
                                              "electron/package-lock.json"})]
    return sorted(set(monitored + allowed_extra)), sorted(allowed_extra)


def state(base):
    names, extra = sources(base)
    return {"commit": git(base, "rev-parse", "HEAD").decode().strip(),
            "branch": git(base, "branch", "--show-current").decode().strip(),
            "untracked_sources": extra,
            "sha256": {r: digest((base / r).read_bytes()) for r in names}}


def capture(base):
    if base.resolve() == ROOT.resolve():
        raise ValueError("Source must be a separate workspace")
    if INDEX.exists():
        raise ValueError("Existing snapshot cannot be replaced")
    before = state(base)
    if before["commit"] != "a4ece80504c7763a9a1a391d0f983ab9d1d3e0e2" or before["branch"] != "mobile-app":
        raise ValueError("Unexpected approved source baseline")
    frozen = set()
    manifest_records = []
    for rel in MANIFESTS:
        manifest = json.loads((base / rel).read_text(encoding="utf-8-sig"))
        entries = manifest["source_sha256"]
        for name, expected in entries.items():
            if digest((base / name).read_bytes()) != expected:
                raise ValueError("Experiment manifest mismatch: " + name)
            frozen.add(name)
        manifest_records.append({"source_path": str(base / rel), "source_sha256": entries})
    experiment_count = len(frozen)
    # Protect all model directories, including unused Android model copies.
    for r in before["sha256"]:
        if r.startswith("market/") or r.startswith("mobile-app/app/src/main/python/market/"):
            frozen.add(r)
        if r in {"config.py", "mobile-app/app/src/main/python/config.py"}:
            frozen.add(r)
        if r.startswith("mobile-app/app/src/main/python/") and Path(r).name in {
                "evaluator.py", "indicators.py", "model.py", "decision_profiles.py"}:
            frozen.add(r)
    selected = sorted(frozen | set(before["untracked_sources"]))
    raw = {r: (base / r).read_bytes() for r in selected}
    if state(base) != before:
        raise ValueError("Source changed during capture; no files copied")
    directory = ROOT / ".local/source-snapshot"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / "v1.2.1-original.zip"
    with zipfile.ZipFile(str(archive), "w", zipfile.ZIP_DEFLATED) as z:
        for r, data in raw.items():
            z.writestr(r, data)
    if state(base) != before:
        archive.unlink()
        raise ValueError("Source changed during snapshot; no files copied")
    index = {"source_root": str(base.resolve()), "base_commit": before["commit"],
             "experiment_frozen_count": experiment_count, "experiment_manifests": manifest_records,
             "archive": ".local/source-snapshot/v1.2.1-original.zip",
             "archive_sha256": digest(archive.read_bytes()), "main_source_state": before,
             "files": [{"path": r, "source_path": str(base / r), "sha256": digest(raw[r]),
                        "frozen": r in frozen, "size": len(raw[r])} for r in selected]}
    # All source consistency checks precede the first copy into the isolation tree.
    for r, data in raw.items():
        target = ROOT / r
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # New research sources preserve raw bytes when registered with Git.
    attrs = ROOT / ".gitattributes"
    attrs.write_text("# Preserve imported, untracked research source bytes.\n" +
                     "".join(r + " -text\n" for r in before["untracked_sources"]), encoding="utf-8")
    print("Captured %d files; %d experiment files; ZIP SHA-256 %s" %
          (len(raw), experiment_count, index["archive_sha256"]))


def check_git(index):
    errors = []
    baseline = index.get("git_baseline_commit")
    if not baseline:
        return ["Git freeze metadata missing"]
    for entry in index["files"]:
        if not entry["frozen"]:
            continue
        rel = entry["path"]
        try:
            expected = entry["git_blob"]
            refs = (baseline + ":" + rel, "HEAD:" + rel, ":" + rel)
            if any(git(ROOT, "rev-parse", ref).decode().strip() != expected for ref in refs):
                errors.append("Frozen Git object changed: " + rel)
            blob = git(ROOT, "show", baseline + ":" + rel)
            if digest(blob) != entry["git_sha256"]:
                errors.append("Frozen Git digest changed: " + rel)
            work = git(ROOT, "hash-object", "--path=" + rel, str(ROOT / rel)).decode().strip()
            if work != expected:
                errors.append("Frozen working source changed: " + rel)
        except (KeyError, OSError, subprocess.CalledProcessError):
            errors.append("Frozen Git source unavailable: " + rel)
    return errors


def check(require_main=False, git_only=False):
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    if git_only:
        errors = check_git(index)
        for error in errors:
            print(error)
        if not errors:
            print("PASS: frozen Git baseline/index/HEAD/working sources unchanged")
        return 1 if errors else 0
    errors = []
    for entry in index["files"]:
        if entry["frozen"]:
            p = ROOT / entry["path"]
            if not p.is_file() or digest(p.read_bytes()) != entry["sha256"]:
                errors.append("Frozen source changed: " + entry["path"])
    archive = ROOT / index["archive"]
    if archive.exists() and digest(archive.read_bytes()) != index["archive_sha256"]:
        errors.append("Original ZIP changed")
    base = Path(index["source_root"])
    if base.exists():
        if state(base) != index["main_source_state"]:
            errors.append("Main workspace source state changed; re-audit required")
        for m in index["experiment_manifests"]:
            current = json.loads(Path(m["source_path"]).read_text(encoding="utf-8-sig"))
            if current["source_sha256"] != m["source_sha256"]:
                errors.append("Experiment source manifest changed: " + m["source_path"])
    elif require_main:
        errors.append("Main workspace unavailable")
    for err in errors:
        print(err)
    if not errors:
        print("PASS: frozen bytes and available main source state unchanged")
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--require-main", action="store_true")
    parser.add_argument("--git-only", action="store_true", help="Portable clone check; local raw/main guard remains separate")
    args = parser.parse_args()
    if args.git_only and (args.require_main or args.capture):
        parser.error("--git-only cannot bypass --require-main or capture")
    if args.capture:
        capture(args.capture)
    return check(args.require_main, args.git_only)


if __name__ == "__main__":
    raise SystemExit(main())
