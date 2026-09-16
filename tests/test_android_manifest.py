"""Android Manifest regression checks for the foreground backend service."""

from __future__ import annotations

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
MANIFEST_PATH = Path(__file__).parents[1] / "mobile-app" / "app" / "src" / "main" / "AndroidManifest.xml"


class AndroidManifestTests(unittest.TestCase):
    def test_data_sync_foreground_service_has_type_specific_permission(self):
        root = ET.parse(MANIFEST_PATH).getroot()
        permissions = {
            element.get(f"{ANDROID_NS}name")
            for element in root.findall("uses-permission")
        }
        self.assertIn("android.permission.FOREGROUND_SERVICE_DATA_SYNC", permissions)


if __name__ == "__main__":
    unittest.main()
