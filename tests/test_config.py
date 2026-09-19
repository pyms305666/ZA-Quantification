"""配置与本地凭据存储测试（不依赖网络、不触碰真实系统凭据管理器）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config as config_module
from config import load_config, save_credentials


class _FakeVault:
    """keyring 的内存替身，避免测试写入真实 Windows 凭据管理器。"""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))


class _BrokenVault(_FakeVault):
    def set_password(self, service, username, password):
        raise OSError("凭据管理器不可用")


class CredentialsStoreTests(unittest.TestCase):
    """默认路径：密码进凭据管理器（fake），文件只留账号。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("TQ_GATEWAY_CREDENTIALS")
        os.environ["TQ_GATEWAY_CREDENTIALS"] = str(Path(self._tmp.name) / "credentials.json")
        self.vault = _FakeVault()
        patcher = mock.patch.object(config_module, "keyring", self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        account, password = config_module.load_saved_credentials()
        self.assertEqual(account, "13800138000")
        self.assertEqual(password, "secret-pass")

    def test_save_keeps_password_out_of_file(self):
        save_credentials("13800138000", "secret-pass")
        stored = json.loads(config_module._credentials_path().read_text(encoding="utf-8"))
        self.assertEqual(stored, {"account": "13800138000"})
        # 密码只存在于凭据管理器
        self.assertEqual(self.vault.store[(config_module.KEYRING_SERVICE, "13800138000")],
                         "secret-pass")

    def test_load_missing_returns_empty(self):
        self.assertEqual(config_module.load_saved_credentials(), ("", ""))

    def test_legacy_plaintext_migrates_to_vault(self):
        path = config_module._credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"account": "13700137000", "password": "old-pw"}),
                        encoding="utf-8")
        account, password = config_module.load_saved_credentials()
        self.assertEqual((account, password), ("13700137000", "old-pw"))
        # 迁移后：明文密码从文件清除，凭据进入管理器
        after = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(after, {"account": "13700137000"})
        self.assertEqual(self.vault.store[(config_module.KEYRING_SERVICE, "13700137000")],
                         "old-pw")

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


class KeyringFallbackTests(unittest.TestCase):
    """降级路径：keyring 关闭/异常时退回明文文件（旧行为），不抛错。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("TQ_GATEWAY_CREDENTIALS")
        os.environ["TQ_GATEWAY_CREDENTIALS"] = str(Path(self._tmp.name) / "credentials.json")
        self._old_off = os.environ.pop("TQ_GATEWAY_KEYRING", None)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TQ_GATEWAY_CREDENTIALS", None)
        else:
            os.environ["TQ_GATEWAY_CREDENTIALS"] = self._old
        if self._old_off is not None:
            os.environ["TQ_GATEWAY_KEYRING"] = self._old_off
        else:
            os.environ.pop("TQ_GATEWAY_KEYRING", None)
        self._tmp.cleanup()

    def test_env_off_falls_back_to_plaintext(self):
        os.environ["TQ_GATEWAY_KEYRING"] = "off"
        save_credentials("13600136000", "file-pw")
        stored = json.loads(config_module._credentials_path().read_text(encoding="utf-8"))
        self.assertEqual(stored, {"account": "13600136000", "password": "file-pw"})
        self.assertEqual(config_module.load_saved_credentials(), ("13600136000", "file-pw"))

    def test_vault_write_error_falls_back_to_plaintext(self):
        with mock.patch.object(config_module, "keyring", _BrokenVault()):
            save_credentials("13500135000", "file-pw2")
        stored = json.loads(config_module._credentials_path().read_text(encoding="utf-8"))
        self.assertEqual(stored, {"account": "13500135000", "password": "file-pw2"})
        self.assertEqual(config_module.load_saved_credentials(), ("13500135000", "file-pw2"))

    def test_keyring_module_missing_disables_vault(self):
        with mock.patch.object(config_module, "keyring", None):
            self.assertFalse(config_module._keyring_enabled())
            save_credentials("13400134000", "file-pw3")
        stored = json.loads(config_module._credentials_path().read_text(encoding="utf-8"))
        self.assertEqual(stored, {"account": "13400134000", "password": "file-pw3"})


if __name__ == "__main__":
    unittest.main()
