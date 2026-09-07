import logging
from pathlib import Path
import tempfile
import unittest

from mini_hermes.logging_setup import setup_logging


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        logging.shutdown()
        logging.getLogger().handlers.clear()

    def tearDown(self) -> None:
        logging.shutdown()
        logging.getLogger().handlers.clear()

    def test_file_handler_reopens_after_external_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            log_dir = Path(tempdir)
            active_log = log_dir / "mini-hermes.log"
            rotated_log = log_dir / "mini-hermes.log.1"
            setup_logging(log_dir)

            logger = logging.getLogger("rotation-test")
            logger.info("before rotation")
            for handler in logging.getLogger().handlers:
                handler.flush()

            active_log.rename(rotated_log)
            active_log.touch()
            logger.info("after rotation")
            for handler in logging.getLogger().handlers:
                handler.flush()

            self.assertIn("before rotation", rotated_log.read_text())
            self.assertNotIn("after rotation", rotated_log.read_text())
            self.assertIn("after rotation", active_log.read_text())


if __name__ == "__main__":
    unittest.main()
