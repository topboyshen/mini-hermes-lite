from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
