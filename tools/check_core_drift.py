"""桌面端与手机端核心代码漂移检查（对应 docs/项目检查报告-2026-09-11.md P2"两端分叉"）。

背景：手机端 Python 源码是根目录的一份拷贝（mobile-app/app/src/main/python/），
修 bug 容易只改一边。本工具在每次发布前运行，列出两端核心文件的一致性；
"预期内"的分叉必须在 EXPECTED_DRIFT 里留档原因，未留档的分叉即"意外漂移"。

用法：
  python tools/check_core_drift.py             # 报告模式：打印漂移表，恒定 exit 0
  python tools/check_core_drift.py --strict    # 严格模式：存在预期外漂移时 exit 1（可接 CI）
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOBILE = ROOT / "mobile-app" / "app" / "src" / "main" / "python"

# 两端应保持一致的共享核心（相对路径；缺失一侧也会被报告）
SHARED = [
    "services.py",
    "market/__init__.py",
    "market/cache.py",
    "market/evaluator.py",
    "market/decision_profiles.py",
    "static/decision-controls.js",
    "static/decision-controls.css",
    "market/indicators.py",
    "market/model.py",
    "market/processor.py",
    "tq/__init__.py",
    "tq/client.py",
    "tq/instruments.py",
    "tq/subscriber.py",
    "tqdiff/client.py",
]

# 预期内的分叉（必须留档原因）；这些文件出现 DIFF 不算"意外漂移"
EXPECTED_DRIFT = {
    "config.py": "手机端多出 App 私有目录解析(za.filesdir/TQ_MOBILE_FILES_DIR)与 clear_credentials；桌面端为 config.json+.tqsdk 相对路径",
}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="存在预期外漂移时以退出码 1 结束（可接 CI）")
    args = parser.parse_args()

    # 检查清单 = 显式共享清单 + 两侧 tqdiff/*.py 的并集（捕捉"一侧新增模块"）
    rels = list(SHARED)
    for base in (ROOT, MOBILE):
        for p in sorted((base / "tqdiff").glob("*.py")):
            rel = f"tqdiff/{p.name}"
            if rel not in rels:
                rels.append(rel)

    unexpected = 0
    width = max(len(r) for r in rels)
    print(f"{'文件':<{width}}  状态        说明")
    print("-" * (width + 46))
    for rel in rels:
        a, b = ROOT / rel, MOBILE / rel
        if not a.exists() and not b.exists():
            continue
        if a.exists() and not b.exists():
            reason = EXPECTED_DRIFT.get(rel, "未留档")
            tag = "仅桌面端" if rel in EXPECTED_DRIFT else "仅桌面端!"
            if rel not in EXPECTED_DRIFT:
                unexpected += 1
            print(f"{rel:<{width}}  {tag:<10}  {reason}")
            continue
        if b.exists() and not a.exists():
            reason = EXPECTED_DRIFT.get(rel, "未留档")
            tag = "仅手机端!" if rel not in EXPECTED_DRIFT else "仅手机端"
            if rel not in EXPECTED_DRIFT:
                unexpected += 1
            print(f"{rel:<{width}}  {tag:<10}  {reason}")
            continue
        same = sha256_of(a) == sha256_of(b)
        if same:
            print(f"{rel:<{width}}  一致")
        else:
            reason = EXPECTED_DRIFT.get(rel)
            if reason is None:
                unexpected += 1
                reason = "未留档（需要同步或留档）"
            print(f"{rel:<{width}}  分叉        {reason}")

    print("-" * (width + 46))
    if unexpected:
        print(f"预期外漂移：{unexpected} 项 —— 请同步两端代码，或在 EXPECTED_DRIFT 留档原因")
    else:
        print("无预期外漂移")
    return 1 if (args.strict and unexpected) else 0


if __name__ == "__main__":
    sys.exit(main())
