"""Gateway configuration.

加载顺序（后者覆盖前者）：
  1. ``config.json``（可从 ``config.json.example`` 复制，路径可用环境变量 TQ_GATEWAY_CONFIG 指定）
  2. 本地凭据存储：优先 **Windows 凭据管理器**（keyring，服务名 ``ZAQuant``），
     本地文件 ``.tqsdk/credentials.json`` 只存账号（供登录页回显与定位凭据管理器条目）；
     旧版明文密码文件在首次读取时自动迁移进凭据管理器并清除明文。
     设置环境变量 ``TQ_GATEWAY_KEYRING=off`` 可强制退回明文文件存储（旧行为）。
  3. 环境变量 ``TQ_ACCOUNT`` / ``TQ_PASSWORD``（优先级最高，避免密码落任何文件）

天勤账号（手机号 + 密码）在 https://www.tqsdk.com 注册，免费。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import keyring   # 桌面端可选依赖（requirements.txt）；缺失时退回明文文件存储
except ImportError:  # pragma: no cover - 手机端/极简环境无 keyring
    keyring = None

KEYRING_SERVICE = "ZAQuant"


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

    金额和权益比例取更严格值；手数向下取整，一手超出预算时为 0。
    """

    account_equity: float = 50_000.0  # 账户权益（元）
    max_loss_per_trade: float = 900.0  # 单笔最大亏损（元）——以它为准计算手数
    risk_percent: float = 1.8  # 单笔亏损占权益的上限（%）
    max_contracts: int = 10  # 单笔最大手数


@dataclass
class Config:
    tqsdk: TqSdkConfig = field(default_factory=TqSdkConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)


def _default_config_path() -> Path:
    return Path(os.environ.get("TQ_GATEWAY_CONFIG", "config.json")).expanduser()


def _credentials_path() -> Path:
    """本地凭据文件路径（git 已忽略 .tqsdk/）。文件只存账号；密码在系统凭据管理器。"""
    return Path(os.environ.get("TQ_GATEWAY_CREDENTIALS", ".tqsdk/credentials.json"))


def _keyring_enabled() -> bool:
    """keyring 可用且未被 TQ_GATEWAY_KEYRING=off 显式关闭。"""
    if str(os.environ.get("TQ_GATEWAY_KEYRING", "")).strip().lower() == "off":
        return False
    return keyring is not None


def load_saved_credentials() -> tuple[str, str]:
    """读取本地保存的天勤凭据。

    顺序：凭据管理器（按文件中的账号定位）→ 旧版明文文件（读后自动迁移并清除明文）。
    无凭据或解析失败返回空串。
    """
    path = _credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    account = str(data.get("account", "")).strip()
    stored_password = str(data.get("password", ""))
    if _keyring_enabled() and account:
        try:
            vault_password = keyring.get_password(KEYRING_SERVICE, account)
        except Exception:
            vault_password = None   # 凭据管理器不可用（如无 UI 会话），退回文件
        if vault_password:
            return account, vault_password
    if stored_password:
        if _keyring_enabled() and account:
            # 旧版明文一次性迁移：写入凭据管理器，文件只留账号
            try:
                keyring.set_password(KEYRING_SERVICE, account, stored_password)
                path.write_text(
                    json.dumps({"account": account}, ensure_ascii=False),
                    encoding="utf-8")
            except Exception:
                pass   # 迁移失败保持旧明文不动，下次再试
        return account, stored_password
    return account, ""


def save_credentials(account: str, password: str) -> Path:
    """保存天勤凭据（登录界面"保存并连接"时调用）。

    keyring 可用：密码进系统凭据管理器，文件只留账号（不再落明文密码）。
    keyring 不可用（未安装 / TQ_GATEWAY_KEYRING=off / 写入异常）：退回旧版明文文件存储。
    """
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if _keyring_enabled() and account:
        try:
            keyring.set_password(KEYRING_SERVICE, account.strip(), password)
            path.write_text(
                json.dumps({"account": account.strip()}, ensure_ascii=False),
                encoding="utf-8")
            return path
        except Exception:
            pass   # 凭据管理器写入失败（无 UI 会话等）→ 退回明文文件
    path.write_text(
        json.dumps({"account": account.strip(), "password": password}, ensure_ascii=False),
        encoding="utf-8")
    return path


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
    # 本地凭据存储（登录界面保存）覆盖 config.json，实现"保存一次、后续自动登录"。
    saved_account, saved_password = load_saved_credentials()
    if saved_account and saved_password:
        config.tqsdk.account, config.tqsdk.password = saved_account, saved_password
    # 环境变量优先级最高，避免把密码写进任何文件。
    config.tqsdk.account = os.environ.get("TQ_ACCOUNT", config.tqsdk.account).strip()
    config.tqsdk.password = os.environ.get("TQ_PASSWORD", config.tqsdk.password)
    return config
