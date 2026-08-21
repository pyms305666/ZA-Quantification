"""Gateway configuration.

加载顺序（后者覆盖前者）：
  1. ``config.json``（可从 ``config.json.example`` 复制，路径可用环境变量 TQ_GATEWAY_CONFIG 指定）
  2. 环境变量 ``TQ_ACCOUNT`` / ``TQ_PASSWORD`` 覆盖天勤账号

天勤账号（手机号 + 密码）在 https://www.tqsdk.com 注册，免费。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TqSdkConfig:
    account: str = ""
    password: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.account and self.password)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"


@dataclass
class RiskConfig:
    """决策系统的资金与风险参数。

    手数 = min(最大手数, 单笔最大亏损 ÷ (止损距离 × 合约乘数))，至少 1 手。
    """

    account_equity: float = 50_000.0  # 账户权益（元）
    max_loss_per_trade: float = 900.0  # 单笔最大亏损（元）——以它为准计算手数
    risk_percent: float = 1.8  # 展示用：单笔亏损占权益比例（%）
    max_contracts: int = 10  # 单笔最大手数


@dataclass
class Config:
    tqsdk: TqSdkConfig = field(default_factory=TqSdkConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)


def _default_config_path() -> Path:
    return Path(os.environ.get("TQ_GATEWAY_CONFIG", "config.json")).expanduser()


def load_config(path: Path | None = None) -> Config:
    config = Config()
    config_path = path or _default_config_path()
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"配置文件 {config_path} 无法解析：{error}") from error
        tqsdk = raw.get("tqsdk", {})
        config.tqsdk.account = str(tqsdk.get("account", "")).strip()
        config.tqsdk.password = str(tqsdk.get("password", ""))
        server = raw.get("server", {})
        config.server.host = str(server.get("host", config.server.host)).strip()
        config.server.port = int(server.get("port", config.server.port))
        config.server.log_level = str(server.get("log_level", config.server.log_level))
        risk = raw.get("risk", {})
        config.risk.account_equity = float(risk.get("account_equity", config.risk.account_equity))
        config.risk.max_loss_per_trade = float(risk.get("max_loss_per_trade", config.risk.max_loss_per_trade))
        config.risk.risk_percent = float(risk.get("risk_percent", config.risk.risk_percent))
        config.risk.max_contracts = int(risk.get("max_contracts", config.risk.max_contracts))
    # 环境变量优先，避免把密码写进配置文件。
    config.tqsdk.account = os.environ.get("TQ_ACCOUNT", config.tqsdk.account).strip()
    config.tqsdk.password = os.environ.get("TQ_PASSWORD", config.tqsdk.password)
    return config
