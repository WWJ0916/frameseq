"""Credential file boundaries only; no real keys or network calls."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from scripts.configure_minimax import check_configuration, save_key


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="provider-config-", dir=None)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_file_and_status_redaction(self):
        key = "dummy-key-not-a-real-credential"
        self.assertFalse(check_configuration(self.root)["configured"])
        path = save_key(self.root, "cn", key)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        status = check_configuration(self.root)
        self.assertTrue(status["configured"])
        self.assertFalse(status["network_request_made"])
        self.assertFalse(status["api_access_verified"])
        self.assertNotIn(key, json.dumps(status))
        self.assertNotIn("api_key", status)

    def test_refuses_silent_overwrite(self):
        path = save_key(self.root, "cn", "dummy-first")
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            save_key(self.root, "global", "dummy-second")
        self.assertEqual(path.read_bytes(), original)

    def test_refuses_shared_permissions(self):
        path = save_key(self.root, "cn", "dummy-test")
        os.chmod(path, 0o644)
        with self.assertRaises(ValueError):
            check_configuration(self.root)

    def test_refuses_symlink(self):
        external = self.root / "other"
        external.mkdir()
        (self.root / ".secrets").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            save_key(self.root, "cn", "dummy-test")
        self.assertEqual(list(external.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
