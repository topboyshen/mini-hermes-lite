from pathlib import Path
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

from mini_hermes import config as config_mod
from mini_hermes.config import Settings, load_env_file


class ConfigTests(unittest.TestCase):
    def test_load_env_does_not_overwrite_exported_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("MODEL=file-model\nTELEGRAM_ALLOWED_USER_IDS=1, 2\n", encoding="utf-8")
            with patch.dict(os.environ, {"MODEL": "exported-model"}, clear=True):
                load_env_file(path)
                settings = Settings.from_env()
                self.assertEqual(settings.model, "exported-model")
                self.assertEqual(settings.allowed_user_ids, frozenset({1, 2}))

    def test_report_model_not_on_settings_and_warns_once(self) -> None:
        config_mod._report_model_warned = False
        with patch.dict(os.environ, {"REPORT_MODEL": "openai:gpt-4o", "MODEL": "x"}, clear=True):
            with self.assertLogs(config_mod._LOGGER, level=logging.WARNING) as captured:
                settings = Settings.from_env()
                Settings.from_env()  # second call should not warn again
            self.assertFalse(hasattr(settings, "report_model"))
            self.assertEqual(len(captured.records), 1)
            self.assertIn("REPORT_MODEL", captured.records[0].getMessage())


if __name__ == "__main__":
    unittest.main()
