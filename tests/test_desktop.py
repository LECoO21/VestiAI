import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitmix.desktop as desktop


class DesktopTests(unittest.TestCase):
    def test_prepare_environment_uses_application_support_directory(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"FITMIX_SUPPORT_DIR": temporary}, clear=False
        ):
            support_dir = desktop.prepare_environment()
            self.assertEqual(support_dir, Path(temporary))
            self.assertEqual(os.environ["WARDROBE_DATA_DIR"], str(support_dir / "data"))
            self.assertEqual(os.environ["FITMIX_CONFIG_PATH"], str(support_dir / ".env"))
            self.assertEqual(os.environ["FITMIX_DESKTOP"], "1")
            self.assertEqual(support_dir.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
