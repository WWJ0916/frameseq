"""Operational permissions and persisted state, without payment or generation."""

import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import create_app, TaskRequest
from video_platform.auth import AuthStore
from video_platform.management import (
    ManagementStore,
    local_daily_limit,
    MemberChange,
    ModelChange,
)
from video_platform.ownership import as_user
from video_platform.store import TaskStore
from video_platform.cloud import CloudStore


class ManagementTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "logs").mkdir()
        self.auth = AuthStore(self.root)
        self.admin = self.auth.create_user(
            "operator", "fixture-admin-password", "Operator", admin=True
        )
        self.user = self.auth.create_user(
            "alice",
            "fixture-alice-password",
            "Alice",
            self.auth.invite(self.admin["id"]),
        )
        self.tasks = TaskStore(self.root / "data/tasks.sqlite3")
        self.m = ManagementStore(self.root)
        self.patch = patch("video_platform.api.ROOT", self.root)
        self.patch.start()
        self.client = TestClient(
            create_app(self.tasks, deployment={"auth_required": True})
        )
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.patch.stop()
        self.tmp.cleanup()

    def login(self, admin=True):
        r = self.client.post(
            "/api/auth/login",
            json={
                "username": "operator" if admin else "alice",
                "password": (
                    "fixture-admin-password" if admin else "fixture-alice-password"
                ),
            },
        )
        self.assertEqual(r.status_code, 200)
        return {"X-CSRF-Token": r.json()["csrf"]}

    def test_every_admin_endpoint_requires_admin_and_csrf(self):
        paths = [
            "/api/admin/overview",
            "/api/admin/users",
            "/api/admin/models",
            "/api/admin/campaigns",
            "/api/admin/audit",
        ]
        for p in paths:
            self.assertEqual(self.client.get(p).status_code, 401)
        h = self.login(False)
        for p in paths:
            self.assertEqual(self.client.get(p).status_code, 403)
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(
            self.client.put(
                "/api/admin/models/local-wan",
                headers=h,
                json={"enabled": False, "revision": 0},
            ).status_code,
            403,
        )
        self.login()
        self.assertEqual(
            self.client.put(
                "/api/admin/models/local-wan", json={"enabled": False, "revision": 0}
            ).status_code,
            403,
        )

    def test_counts_real_rows_no_secrets_and_no_paid_members(self):
        with as_user(self.user):
            self.tasks.create(TaskRequest(prompt="private-owner-prompt").parameters())
        self.login()
        d = self.client.get("/api/admin/overview").json()
        self.assertEqual((d["users"], d["tasks"], d["paid_members"]), (2, 1, 0))
        self.assertEqual(sum(x["tasks"] for x in d["trend"]), 1)
        text = self.client.get("/api/admin/users").text
        for secret in [
            "password_hash",
            "csrf",
            "fixture-admin-password",
            "private-owner-prompt",
        ]:
            self.assertNotIn(secret, text)
        self.assertEqual(
            self.client.get("/api/admin/users?q=%27%20OR%201%3D1").json()["total"], 0
        )

    def test_disable_revokes_session_and_admin_is_protected(self):
        token, _, _ = self.auth.login("alice", "fixture-alice-password")
        h = self.login()
        self.assertEqual(
            self.client.patch(
                "/api/admin/users/" + self.admin["id"] + "/status",
                headers=h,
                json={"status": "disabled"},
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.patch(
                "/api/admin/users/" + self.user["id"] + "/status",
                headers=h,
                json={"status": "disabled"},
            ).status_code,
            200,
        )
        self.assertIsNone(self.auth.session(token))
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"username": "alice", "password": "fixture-alice-password"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.patch(
                "/api/admin/users/" + self.user["id"] + "/status",
                headers=h,
                json={"status": "active"},
            ).status_code,
            200,
        )

    def test_membership_expiry_conflict_quota_and_no_payment(self):
        h = self.login()
        url = "/api/admin/users/" + self.user["id"] + "/membership"
        body = {"days": 7, "note": "fixture trial", "revision": 0}
        self.assertFalse(
            self.client.put(url, headers=h, json=body).json()["payment_created"]
        )
        self.assertEqual(local_daily_limit(self.root, self.user["id"]), 20)
        self.assertEqual(self.client.put(url, headers=h, json=body).status_code, 409)
        # A real eleventh queue insertion is allowed, not only a UI badge.
        with as_user(self.user):
            for i in range(11):
                t = self.tasks.create(TaskRequest(prompt=str(i)).parameters())
                with self.tasks.connect() as db:
                    db.execute(
                        "UPDATE tasks SET status='cancelled' WHERE id=?", (t["id"],)
                    )
        with self.auth.connect() as db:
            db.execute("UPDATE memberships SET expires_at=?", (time.time() - 1,))
        self.assertEqual(local_daily_limit(self.root, self.user["id"]), 10)
        self.assertEqual(
            self.client.get("/api/admin/overview").json()["active_trial_members"], 0
        )

    def test_model_switch_server_side_and_optimistic_revision(self):
        h = self.login()
        url = "/api/admin/models/local-wan"
        body = {"enabled": False, "revision": 0}
        self.assertEqual(self.client.put(url, headers=h, json=body).status_code, 200)
        self.assertEqual(self.client.put(url, headers=h, json=body).status_code, 409)
        self.assertEqual(
            self.client.post(
                "/api/tasks", headers=h, json={"prompt": "test"}
            ).status_code,
            409,
        )
        self.assertEqual(self.tasks.list()["total"], 0)
        with self.assertRaises(ValueError):
            self.tasks.create(TaskRequest(prompt="assistant path").parameters())
        self.m.model_update("minimax-h3", ModelChange(enabled=False, revision=0))
        with self.assertRaises(ValueError):
            CloudStore(self.root).first("cloud blocked")
        self.assertEqual(CloudStore(self.root).state()["reserved_cny"], 0)

    def test_campaign_visibility_audit_and_conflicts(self):
        h = self.login()
        body = {
            "title": "<script>test</script>",
            "body": "plain text only",
            "starts_at": time.time() - 1,
            "ends_at": time.time() + 3600,
        }
        r = self.client.post("/api/admin/campaigns", headers=h, json=body)
        self.assertEqual(r.status_code, 201)
        cid = r.json()["id"]
        self.assertEqual(self.client.get("/api/public/campaigns").json()["items"], [])
        body.update(status="published", revision=1)
        self.assertEqual(
            self.client.put(
                "/api/admin/campaigns/" + cid, headers=h, json=body
            ).status_code,
            200,
        )
        self.assertEqual(
            len(self.client.get("/api/public/campaigns").json()["items"]), 1
        )
        self.assertEqual(
            self.client.put(
                "/api/admin/campaigns/" + cid, headers=h, json=body
            ).status_code,
            409,
        )
        body.update(revision=2, ends_at=body["starts_at"] - 1)
        self.assertEqual(
            self.client.put(
                "/api/admin/campaigns/" + cid, headers=h, json=body
            ).status_code,
            422,
        )
        self.assertEqual(self.client.get("/api/admin/audit").json()["total"], 2)
        body.update(status="archived", ends_at=time.time() + 3600)
        self.assertEqual(
            self.client.put(
                "/api/admin/campaigns/" + cid, headers=h, json=body
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get("/api/public/campaigns").json()["items"], [])
