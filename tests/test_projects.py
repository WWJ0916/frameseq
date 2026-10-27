"""Isolated project/version workflow, no GPU or paid requests."""

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import create_app, TaskRequest
from video_platform.store import TaskStore
from video_platform.cloud import CloudStore, PARAMS


class ProjectTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=None)
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / "data/tasks.sqlite3")
        self.body = {
            "title": "fixture series",
            "episodes": [
                {
                    "id": "1" * 32,
                    "title": "EP1",
                    "shots": [
                        {
                            "id": "2" * 32,
                            "title": "A",
                            "description": "sea",
                            "seconds": 8,
                        },
                        {
                            "id": "3" * 32,
                            "title": "B",
                            "description": "boat",
                            "seconds": 4,
                        },
                    ],
                }
            ],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_persistence_reorder_conflict_and_no_generation(self):
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.api.subprocess.Popen"
        ) as spawn, TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as c:
            p = c.post("/api/projects", json=self.body).json()
            body = {**self.body, "revision": p["revision"]}
            body["episodes"][0]["shots"].reverse()
            updated = c.put("/api/projects/" + p["id"], json=body)
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["episodes"][0]["shots"][0]["id"], "3" * 32)
            self.assertEqual(
                c.put("/api/projects/" + p["id"], json=body).status_code, 409
            )
            self.assertEqual(c.get("/api/projects/" + p["id"]).json(), updated.json())
            self.assertEqual(self.store.list()["total"], 0)
            self.assertFalse((self.root / "data/cloud.sqlite3").exists())
            spawn.assert_not_called()

    def test_real_task_link_dedup_selection_and_history_protected(self):
        task = self.store.create(TaskRequest(prompt="fixture").parameters())
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as c:
            p = c.post("/api/projects", json=self.body).json()
            link = dict(
                episode_id="1" * 32,
                shot_id="2" * 32,
                origin="local",
                task_id=task["id"],
                revision=1,
            )
            p = c.post(f"/api/projects/{p['id']}/versions", json=link).json()
            self.assertEqual(len(p["versions"]), 1)
            self.assertEqual(
                c.post(f"/api/projects/{p['id']}/versions", json=link).json()[
                    "revision"
                ],
                p["revision"],
            )
            version = p["versions"][0]
            self.assertEqual(
                c.post(
                    f"/api/projects/{p['id']}/versions/{version['id']}/choose",
                    json={"revision": p["revision"]},
                ).status_code,
                409,
            )
            bad = {**self.body, "episodes": [], "revision": p["revision"]}
            self.assertEqual(
                c.put("/api/projects/" + p["id"], json=bad).status_code, 422
            )
            self.assertEqual(
                c.post(
                    f"/api/projects/{p['id']}/versions",
                    json={**link, "task_id": "f" * 32},
                ).status_code,
                404,
            )

    def test_duplicate_ids_and_invalid_duration_rejected(self):
        self.body["episodes"][0]["shots"][1]["id"] = "2" * 32
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as c:
            self.assertEqual(c.post("/api/projects", json=self.body).status_code, 422)
            for duration in [True, 3, 16, 7.5]:
                self.assertEqual(
                    c.post(
                        "/api/cloud/tasks",
                        json={"prompt": "fixture", "duration": duration},
                    ).status_code,
                    422,
                )

    def test_old_cloud_database_migrates_without_changing_history(self):
        path = self.root / "data/cloud.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE jobs (id TEXT PRIMARY KEY,request_key TEXT UNIQUE,prompt TEXT,status TEXT,stage TEXT,provider_task_id TEXT,error TEXT,result TEXT,reserved_fen INTEGER,created_at TEXT,updated_at TEXT)"
            )
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "a" * 32,
                    "old",
                    "boat",
                    "succeeded",
                    "done",
                    "123",
                    None,
                    None,
                    200,
                    "date",
                    "date",
                ),
            )
        task = CloudStore(self.root).get("a" * 32)
        self.assertEqual(task["parameters"], PARAMS)
        self.assertEqual(task["reserved_fen"], 200)
