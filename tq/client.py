"""C 路线：数据层直连天勤 DIFF 协议（不再依赖 TqSdk）。

本文件只保留兼容适配层：真实的连接 / 订阅 / 合约目录 / K 线实现全部在
:tmod:`tqdiff.client`（asyncio + WebSocket + 静态合约文件）。
对外接口（TqClient / TqClientError）与旧 TqSdk 版本完全一致，
因此 tq/instruments、tq/subscriber、api/、market/ 各层零改动。
"""

from __future__ import annotations

from tqdiff.client import DiffClient as TqClient
from tqdiff.client import TqClientError

__all__ = ["TqClient", "TqClientError"]
