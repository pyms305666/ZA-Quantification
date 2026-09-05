"""练习07 参考答案"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # 引用项目代码



def 合并(rows: dict[int, list], last_id: int, chart_data: dict) -> tuple[dict, int]:
    new_rows = dict(rows)                       # 复制，不改原件
    data = chart_data.get("data")
    if isinstance(data, dict):
        for key, row in data.items():
            if key == "@":
                continue
            try:
                new_rows[int(key)] = row        # 字符串行号 → int
            except (TypeError, ValueError):
                continue                        # 坏键跳过
        anchor = data.get("@")
    else:
        anchor = None
    new_last = chart_data.get("last_id", anchor)
    if new_last is not None:
        try:
            last_id = int(new_last)
        except (TypeError, ValueError):
            pass
    return new_rows, last_id


def 缺失数量(need_from: int, last_id: int, rows: dict) -> int:
    return sum(1 for i in range(need_from, last_id + 1) if i not in rows)


# 自检
if __name__ == "__main__":
    rows, lid = 合并({0: ["K0"], 1: ["K1"]}, 1,
                    {"last_id": 3, "data": {"@": 3, "2": ["K2"], "3": ["K3"]}})
    assert len(rows) == 4 and lid == 3 and rows[3] == ["K3"]
    assert 缺失数量(0, 5, {0: ["A"], 1: ["B"], 4: ["E"]}) == 3
    print("✅ 练习07 答案验证通过")
