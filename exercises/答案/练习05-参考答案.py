"""练习05 参考答案"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # 引用项目代码



def highest(values: list[float], n: int):
    if values is None or n <= 0 or len(values) < n:
        return None
    return max(values[-n:])


# 自检
if __name__ == "__main__":
    assert highest([1.0, 5.0, 3.0, 2.0], 3) == 5.0
    assert highest([1.0, 2.0], 3) is None
    assert highest([1.0, 2.0], 0) is None
    assert highest([-5.0, -1.0], 2) == -1.0
    print("✅ 练习05 答案验证通过")
