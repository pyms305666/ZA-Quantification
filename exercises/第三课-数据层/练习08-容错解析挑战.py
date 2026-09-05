"""练习08：容错解析挑战（脏数据防线）

目标：练习 docs/12 §3.4 的数据清洗哲学——外部数据一律不可信，坏行丢掉，好行放行。

任务：实现 `容错解析(rows)` —— 对一批 DIFF 原始行做清洗：
- 能解析的（用 tqdiff.auth.parse_kline_row）→ 保留
- 解析不了的（None / 字符串 / 长度不够 / NaN 收盘）→ **丢弃且不让程序崩溃**

做法：把 TODO 补全，运行本文件看全 ✅。
注意：本练习引用 tqdiff 包，请先切到 route-c-diff-direct 分支运行：
    git checkout route-c-diff-direct
提示：try/except 包住单行解析——一行坏数据不能拖垮整批。
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tqdiff.auth import parse_kline_row

# 一批真实感十足的"脏"数据（混合了好行和各种坏行）
脏数据 = [
    # 好行（字典格式，datetime 是纳秒）
    {"datetime": 1788531000000000000, "open": 3109, "high": 3111, "low": 3108,
     "close": 3108, "volume": 442, "open_oi": 576030, "close_oi": 576055},
    # 坏行 1：None
    None,
    # 坏行 2：字符串
    "这不是一行K线",
    # 坏行 3：缺字段（长度不够的数组）
    [1, 2, 3],
    # 好行（数组格式）
    [1788531300000000000, 3110.0, 3112.0, 3109.0, 3111.0, 100, 576055, 576100],
    # 坏行 4：收盘是 NaN
    {"datetime": 1788531600000000000, "open": 3111, "high": 3112, "low": 3110,
     "close": float("nan"), "volume": 50, "open_oi": 576100, "close_oi": 576100},
    # 坏行 5：datetime 是乱码
    {"datetime": "昨天", "open": 3111, "high": 3112, "low": 3110,
     "close": 3111.5, "volume": 60, "open_oi": 576100, "close_oi": 576101},
]


def 容错解析(rows: list) -> list[dict]:
    """解析一批原始行：只保留能解析成功的，坏行静默丢弃"""
    output: list[dict] = []
    # TODO:
    # 1. 遍历 rows
    # 2. 对每一行调用 parse_kline_row（包在 try/except 里——一行坏不能拖垮整批）
    # 3. 结果不是 None 的追加到 output
    ...
    return output


# ============ 以下是自检代码，不要改 ============
def 自检():
    检查 = []
    def 断言(说明, 实际, 期望):
        ok = 实际 == 期望
        检查.append(ok)
        print(("✅" if ok else f"❌ 实际={实际} 期望={期望}"), 说明)

    good = 容错解析(脏数据)
    断言("7 行脏数据里救回 2 行好行", len(good), 2)
    断言("第一行收盘价", good[0]["close"] if good else None, 3108.0)
    断言("第二行是数组格式那行", good[1]["close"] if len(good) > 1 else None, 3111.0)

    # 极端情况：全坏也不能崩
    断言("全 None 也不崩、返回空", len(容错解析([None, None, "x"])), 0)
    断言("空列表返回空", len(容错解析([])), 0)

    print()
    print("🎉 全部通过！" if all(检查) else f"还有 {检查.count(False)} 个没过")


if __name__ == "__main__":
    自检()
