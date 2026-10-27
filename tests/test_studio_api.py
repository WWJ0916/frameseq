"""Isolated HTTP fixture tests; not evidence of cloud generation."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import create_app, TaskRequest
from video_platform.cloud import CloudStore
from video_platform.store import TaskStore
from tests.media_fixture import synthetic_video


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-api-", dir=None)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / "data/tasks.sqlite3")
        (self.root / "logs").mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_library_merges_sources_and_preserves_failure(self):
        local = self.store.create(TaskRequest(prompt="local fixture").parameters())
        cloud, _ = CloudStore(self.root).first("cloud fixture")
        CloudStore(self.root).update(
            cloud["id"], status="failed", error="fixture HTTP 402"
        )
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            response = client.get("/api/library?limit=100")
            self.assertEqual(response.status_code, 200)
            items = response.json()["items"]
            self.assertEqual(response.json()["total"], 2)
            self.assertEqual({task["origin"] for task in items}, {"cloud", "local"})
            self.assertIsNone(
                next(t for t in items if t["origin"] == "cloud")["video_url"]
            )
            model_response = client.get("/api/models").json()
            self.assertFalse(model_response["models"][1]["verified"])
            self.assertFalse(model_response["models"][1]["configured"])

    def test_public_catalog_does_not_invent_instance_verification(self):
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            self.assertTrue(
                all(
                    not m["verified"]
                    for m in client.get("/api/public/catalog").json()["models"]
                )
            )
            self.assertEqual(
                client.get("/api/public/demo").json()["status"], "not_installed"
            )
            (self.root / "logs/fp16-api-report.json").write_text("{}")
            self.assertTrue(
                client.get("/api/public/catalog").json()["models"][0]["verified"]
            )
            (self.root / "logs/cogvideo-suspended.json").write_text("{}")
            self.assertIn(
                "暂停", client.get("/api/public/catalog").json()["models"][1]["name"]
            )

    def test_cloud_submission_requires_idempotency_and_first_validation(self):
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.api.subprocess.Popen"
        ) as spawn, TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            for headers in ({}, {"Idempotency-Key": "old-client"}):
                self.assertEqual(
                    client.post(
                        "/api/cloud/tasks", json={"prompt": "fixture"}, headers=headers
                    ).status_code,
                    410,
                )
            self.assertEqual(CloudStore(self.root).list()["total"], 0)
            spawn.assert_not_called()

    def test_cloud_artifacts_are_fixed_paths_and_range_readable(self):
        cloud_store = CloudStore(self.root)
        task, _ = cloud_store.first("HTTP fixture only")
        cloud_store.update(task["id"], status="succeeded", result={"fixture": True})
        run = self.root / "runs" / ("cloud-" + task["id"])
        run.mkdir(parents=True)
        original = synthetic_video(self.root).read_bytes()
        (run / "video.mp4").write_bytes(original)
        (run / "contact-sheet.jpg").write_bytes(b"fixture")
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            response = client.get(
                f"/api/cloud/tasks/{task['id']}/video", headers={"Range": "bytes=0-31"}
            )
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, original[:32])
            self.assertEqual(
                client.get(f"/api/cloud/tasks/{task['id']}/preview").content, b"fixture"
            )
            poster = client.get(f"/api/cloud/tasks/{task['id']}/poster")
            self.assertEqual(poster.status_code, 200)
            self.assertEqual(poster.content[:2], b"\xff\xd8")
            cached = run / "poster.jpg"
            stamp = cached.stat().st_mtime_ns
            self.assertEqual(
                client.get(f"/api/cloud/tasks/{task['id']}/poster").content,
                poster.content,
            )
            self.assertEqual(cached.stat().st_mtime_ns, stamp)
            self.assertEqual((run / "video.mp4").read_bytes(), original)
            self.assertEqual(
                client.get(f"/api/cloud/tasks/not-an-id/poster").status_code, 404
            )

    def test_resume_only_starts_existing_id_once_and_never_reserves_again(self):
        cloud = CloudStore(self.root)
        task, _ = cloud.first("fixture")
        cloud.update(
            task["id"],
            status="failed",
            provider_task_id="12345",
            error="fixture download failure",
        )
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.api.subprocess.Popen"
        ) as spawn, TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            for _ in range(2):
                self.assertEqual(
                    client.post(
                        f"/api/cloud/tasks/{task['id']}/resume-result"
                    ).status_code,
                    410,
                )
            spawn.assert_not_called()
        self.assertEqual(cloud.list()["total"], 1)
        self.assertEqual(cloud.state()["reserved_cny"], 2)
        self.assertEqual(cloud.get(task["id"])["status"], "failed")

    def test_resume_missing_official_id_never_spawns(self):
        cloud = CloudStore(self.root)
        task, _ = cloud.first("fixture")
        cloud.update(task["id"], status="unknown")
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.api.subprocess.Popen"
        ) as spawn, TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            self.assertEqual(
                client.post(f"/api/cloud/tasks/{task['id']}/resume-result").status_code,
                410,
            )
            spawn.assert_not_called()

    def test_expired_pricing_refuses_before_reserving_or_spawning(self):
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.api.subprocess.Popen"
        ) as spawn, patch(
            "scripts.configure_minimax.check_configuration",
            return_value={"configured": True, "region": "cn"},
        ), patch(
            "video_platform.cloud.price_is_current", return_value=False
        ), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            self.assertEqual(
                client.post(
                    "/api/cloud/first-test", json={"prompt": "fixture"}
                ).status_code,
                410,
            )
            self.assertEqual(CloudStore(self.root).list()["total"], 0)
            self.assertEqual(CloudStore(self.root).state()["reserved_cny"], 0)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
