"""Gateway configuration.

加载顺序（后者覆盖前者）：
  1. ``config.json``（可从 ``config.json.example`` 复制，路径可用环境变量 TQ_GATEWAY_CONFIG 指定）
  2. 本地凭据存储 ``.tqsdk/credentials.json``（前端登录界面保存，保存一次后自动复用；
     该目录已被 .gitignore 排除，凭据不进仓库。注意：本机文件为明文，请保管好设备与目录权限）
  3. 环境变量 ``TQ_ACCOUNT`` / ``TQ_PASSWORD``（优先级最高）

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
    """本地凭据存储路径（git 已忽略 .tqsdk/）。

    Android（Chaquopy）环境使用 App 私有目录的绝对路径，避免依赖当前工作目录
    可能指向只读 assets 目录；桌面环境保持相对路径（或 TQ_GATEWAY_CREDENTIALS 覆盖）。
    """
    override = os.environ.get("TQ_GATEWAY_CREDENTIALS")
    if override:
        return Path(override).expanduser()
    mobile_dir = _mobile_files_dir()
    if mobile_dir is not None:
        return mobile_dir / ".tqsdk" / "credentials.json"
    return Path(".tqsdk/credentials.json")


def _mobile_files_dir() -> Path | None:
    """Android（Chaquopy）环境下返回 App 私有 files 目录；否则返回 None。

    优先读取 Java 系统属性 za.filesdir（由 MainActivity 写入，指向可写目录），
    读取失败则回退到环境变量 TQ_MOBILE_FILES_DIR，两者都没有说明不是移动端。
    """
    try:
        from java import jclass  # type: ignore[import-not-found]  # 仅 Chaquopy 存在
        value = jclass("java.lang.System").getProperty("za.filesdir")
        if value:
            return Path(value)
    except Exception:
        pass
    env = os.environ.get("TQ_MOBILE_FILES_DIR")
    if env:
        return Path(env)
    return None


def load_saved_credentials() -> tuple[str, str]:
    """读取本地保存的天勤凭据；无文件或解析失败返回空串。"""
    path = _credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("account", "")).strip(), str(data.get("password", ""))
    except (OSError, ValueError):
        return "", ""


def save_credentials(account: str, password: str) -> Path:
    """保存天勤凭据到本地（登录界面"保存并连接"时调用）。"""
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"account": account.strip(), "password": password}, ensure_ascii=False),
        encoding="utf-8")
    return path


def clear_credentials() -> None:
    """清除本地保存的天勤凭据（"退出登录"时调用）。"""
    path = _credentials_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


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
