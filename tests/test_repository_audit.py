"""Publication checks must reject private material without printing its value."""

import json, tempfile
from pathlib import Path
from unittest import TestCase
from scripts.audit_repository import inspect


class RepositoryAuditTests(TestCase):
    def test_secret_is_rejected_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            secret = "sk-" + "api-" + ("x" * 40)
            (root / "example.py").write_text("token=" + repr(secret))
            report = inspect(root)
            self.assertEqual(report["status"], "failed")
            self.assertNotIn(secret, json.dumps(report))
            self.assertEqual(report["findings"][0]["rule"], "provider_key")

    def test_private_database_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data").mkdir()
            (root / "data/tasks.sqlite3").write_bytes(b"fixture")
            self.assertEqual(inspect(root)["status"], "failed")

    def test_synthetic_credentials_in_tests_are_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "test.py").write_text('password="fixture-not-a-real-credential"')
            self.assertEqual(inspect(root)["status"], "passed")

    def test_large_media_allowlist_requires_exact_size_and_hash(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "README.md").write_text("fixture review package")
            (root / "preview/assets").mkdir(parents=True)
            (root / "preview/assets/episode-demo.mp4").write_bytes(b"x" * (6 * 1024**2))
            report = inspect(root)
            self.assertEqual(report["status"], "failed")
            self.assertIn("large_file", [item["rule"] for item in report["findings"]])
