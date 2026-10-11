"""启动资源与诊断；独立于冻结的配置/评估模块，兼容 Python 3.8。"""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys

MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3


def resource_path(name):
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / name


def version():
    try:
        return resource_path('VERSION').read_text(encoding='utf-8').strip() or 'unknown'
    except OSError:
        return 'unknown'


def config_path():
    return Path(os.environ.get('TQ_GATEWAY_CONFIG', 'config.json')).expanduser()


def public_url(host, port):
    host = str(host).strip().strip('[]')
    host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
    if ':' in host:
        host = '[' + host + ']'
    return 'http://%s:%s' % (host, port)


def log_directory():
    mobile = os.environ.get('TQ_MOBILE_FILES_DIR')
    if mobile:
        return Path(mobile) / 'logs'
    if os.environ.get('LOCALAPPDATA'):
        return Path(os.environ['LOCALAPPDATA']) / 'ZAQuant' / 'logs'
    return Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'ZAQuant/logs'


def redact(text, secrets=()):
    text = str(text)
    values = set()
    for secret in secrets:
        if secret:
            values.update((str(secret), json.dumps(str(secret), ensure_ascii=True)[1:-1]))
    for secret in sorted(values, key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'''(?i)((?:password|access_token|refresh_token|token|account)["']?\s*[:=]\s*["']?)[^\s,"'}]+''',
                  r'\1[REDACTED]', text)
    if re.search(r'''["'](?:quotes|bid|ask)["']\s*:''', text):
        return '[行情内容已省略]'
    return text[:8192]


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets):
        super().__init__('%(asctime)s %(levelname)s %(name)s %(message)s')
        self.secrets = secrets

    def format(self, record):
        return redact(super().format(record), self.secrets)


class UTF8RotatingHandler(RotatingFileHandler):
    def shouldRollover(self, record):
        # 标准处理器按字符数估算，中文 UTF-8 必须按实际字节限制。
        if self.stream is None:
            self.stream = self._open()
        text = (self.format(record) + self.terminator).replace('\n', os.linesep)
        self.stream.seek(0, 2)
        return self.maxBytes > 0 and self.stream.tell() + len(text.encode('utf-8')) > self.maxBytes


def configure_logging(config=None):
    """仅启动入口调用；文件与控制台统一过滤，不读凭据管理器。"""
    directory = log_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'gateway.log'
    secrets = [os.environ.get('TQ_ACCOUNT'), os.environ.get('TQ_PASSWORD')]
    if config is not None:
        secrets.extend((config.tqsdk.account, config.tqsdk.password))
    formatter = SafeFormatter(secrets)
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        if getattr(handler, '_zaquant', False):
            logger.removeHandler(handler)
            handler.close()
    file_handler = UTF8RotatingHandler(str(path), maxBytes=MAX_LOG_BYTES,
                                      backupCount=LOG_BACKUPS, encoding='utf-8')
    console_handler = logging.StreamHandler()
    for handler in (file_handler, console_handler):
        handler._zaquant = True
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return path
