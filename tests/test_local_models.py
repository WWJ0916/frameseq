"""Third-model routing and admission fixtures; no actual GPU work."""

import json, tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import create_app, TaskRequest
from video_platform.store import TaskStore, now
from video_platform.worker import inference_command
from video_platform.inference import require_gpu_available
from video_platform.model_profiles import (
    COG_PROFILE_ID,
    COG_NEGATIVE,
    cog_profile_verified,
)


class LocalModelTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=None)
        self.root = Path(self.temp.name)
        (self.root / "logs").mkdir()
        self.store = TaskStore(self.root / "data/tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_not_verified_or_old_worker_never_accepts_cog_task(self):
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.store, deployment={"auth_required": False})
        ) as c:
            body = {"prompt": "fixture", "model": "local-cogvideo", "seed": 73}
            self.assertEqual(c.post("/api/tasks", json=body).status_code, 409)
            (self.root / "logs/cogvideo-profile-verified.json").write_text(
                json.dumps(dict(status="passed", inference_profile=COG_PROFILE_ID))
            )
            self.assertEqual(c.post("/api/tasks", json=body).status_code, 409)
            (self.root / "data/worker.json").write_text(
                json.dumps(
                    dict(
                        state="idle",
                        updated_at=now(),
                        models=["local-wan", "local-cogvideo"],
                    )
                )
            )
            self.assertEqual(c.post("/api/tasks", json=body).status_code, 409)
            (self.root / "data/worker.json").write_text(
                json.dumps(
                    dict(
                        state="idle",
                        updated_at=now(),
                        models=["local-wan", "local-cogvideo"],
                        profiles=[COG_PROFILE_ID],
                    )
                )
            )
            task = c.post("/api/tasks", json=body).json()
            p = task["parameters"]
            self.assertEqual(
                (p["width"], p["height"], p["frames"], p["fps"]), (720, 480, 49, 8)
            )
            self.assertEqual(p["model_id"], "zai-org/CogVideoX-2b")
            self.assertEqual(p["negative_prompt"], COG_NEGATIVE)
            self.assertEqual(p["steps"], 20)
            command = inference_command(task, 1)
            self.assertIn("video_platform.cogvideo", command)
            self.assertNotIn("video_platform.inference", command)
            local = {
                "id": "1" * 32,
                "parameters": TaskRequest(prompt="fixture").parameters(),
            }
            self.assertIn("video_platform.inference", inference_command(local, 1))
            with self.assertRaises(ValueError):
                inference_command(
                    {"id": "1" * 32, "parameters": {**p, "model_id": "unknown"}}, 1
                )
            (self.root / "logs/cogvideo-suspended.json").write_text("{}")
            self.assertEqual(c.post("/api/tasks", json=body).status_code, 409)
            cog = next(
                m
                for m in c.get("/api/models").json()["models"]
                if m["id"] == "local-cogvideo"
            )
            self.assertFalse(cog["configured"])
            self.assertIn("暂不接受", cog["unavailable_reason"])

    def test_worker_uses_recorded_cog_profile_and_rejects_drift(self):
        params = TaskRequest(prompt="fixture", model="local-cogvideo").parameters()
        task = {"id": "f" * 32, "parameters": params}
        command = inference_command(task, 1, precision="float32")
        for option, value in [
            ("--precision", "float16"),
            ("--vae-precision", "float16"),
            ("--scheduler", "dpm"),
            ("--steps", "20"),
        ]:
            self.assertEqual(command[command.index(option) + 1], value)
        with self.assertRaises(ValueError):
            inference_command(
                {**task, "parameters": {**params, "vae_precision": "float32"}}, 1
            )
        with self.assertRaises(ValueError):
            inference_command(
                {**task, "parameters": {**params, "inference_profile": "unknown"}}, 1
            )

    def test_profile_evidence_is_bound_to_exact_version_and_status(self):
        marker = self.root / "logs/cogvideo-profile-verified.json"
        for value in [
            "broken",
            "[]",
            json.dumps(dict(status="passed", inference_profile="old")),
            json.dumps(dict(status="pending", inference_profile=COG_PROFILE_ID)),
        ]:
            marker.write_text(value)
            self.assertFalse(cog_profile_verified(self.root))
        marker.write_text(
            json.dumps(dict(status="passed", inference_profile=COG_PROFILE_ID))
        )
        self.assertTrue(cog_profile_verified(self.root))

    def test_temperature_and_occupied_gpu_admission(self):
        base = {
            "index": 1,
            "memory_used_mib": 0,
            "utilization_percent": 0,
            "temperature_c": 70,
        }
        with patch("video_platform.inference.gpu_snapshot", return_value=base):
            self.assertEqual(require_gpu_available(1), base)
        for change in [
            {"temperature_c": 88},
            {"memory_used_mib": 1},
            {"utilization_percent": 10},
        ]:
            with patch(
                "video_platform.inference.gpu_snapshot", return_value={**base, **change}
            ):
                with self.assertRaises(RuntimeError):
                    require_gpu_available(1)
