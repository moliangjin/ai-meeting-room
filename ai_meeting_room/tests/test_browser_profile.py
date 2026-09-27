from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ai_meeting_room.brain.browser_profile import BrowserProfileManager


class BrowserProfileTests(unittest.TestCase):
    def test_profile_is_created_private_and_separate_from_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = BrowserProfileManager(data_dir=Path(directory) / "app-data" / "browser_profiles")
            path = manager.create()
            self.assertTrue(path.is_dir())
            self.assertEqual(path.name, "chatgpt_brain")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
            self.assertTrue(manager.health_check())

    def test_profile_id_cannot_escape_base_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                BrowserProfileManager("../personal-chrome", data_dir=directory)


if __name__ == "__main__":
    unittest.main()
