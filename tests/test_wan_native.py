import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import TaskRequest, create_app
from video_platform.model_profiles import WAN_NATIVE, WAN_NATIVE_ID, wan_native_verified
from video_platform.store import TaskStore, now
from video_platform.worker import inference_command


class NativeWanTests(unittest.TestCase):
    def test_worker_keeps_full_profile_and_rejects_drift(self):
        params = TaskRequest(prompt="rain courier", preset="cinema").parameters()
        command = inference_command(
            {"id": "fixture", "parameters": params}, 1, "float32"
        )
        self.assertEqual(command[command.index("--precision") + 1], "float16")
        self.assertEqual(command[command.index("--width") + 1], "832")
        self.assertEqual(command[command.index("--max-sequence-length") + 1], "512")
        params["steps"] = 20
        with self.assertRaisesRegex(ValueError, "drifted"):
            inference_command({"id": "fixture", "parameters": params}, 1)

    def test_marker_matches_profile_and_creator_cannot_submit_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "logs").mkdir()
            store = TaskStore(root / "data/tasks.sqlite3")
            (root / "data/worker.json").write_text(
                json.dumps(
                    {"state": "idle", "updated_at": now(), "profiles": [WAN_NATIVE_ID]}
                )
            )
            marker = root / "logs/wan-native480-verified.json"
            self.assertFalse(wan_native_verified(root))
            with patch("video_platform.api.ROOT", root), patch(
                "video_platform.api.is_admin", return_value=False
            ), TestClient(
                create_app(store, deployment={"auth_required": False})
            ) as client:
                response = client.post(
                    "/api/tasks", json={"prompt": "fixture", "preset": "cinema"}
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(store.list()["total"], 0)
                marker.write_text(
                    json.dumps({"status": "passed", "profile": WAN_NATIVE})
                )
                self.assertTrue(wan_native_verified(root))
                self.assertEqual(
                    client.post(
                        "/api/tasks", json={"prompt": "fixture", "preset": "cinema"}
                    ).status_code,
                    202,
                )
            record = json.loads(marker.read_text())
            record["profile"]["width"] = 480
            marker.write_text(json.dumps(record))
            self.assertFalse(wan_native_verified(root))
