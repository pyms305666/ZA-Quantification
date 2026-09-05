"""练习05：实现 highest 函数（测试先行）

目标：练习"先写测试、再写实现"（docs/11 §2.3）。
任务：实现 `highest(values, n)` —— 返回**最近 n 根**里的最高价；
      数据不足（len(values) < n）或 n <= 0 → 返回 None。

做法：
1. 先读下面的自检代码，弄清楚每个用例期望什么；
2. 把 TODO 处补全；
3. 运行本文件，全 ✅ 通过。

提示：
- 切片：values[-n:] 取最后 n 个
- max() 求最大
- "数据不足返回 None"和 indicators.py 里 sma 的写法一样
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))


def highest(values: list[float], n: int):
    """返回最近 n 个值里的最大值；数据不足返回 None"""
    # TODO: 实现（3 行以内就够）
    return None  # TODO 完成前先返回 None，让自检优雅显示 ❌


# ============ 自检代码，不要改 ============
def 自检():
    检查 = []
    def 断言(说明, 实际, 期望):
        ok = 实际 == 期望
        检查.append(ok)
        print(("✅" if ok else f"❌ 实际={实际} 期望={期望}"), 说明)

    断言("基本用法", highest([1.0, 5.0, 3.0, 2.0], 3), 5.0)          # 最近3个是 5,3,2
    断言("n=1 取最后一个", highest([1.0, 5.0, 3.0], 1), 3.0)
    断言("n 覆盖全部", highest([7.0, 1.0, 4.0], 3), 7.0)
    断言("数据不足 → None", highest([1.0, 2.0], 3), None)
    断言("n=0 → None", highest([1.0, 2.0], 0), None)
    断言("负 n → None", highest([1.0, 2.0], -1), None)
    断言("负数序列", highest([-5.0, -1.0, -3.0], 2), -1.0)

    print()
    print("🎉 全部通过！" if all(检查) else f"还有 {检查.count(False)} 个没过")


if __name__ == "__main__":
    自检()
