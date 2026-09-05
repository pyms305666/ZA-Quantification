"""练习09：给自己的第一个接口写实现（接口层入门）

目标：练习 FastAPI 的路径/查询参数、返回 JSON、错误处理（docs/13 §4.1）。

任务：实现 /summary 接口——
    访问  http://127.0.0.1:8030/summary?numbers=1,2,3
    返回  {"max": 3.0, "min": 1.0, "avg": 2.0}
    没传 numbers 或全是空的 → {"error": "numbers 不能为空"}
    有非数字（如 a,b）   → {"error": "存在无法解析的数字"}

做法：补全 TODO 后运行本文件（它会自动启动服务并自检一次）：
    python "exercises/第四课-接口与前端/练习09-新接口summary.py"
看到 ✅ 后，继续在浏览器里换不同的 numbers 试试。
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
import uvicorn

app = FastAPI(title="练习09")


@app.get("/summary")
def summary(numbers: str = ""):
    """numbers 形如 "1,2,3"（逗号分隔）。按说明实现三种返回。"""
    # TODO:
    # 1. 按 "," 切分成列表；空列表或全是空串 → 返回 {"error": "numbers 不能为空"}
    # 2. 逐个 float() 转数字，转不了的 → 返回 {"error": "存在无法解析的数字"}
    #    （提示：try/except 包住整个转换，遇到一个坏的直接返回 error）
    # 3. 全部成功 → 返回 {"max": 最大, "min": 最小, "avg": 平均（保留两位小数，float）}
    return {"error": "还没实现"}  # TODO 完成后此行会被上面的 return 覆盖


# ============ 以下是自检与服务启动代码，不要改 ============
def 自检():
    import json, time, urllib.request
    def 访问(qs):
        url = f"http://127.0.0.1:8030/summary?numbers={qs}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())
    检查 = []
    def 断言(说明, 实际, 期望):
        ok = 实际 == 期望
        检查.append(ok)
        print(("✅" if ok else f"❌ 实际={实际} 期望={期望}"), 说明)

    断言("正常三数", 访问("1,2,3"), {"max": 3.0, "min": 1.0, "avg": 2.0})
    断言("单个数", 访问("5"), {"max": 5.0, "min": 5.0, "avg": 5.0})
    断言("空参数", 访问(""), {"error": "numbers 不能为空"})
    断言("非法数字", 访问("1,a,3"), {"error": "存在无法解析的数字"})


if __name__ == "__main__":
    import threading
    threading.Thread(target=自检, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8030, log_level="warning")
