"""练习08 参考答案"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # 引用项目代码

from tqdiff.auth import parse_kline_row



def 容错解析(rows: list) -> list[dict]:
    output: list[dict] = []
    for row in rows:
        try:
            bar = parse_kline_row(row)
        except Exception:
            continue
        if bar is not None:
            output.append(bar)
    return output


# 自检
if __name__ == "__main__":
    good = 容错解析(脏数据) if False else None
    from sys import maxsize
    rows = [
        {"datetime": 1788531000000000000, "open": 3109, "high": 3111, "low": 3108,
         "close": 3108, "volume": 442, "open_oi": 576030, "close_oi": 576055},
        None, "junk", [1, 2, 3],
        [1788531300000000000, 3110.0, 3112.0, 3109.0, 3111.0, 100, 576055, 576100],
        {"datetime": 1788531600000000000, "open": 3111, "high": 3112, "low": 3110,
         "close": float("nan"), "volume": 50, "open_oi": 576100, "close_oi": 576100},
    ]
    good = 容错解析(rows)
    assert len(good) == 2 and good[0]["close"] == 3108.0 and good[1]["close"] == 3111.0
    assert 容错解析([]) == []
    print("✅ 练习08 答案验证通过")
