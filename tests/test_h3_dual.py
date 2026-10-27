"""Resource and task contracts only; GPU acceptance is recorded separately."""

import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import TaskRequest, create_app
from video_platform.h3_local import stage_command, wait_for_dual_idle
from video_platform.h3_runtime import H3_PROFILE_ID
from video_platform.store import TaskStore
from video_platform.worker import dual_gpu_choice, inference_command


class DualContracts(unittest.TestCase):
    def test_encode_to_sample_waits_for_both_cards_to_cool(self):
        pauses = []
        with patch(
            "video_platform.h3_local.require_gpu_available",
            side_effect=[
                {"temperature_c": 69},
                {"temperature_c": 48},
                {"temperature_c": 64},
                {"temperature_c": 49},
            ],
        ) as probe:
            self.assertTrue(wait_for_dual_idle([0, 1], pause=pauses.append, attempts=2))
        self.assertEqual(pauses, [5])
        self.assertEqual(probe.call_count, 4)

    def test_pair_requires_both_permitted_cards_idle_and_cool(self):
        rows = [(0, 0, 0, 50), (1, 0, 0, 55), (2, 0, 0, 40)]
        self.assertEqual(dual_gpu_choice(rows), [0, 1])
        for bad in (
            [(0, 0, 0, 50)],
            [(0, 0, 0, 50), (1, 1, 0, 40)],
            [(0, 0, 0, 50), (1, 0, 1, 40)],
            [(0, 0, 0, 50), (1, 0, 0, 65)],
        ):
            with self.assertRaises(RuntimeError):
                dual_gpu_choice(bad)
        with self.assertRaises(ValueError):
            dual_gpu_choice(rows, (1, 1))

    def test_one_task_uses_one_dual_sampler_and_inherits_three_locks(self):
        params = TaskRequest(model="local-h3", prompt="scene", seed=12).parameters()
        self.assertEqual(params["gpu_count"], 2)
        self.assertEqual(params["parallelism"], "sequence")
        with patch(
            "video_platform.worker.runtime_config",
            return_value={"python": "/own/python"},
        ):
            argv = inference_command({"id": "a" * 32, "parameters": params}, 0)
            self.assertEqual(argv.count("video_platform.h3_local"), 1)
            self.assertEqual(
                argv[argv.index("--gpus") + 1 : argv.index("--gpus") + 3], ["0", "1"]
            )
            with self.assertRaises(ValueError):
                inference_command({"id": "a" * 32, "parameters": params}, 1)
        config = {
            "python": "/own/python",
            "runner": "/own/single.py",
            "dual_runner": "/own/dual.py",
        }
        argv = stage_command(
            config,
            "sample",
            Path("/own/sample"),
            0,
            "task-" + "a" * 32,
            params,
            [8, 9, 10],
            {"conditioning": "/own/cond.pt"},
            gpus=[0, 1],
        )
        self.assertIn("/own/dual.py", argv)
        self.assertNotIn("/own/single.py", argv)
        self.assertEqual(argv.count("--inference-lock-fd"), 3)
        self.assertEqual(argv[argv.index("--seed") + 1], "12")
        with self.assertRaises(ValueError):
            stage_command(
                config,
                "sample",
                Path("/own/sample"),
                0,
                "task-" + "a" * 32,
                params,
                [8, 9],
                {"conditioning": "x"},
                gpus=[0, 1],
            )

    def test_maintenance_rejects_submission_without_creating_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TaskStore(root / "data/tasks.sqlite3")
            (root / "data/generation-paused.json").write_text(
                json.dumps({"enabled": True})
            )
            with patch("video_platform.api.ROOT", root), TestClient(
                create_app(store, {"auth_required": False})
            ) as client:
                for model in ("local-wan", "local-h3"):
                    reply = client.post(
                        "/api/tasks", json={"prompt": "fixture", "model": model}
                    )
                    self.assertEqual(reply.status_code, 503, reply.text)
            self.assertEqual(store.list()["total"], 0)


if __name__ == "__main__":
    unittest.main()
