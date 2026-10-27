"""Real isolated HTTP/SQLite checks for the portable CPU review boundary."""

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from fastapi.testclient import TestClient
from scripts.container_review import initialize, create_review_app, SOURCE
from video_platform.api import create_app
from video_platform.auth import AuthStore
from video_platform.store import TaskStore


class ContainerReviewTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="frameseq-review-")
        self.root = Path(self.tmp.name) / "runtime"
        initialize(self.root)
        self.store = TaskStore(self.root / "data/tasks.sqlite3")
        self.root_patch = patch("video_platform.api.ROOT", self.root)
        self.root_patch.start()
        self.app = create_review_app(create_app(self.store))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.root_patch.stop()
        self.tmp.cleanup()

    def login(self):
        credential = json.loads(
            (self.root / ".secrets/admin-bootstrap.json").read_text()
        )
        response = self.client.post(
            "/api/auth/login", json={k: credential[k] for k in ("username", "password")}
        )
        self.assertEqual(response.status_code, 200)
        return {"X-CSRF-Token": response.json()["csrf"]}

    def test_bootstrap_is_private_idempotent_and_models_disabled(self):
        secret = self.root / ".secrets/admin-bootstrap.json"
        before = hashlib.sha256(secret.read_bytes()).hexdigest()
        initialize(self.root)
        self.assertEqual(before, hashlib.sha256(secret.read_bytes()).hexdigest())
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        with AuthStore(self.root).connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            self.assertEqual(
                db.execute("SELECT sum(enabled) FROM model_settings").fetchone()[0], 0
            )

    def test_anonymous_preview_and_health_do_not_expose_private_pages(self):
        self.assertEqual(self.client.get("/review/").status_code, 200)
        self.assertIn("CPU 审阅版", self.client.get("/review/").text)
        health = self.client.get("/healthz").json()
        self.assertFalse(health["generation_enabled"])
        self.assertEqual(self.client.get("/api/tasks").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        self.assertEqual(
            self.client.get("/studio", follow_redirects=False).status_code, 303
        )
        self.assertEqual(
            self.client.get("/healthz", headers={"host": "evil.example"}).status_code,
            400,
        )

    def test_authenticated_generation_cannot_spawn_or_enqueue(self):
        headers = self.login()
        with patch("video_platform.api.subprocess.Popen") as spawn:
            for path in (
                "/api/tasks",
                "/api/tasks/",
                "/api/cloud/tasks",
                "/api/cloud/first-test",
                "/api/cloud/retry-after-topup",
                "/api/cloud/tasks/x/resume-result",
                "/api/agent/plan",
                "/api/agent/plans/x/submit",
                "/api/projects/x/episodes/y/export",
                "/api/projects/x/episodes/y/exports",
                "/api/edits",
            ):
                response = self.client.post(
                    path, json={"prompt": "fixture review"}, headers=headers
                )
                self.assertEqual(response.status_code, 503, path)
            spawn.assert_not_called()
        self.assertEqual(self.store.list()["total"], 0)

    def test_real_project_persists_and_admin_console_has_new_instance_counts(self):
        headers = self.login()
        response = self.client.post(
            "/api/projects", json={"title": "朋友审阅项目"}, headers=headers
        )
        self.assertEqual(response.status_code, 201)
        project_id = response.json()["id"]
        core = create_app(self.store)
        with TestClient(create_review_app(core)) as reopened:
            reopened.cookies.update(self.client.cookies)
            self.assertEqual(
                reopened.get("/api/projects/" + project_id).json()["title"],
                "朋友审阅项目",
            )
            overview = reopened.get("/api/admin/overview").json()
            self.assertEqual(
                (overview["users"], overview["tasks"], overview["paid_members"]),
                (1, 0, 0),
            )
            self.assertIn("CPU 审阅版", reopened.get("/admin").text)

    def test_external_language_provider_is_blocked_even_if_volume_flag_enabled(self):
        (self.root / "data/language-providers.json").write_text(
            json.dumps({"enabled": True})
        )
        headers = self.login()
        with patch("video_platform.language_providers.draft_prompt") as provider:
            response = self.client.post(
                "/api/assistant/draft",
                headers=headers,
                json={
                    "provider": "deepseek",
                    "model": "fixture",
                    "api_key": "fixture-customer-key",
                    "request": "A boat floats.",
                    "allow_paid_call": True,
                },
            )
            self.assertEqual(response.status_code, 503)
            provider.assert_not_called()

    def test_refuses_to_repurpose_existing_non_review_database(self):
        existing = Path(self.tmp.name) / "existing"
        existing.mkdir()
        AuthStore(existing)
        with self.assertRaisesRegex(RuntimeError, "Refusing"):
            initialize(existing)
        self.assertFalse((existing / ".secrets/admin-bootstrap.json").exists())

    def test_public_video_range_and_path_traversal_boundary(self):
        response = self.client.get(
            "/review/assets/wan-rain.mp4", headers={"Range": "bytes=0-127"}
        )
        self.assertEqual(response.status_code, 206)
        expected = (SOURCE / "preview/assets/wan-rain.mp4").read_bytes()[:128]
        self.assertEqual(response.content, expected)
        for path in (
            "/review/%2e%2e/requirements-platform.txt",
            "/review/.secrets/admin-bootstrap.json",
        ):
            self.assertEqual(self.client.get(path).status_code, 404)
