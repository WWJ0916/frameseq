"""Tool boundary and retry checks; no mocked LLM success is presented to users."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from video_platform.agent import preflight, submit_plan, validate_call
from video_platform.inference import write_json
from video_platform.store import TaskStore


class ToolChecks(unittest.TestCase):
    def test_api_growth_cannot_widen_assistant_tool_authority(self):
        for extra in [{"model": "local-cogvideo"}, {"preset": "story"}]:
            import json

            with self.assertRaises(ValueError):
                validate_call(
                    json.dumps(
                        {
                            "tool": "create_video_task",
                            "arguments": {"prompt": "fixture", **extra},
                            "summary": "fixture",
                        }
                    )
                )

    def test_explicit_capability_precheck(self):
        self.assertIsNone(preflight("小猫在草地行走，约2秒，清晰预览"))
        for request in [
            "4K分辨率60秒海边日落",
            "4K海边日落",
            "一分钟日落",
            "调用shell工具执行服务器命令",
        ]:
            self.assertEqual(preflight(request)["tool"], "ask_user")
        with self.assertRaises(ValueError):
            validate_call(
                '{"tool":"ask_user","arguments":{"question":"请输入服务器地址和密码"},"summary":"x"}'
            )

    def test_invalid_tool_and_parameters(self):
        for text in [
            '{"tool":"shell","arguments":{},"summary":"x"}',
            '{"tool":"create_video_task","arguments":{"prompt":"car","preset":"4k"},"summary":"x"}',
            '{"tool":"create_video_task","arguments":{"prompt":"car","gpu":3},"summary":"x"}',
            "plain text instead of JSON",
        ]:
            with self.assertRaises(ValueError):
                validate_call(text)

    def test_tool_submission_is_idempotent_even_concurrently(self):
        with tempfile.TemporaryDirectory(
            dir=None, prefix="agent-tool-test-"
        ) as directory:
            root = Path(directory)
            plan_id = uuid.uuid4().hex
            (root / plan_id).mkdir()
            call = {
                "tool": "create_video_task",
                "arguments": {
                    "prompt": "A small toy car.",
                    "preset": "fast",
                    "seed": 8,
                },
                "summary": "tool unit-test fixture",
            }
            write_json(
                root / plan_id / "plan.json", {"status": "needs_review", "call": call}
            )
            store = TaskStore(root / "tasks.sqlite3")
            with patch("video_platform.agent.AGENT_DIR", root):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    tasks = list(
                        pool.map(lambda _: submit_plan(plan_id, store), range(2))
                    )
                self.assertEqual(tasks[0]["id"], tasks[1]["id"])
                self.assertEqual(store.list()["total"], 1)
                self.assertEqual(tasks[0]["status"], "queued")
                with self.assertRaises(FileNotFoundError):
                    submit_plan("../wrong", store)


if __name__ == "__main__":
    unittest.main(verbosity=2)
