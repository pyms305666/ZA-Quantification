"""自动发现共享目录新增/缺失文件；必要分叉必须登记双方规范换行摘要。"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOBILE = ROOT / "mobile-app/app/src/main/python"
REGISTRY = ROOT / "docs/technical-debt/expected-drift.json"
INVENTORY = ROOT / "docs/technical-debt/shared-files.json"
DIRECTORIES = ("market", "tq", "tqdiff", "api", "static")
TOP_LEVEL = {"services.py", "config.py", "app_runtime.py", "ws_transport.py", "VERSION"}


def discover(root, mobile):
    rels = set(TOP_LEVEL)
    for base in (root, mobile):
        for directory in DIRECTORIES:
            for path in (base / directory).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    rels.add(path.relative_to(base).as_posix())
    return rels


def sha256_of(path):
    if not path.is_file():
        return None
    data = path.read_bytes()
    if path.suffix in {".py", ".js", ".css", ".html", ".txt"} or path.name == "VERSION":
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def check(root=ROOT, mobile=MOBILE, registry=None, required=None):
    if registry is None:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if required is None:
        required = json.loads(INVENTORY.read_text(encoding="utf-8"))
    rels = discover(root, mobile) | set(required)
    errors = []
    for rel in sorted(rels | set(registry)):
        a, b = sha256_of(root / rel), sha256_of(mobile / rel)
        entry = registry.get(rel)
        if entry:
            if not entry.get("reason") or rel not in rels or a == b or a != entry.get("desktop_sha256") or b != entry.get("android_sha256"):
                errors.append("分叉登记过期或摘要不符：" + rel)
            else:
                print("FORK " + rel + "：" + entry["reason"])
        elif a != b or a is None:
            errors.append("未登记的漂移/缺失：" + rel)
    for error in errors:
        print(error)
    print("Checked %d discovered files, %d registered forks, %d errors" % (len(rels), len(registry), len(errors)))
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    return 1 if check() and args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
