"""配置与本地凭据存储测试（不依赖网络）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import config as config_module
from config import load_config, save_credentials


class CredentialsStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("TQ_GATEWAY_CREDENTIALS")
        os.environ["TQ_GATEWAY_CREDENTIALS"] = str(Path(self._tmp.name) / "credentials.json")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TQ_GATEWAY_CREDENTIALS", None)
        else:
            os.environ["TQ_GATEWAY_CREDENTIALS"] = self._old
        self._tmp.cleanup()

    def test_save_and_load_roundtrip(self):
        save_credentials("13800138000", "secret-pass")
        path = config_module._credentials_path()
        self.assertTrue(path.exists())
        # 磁盘内容不应包含在仓库可跟踪范围内（路径位于 .tqsdk/ 或显式临时目录）
        account, password = config_module.load_saved_credentials()
        self.assertEqual(account, "13800138000")
        self.assertEqual(password, "secret-pass")

    def test_load_missing_returns_empty(self):
        self.assertEqual(config_module.load_saved_credentials(), ("", ""))

    def test_saved_credentials_override_config(self):
        # 写一个不含凭据的 config.json，凭据来自本地存储 → load_config 优先采用存储
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(json.dumps({"tqsdk": {"account": "", "password": ""}}), encoding="utf-8")
        save_credentials("13900139000", "pw2")
        cfg = load_config(cfg_path)
        self.assertEqual(cfg.tqsdk.account, "13900139000")
        self.assertEqual(cfg.tqsdk.password, "pw2")

    def test_env_overrides_saved_credentials(self):
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(json.dumps({"tqsdk": {"account": "", "password": ""}}), encoding="utf-8")
        save_credentials("13900139000", "pw2")
        old_acc, old_pw = os.environ.get("TQ_ACCOUNT"), os.environ.get("TQ_PASSWORD")
        os.environ["TQ_ACCOUNT"], os.environ["TQ_PASSWORD"] = "env-acc", "env-pw"
        try:
            cfg = load_config(cfg_path)
            self.assertEqual(cfg.tqsdk.account, "env-acc")
            self.assertEqual(cfg.tqsdk.password, "env-pw")
        finally:
            if old_acc is None:
                os.environ.pop("TQ_ACCOUNT", None)
            else:
                os.environ["TQ_ACCOUNT"] = old_acc
            if old_pw is None:
                os.environ.pop("TQ_PASSWORD", None)
            else:
                os.environ["TQ_PASSWORD"] = old_pw


if __name__ == "__main__":
    unittest.main()
