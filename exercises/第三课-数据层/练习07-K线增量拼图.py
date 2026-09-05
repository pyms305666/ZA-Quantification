"""练习07：K 线增量拼图（把 DIFF 协议的增量合并成完整数据）

目标：理解 docs/12 §3.5 的"增量同步"——服务器只给变化的部分，客户端自己拼完整拼图。

背景：C 直连版的 K 线数据是"行号 → 行内容"的拼图：
- rows 字典存已有的行：{行号: 行内容}
- last_id 是最新行号
- 服务器每次只给"新出现的行"

做法：把 TODO 处补全（两个小函数），运行本文件看全 ✅。
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


def 合并(rows: dict[int, list], last_id: int, chart_data: dict) -> tuple[dict, int]:
    """把一批增量合并进拼图，返回 (新的rows, 新的last_id)

    chart_data 的形状（真实协议）：
        {"last_id": 3, "data": {"@": 3, "2": ["K2"], "3": ["K3"]}}
    其中 data 的键是行号（字符串），"@" 是最新行号的锚点，都不是要存的行。

    注意：不要修改传入的 rows——复制一份再改（为什么？想想"给别人副本"的原则）。
    """
    # TODO:
    # 1. 复制 rows（提示：dict(rows)）
    # 2. 取出 data = chart_data.get("data")；不是字典就直接返回 (复制结果, last_id)
    # 3. 遍历 data：跳过 "@" 键；其余键转成 int 当行号，行内容存进去
    # 4. 行号优先级：chart_data["last_id"] 若存在则更新为新 last_id（转 int，失败保持原值）
    return dict(rows), last_id  # TODO 完成前先原样返回，让自检优雅显示 ❌


def 缺失数量(need_from: int, last_id: int, rows: dict) -> int:
    """统计 [need_from, last_id] 区间里缺了几行（0 = 拼图完整）"""
    # TODO: 一行列表推导即可
    return 0  # TODO 完成前先返回 0


# ============ 以下是自检代码，不要改 ============
def 自检():
    检查 = []
    def 断言(说明, 实际, 期望):
        ok = 实际 == 期望
        检查.append(ok)
        print(("✅" if ok else f"❌ 实际={实际} 期望={期望}"), 说明)

    # 用例1：正常增量
    rows, last_id = 合并({0: ["K0"], 1: ["K1"]}, 1,
                         {"last_id": 3, "data": {"@": 3, "2": ["K2"], "3": ["K3"]}})
    断言("合并后行数=4", len(rows), 4)
    断言("合并后 last_id=3", last_id, 3)
    断言("旧行还在", rows.get(0), ["K0"])
    断言("新行已入库", rows.get(3), ["K3"])

    # 用例2：锚点 @ 与 last_id 同义
    rows2, last_id2 = 合并({}, -1, {"data": {"@": 5, "5": ["K5"]}})
    断言("只有锚点也能更新 last_id", last_id2, 5)
    断言("行也进来了", rows2.get(5), ["K5"])

    # 用例3：gap 检测（last_id=5 但缺 4 行中缺 2 行）
    rows3 = {0: ["A"], 1: ["B"], 4: ["E"]}
    断言("区间内缺 3 行（2/3/5 缺）", 缺失数量(0, 5, rows3), 3)
    断言("拼图完整", 缺失数量(0, 1, {0: ["A"], 1: ["B"]}), 0)

    print()
    print("🎉 全部通过！" if all(检查) else f"还有 {检查.count(False)} 个没过")


if __name__ == "__main__":
    自检()
