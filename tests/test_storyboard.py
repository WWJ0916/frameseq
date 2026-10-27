"""Draft persistence and revision tests; no paid requests or video generation."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from video_platform.api import create_app
from video_platform.store import TaskStore


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=None, prefix="board-test-")
        self.root = Path(self.temp.name)
        self.store = TaskStore(self.root / "data/tasks.sqlite3")
        self.payload = {
            "title": "fixture draft",
            "synopsis": "not generated",
            "shots": [
                {"description": "shot " + str(i), "seconds": 10, "narration": "line"}
                for i in range(6)
            ],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_save_reopen_reorder_version_conflict_and_no_generation(self):
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.api.subprocess.Popen"
        ) as spawn, TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            first = client.post("/api/storyboards", json=self.payload)
            self.assertEqual(first.status_code, 201)
            board = first.json()
            self.assertEqual(board["total_seconds"], 60)
            self.assertEqual(board["h3_output_estimate_cny"], 30)
            self.assertFalse(board["execution_supported"])
            url = "/api/storyboards/" + board["id"]
            self.assertEqual(client.get(url).json(), board)
            edited = {
                **self.payload,
                "revision": 1,
                "shots": list(reversed(self.payload["shots"])),
            }
            self.assertEqual(client.put(url, json=edited).json()["revision"], 2)
            self.assertEqual(client.put(url, json=edited).status_code, 409)
            self.assertEqual(
                client.get(url).json()["shots"][0]["description"], "shot 5"
            )
            self.assertEqual(len(client.get("/api/storyboards").json()["items"]), 1)
            self.assertEqual(client.get("/storyboard").status_code, 200)
            spawn.assert_not_called()
            self.assertFalse((self.root / "data/cloud.sqlite3").exists())
            self.assertEqual(self.store.list()["total"], 0)

    def test_invalid_values_and_unknown_ids(self):
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as client:
            for change in [
                {"title": " "},
                {"shots": []},
                {"shots": [{"description": " ", "seconds": 10}]},
                {"shots": [{"description": "x", "seconds": 60}]},
                {"extra": "reject"},
            ]:
                self.assertEqual(
                    client.post(
                        "/api/storyboards", json={**self.payload, **change}
                    ).status_code,
                    422,
                )
            self.assertEqual(client.get("/api/storyboards/bad-id").status_code, 404)
            self.assertEqual(
                client.put(
                    "/api/storyboards/" + "a" * 32, json=self.payload
                ).status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
