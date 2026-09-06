"""天勤 DIFF 协议直连数据层（C 路线）。

不依赖 TqSdk：直接实现天勤 DIFF 协议的行情子集
（认证 → 名称服务 → 行情 WebSocket → subscribe_quote / set_chart），
合约目录来自 openmd 静态合约服务文件（一次 HTTP 拉取 + 磁盘缓存）。
"""
