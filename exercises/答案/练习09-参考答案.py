"""练习09 参考答案（/summary 端点实现）"""


@app.get("/summary")
def summary(numbers: str = ""):
    parts = [p for p in numbers.split(",") if p.strip() != ""]
    if not parts:
        return {"error": "numbers 不能为空"}
    try:
        values = [float(p) for p in parts]
    except ValueError:
        return {"error": "存在无法解析的数字"}
    return {"max": max(values), "min": min(values),
            "avg": round(sum(values) / len(values), 2)}
