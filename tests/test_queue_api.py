"""CPU-only correctness checks; they do not simulate successful model inference."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import fcntl
import subprocess
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from video_platform.api import TaskRequest, create_app
from video_platform.store import Conflict, QueueFull, TaskStore
from tests.media_fixture import synthetic_video


class QueueChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=None, prefix="queue-test-")
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / "tasks.sqlite3")
        self.params = TaskRequest(prompt="A red toy car.").parameters()

    def tearDown(self):
        self.temp.cleanup()

    def test_fifo_cancel_and_reopen(self):
        first = self.store.create(self.params)
        second = self.store.create(self.params)
        self.store.cancel(second["id"])
        reopened = TaskStore(self.store.path)
        self.assertEqual(reopened.get(second["id"])["status"], "cancelled")
        self.assertEqual(reopened.claim()["id"], first["id"])
        with self.assertRaises(Conflict):
            reopened.cancel(first["id"])
        reopened.finish(
            first["id"], "failed", error="deliberate unit-test failure record"
        )
        self.assertEqual(reopened.get(first["id"])["status"], "failed")
        self.assertIsNone(reopened.claim())

    def test_concurrent_claim_is_singleton(self):
        self.store.create(self.params)
        self.store.create(self.params)
        barrier = threading.Barrier(2)

        def claim():
            other = TaskStore(self.store.path)
            barrier.wait()
            return other.claim()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.store.running()), 1)
        self.assertTrue(self.store.has_queued())

    def test_idempotency_and_capacity(self):
        first = self.store.create(self.params, "same-submit")
        self.assertEqual(
            first["id"], self.store.create(self.params, "same-submit")["id"]
        )
        with self.assertRaises(Conflict):
            self.store.create({**self.params, "seed": 9}, "same-submit")
        for _ in range(9):
            self.store.create(self.params)
        with self.assertRaises(QueueFull):
            self.store.create(self.params)
        self.assertEqual(self.store.list()["total"], 10)

    def test_project_admission_lock_across_processes(self):
        # CPU-only resource coordination test, isolated from real model runs.
        (self.root / "logs").mkdir()
        code = "import sys; from pathlib import Path; import video_platform.inference as m; m.ROOT=Path(sys.argv[1]); print(m.project_inference_active())"

        def check():
            return subprocess.run(
                [sys.executable, "-c", code, str(self.root)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        with (self.root / "logs/model-inference.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(check(), "True")
        self.assertEqual(check(), "False")

    def test_api_validation_and_pending_artifact(self):
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            for payload in [
                {"prompt": " "},
                {"prompt": "car", "preset": "unknown"},
                {"prompt": "car", "seed": -1},
                {"prompt": "car", "seed": "42"},
                {"prompt": "car", "gpu": 3},
            ]:
                self.assertEqual(
                    client.post("/api/tasks", json=payload).status_code, 422
                )
            created = client.post(
                "/api/tasks",
                json={"prompt": "car"},
                headers={"Idempotency-Key": "retry-click"},
            )
            self.assertEqual(created.status_code, 202)
            task_id = created.json()["id"]
            again = client.post(
                "/api/tasks",
                json={"prompt": "car"},
                headers={"Idempotency-Key": "retry-click"},
            )
            self.assertEqual(task_id, again.json()["id"])
            self.assertEqual(
                client.get(f"/api/tasks/{task_id}").json()["queue_position"], 1
            )
            self.assertEqual(client.get(f"/api/tasks/{task_id}/video").status_code, 409)
            self.assertEqual(client.get("/api/tasks/not-a-task/video").status_code, 404)
            self.assertEqual(
                client.post(f"/api/tasks/{task_id}/cancel").json()["status"],
                "cancelled",
            )
            self.assertEqual(
                client.post(f"/api/tasks/{task_id}/cancel").status_code, 409
            )

    def test_synthetic_video_range_download_response(self):
        # CPU animation tests serving only, never model generation.
        source = synthetic_video(self.root)
        self.assertTrue(source.is_file())
        task = self.store.create(self.params)
        self.store.claim()
        self.store.finish(
            task["id"], "succeeded", metrics={"fixture": "HTTP serving test only"}
        )
        run = self.root / "runs" / ("task-" + task["id"])
        run.mkdir(parents=True)
        (run / "video.mp4").write_bytes(source.read_bytes())
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            path = f"/api/tasks/{task['id']}/video"
            partial = client.get(path, headers={"Range": "bytes=0-31"})
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, source.read_bytes()[:32])
            downloaded = client.get(path + "?download=true")
            self.assertEqual(downloaded.content, source.read_bytes())
            self.assertIn("attachment", downloaded.headers["content-disposition"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
