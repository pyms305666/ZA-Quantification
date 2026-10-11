import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import app_runtime as runtime
import launcher


class RuntimeTests(unittest.TestCase):
    def test_url_wildcard_ipv6_and_config_path(self):
        self.assertEqual(runtime.public_url('0.0.0.0', 8123), 'http://127.0.0.1:8123')
        self.assertEqual(runtime.public_url('::', 8123), 'http://[::1]:8123')
        self.assertEqual(runtime.public_url('[2001:db8::1]', 8123), 'http://[2001:db8::1]:8123')
        with mock.patch.dict('os.environ', {'TQ_GATEWAY_CONFIG': 'nested/custom.json'}):
            self.assertEqual(runtime.config_path(), Path('nested/custom.json'))

    def test_version_uses_resources_and_missing_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(runtime.sys, '_MEIPASS', directory, create=True):
            self.assertEqual(runtime.version(), 'unknown')
            (Path(directory) / 'VERSION').write_text('1.2.1\n', encoding='utf-8')
            self.assertEqual(runtime.version(), '1.2.1')

    def test_launcher_probe_uses_actual_address_and_one_second(self):
        with mock.patch.object(launcher.requests, 'get') as get:
            get.return_value.json.return_value = {'route': 'test'}
            self.assertEqual(launcher._route_on_port(8123, '::'), 'test')
            get.assert_called_once_with('http://[::1]:8123/api/v1/status', timeout=1)

    def test_ensure_config_respects_override(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'nested/custom.json'
            example = Path(directory) / 'example.json'
            example.write_text('{"server":{"port":8123}}', encoding='utf-8')
            with mock.patch.dict('os.environ', {'TQ_GATEWAY_CONFIG':str(target)}), \
                    mock.patch.object(launcher, 'resource_path', return_value=example), mock.patch('builtins.print'):
                launcher.ensure_config()
            self.assertEqual(target.read_bytes(), example.read_bytes())

    def test_redaction_and_quote_omission(self):
        text = runtime.redact('账户 123456789 密码 p a ss token=secret Bearer abc', ['123456789', 'p a ss'])
        for secret in ('123456789', 'p a ss', 'secret', 'abc'):
            self.assertNotIn(secret, text)
        self.assertEqual(runtime.redact('{"bid": [], "last": 123}'), '[行情内容已省略]')

    def test_utf8_rotation_is_bounded_and_repeat_initialization_does_not_duplicate(self):
        logger = logging.getLogger()
        previous, level = list(logger.handlers), logger.level
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict('os.environ', {'TQ_MOBILE_FILES_DIR':directory}), \
                mock.patch.object(runtime, 'MAX_LOG_BYTES', 120), mock.patch('sys.stderr'):
            config = SimpleNamespace(tqsdk=SimpleNamespace(account='fake-account',password='fake-password'))
            try:
                path = runtime.configure_logging(config)
                runtime.configure_logging(config)
                self.assertEqual(len([h for h in logger.handlers if getattr(h, '_zaquant', False)]), 2)
                for n in range(10):
                    logger.info('中文诊断 %d fake-password', n)
                files = list(path.parent.glob('gateway.log*'))
                self.assertEqual(len(files), 4)
                for file in files:
                    self.assertNotIn('fake-password', file.read_text(encoding='utf-8'))
                    self.assertLessEqual(file.stat().st_size, 120)
            finally:
                for handler in list(logger.handlers):
                    if handler not in previous:
                        logger.removeHandler(handler)
                        handler.close()
                logger.setLevel(level)


if __name__ == '__main__':
    unittest.main()
