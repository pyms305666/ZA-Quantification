"""Android Manifest regression checks for the foreground backend service."""

from __future__ import annotations

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
MANIFEST_PATH = Path(__file__).parents[1] / "mobile-app" / "app" / "src" / "main" / "AndroidManifest.xml"
JAVA_ROOT = MANIFEST_PATH.parent / "java" / "com" / "zaquant" / "mobile"
BACKEND_SERVICE_PATH = JAVA_ROOT / "BackendService.java"
MAIN_ACTIVITY_PATH = JAVA_ROOT / "MainActivity.java"


class AndroidManifestTests(unittest.TestCase):
    def test_data_sync_foreground_service_has_type_specific_permission(self):
        root = ET.parse(MANIFEST_PATH).getroot()
        permissions = {
            element.get(f"{ANDROID_NS}name")
            for element in root.findall("uses-permission")
        }
        self.assertIn("android.permission.FOREGROUND_SERVICE_DATA_SYNC", permissions)

    def test_lockscreen_keepalive_declares_wakelock_and_lifecycle_guards(self):
        root = ET.parse(MANIFEST_PATH).getroot()
        permissions = {
            element.get(f"{ANDROID_NS}name")
            for element in root.findall("uses-permission")
        }
        self.assertIn("android.permission.WAKE_LOCK", permissions)
        self.assertIn("android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS", permissions)

        service = BACKEND_SERVICE_PATH.read_text(encoding="utf-8")
        activity = MAIN_ACTIVITY_PATH.read_text(encoding="utf-8")
        self.assertIn("PowerManager.PARTIAL_WAKE_LOCK", service)
        self.assertIn("void onDestroy()", service)
        self.assertIn("wakeLock.release()", service)
        self.assertIn("startForegroundService", activity)
        self.assertIn("ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS", activity)


if __name__ == "__main__":
    unittest.main()
