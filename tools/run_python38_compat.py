"""在真实 Python 3.8 中回归 Android 共享代码；不启动 APK 或实盘服务。"""
import sys
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / 'mobile-app/app/src/main/python'


def main():
    if sys.version_info[:2] != (3, 8):
        print('Python 3.8 runtime required; syntax parsing is not this acceptance')
        return 2
    sys.dont_write_bytecode = True
    for directory in (ROOT, PHONE):
        text = str(directory)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
    from tools.run_offline_tests import isolated_runtime, network_guard
    names = ['tests.test_tq', 'tests.test_tqdiff', 'tests.test_model_baseline',
             'tests.test_lifecycle.RecoveryTests', 'tests.test_lifecycle.DiffLifecycleTests',
             'tests.test_android_lifecycle', 'tests.test_ws_transport']
    with isolated_runtime(), network_guard():
        import config
        with mock.patch.object(config, 'keyring', None, create=True):
            suite = unittest.TestLoader().loadTestsFromNames(names)
            result = unittest.TextTestRunner(verbosity=1).run(suite)
    print('Runtime: ' + sys.version.split()[0] + '; Android source: ' + str(PHONE))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
