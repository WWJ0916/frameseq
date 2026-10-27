"""CPU fixtures; never submit a real paid request."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import httpx
from video_platform.cloud import (
    CloudStore,
    MiniMax,
    UpstreamError,
    run_job,
    TOPUP_KEY,
    download_video,
)


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cloud-test-", dir=None)
        self.root = Path(self.temp.name)
        self.store = CloudStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_reserves_once_and_changed_prompt_refused(self):
        first, created = self.store.first("sea")
        second, again = self.store.first("sea")
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.state()["reserved_cny"], 2)
        with self.assertRaises(ValueError):
            self.store.first("different scene")

    def test_budget_rejection_has_no_task(self):
        with patch("video_platform.cloud.CAP_FEN", 100):
            with self.assertRaises(ValueError):
                self.store.first("sea")
        self.assertIsNone(self.store.state()["latest"])

    def test_duration_reservation_and_deduplication(self):
        job, _ = self.store.first("fixture", "interactive-eight", duration=8)
        self.assertEqual(job["parameters"]["duration"], 8)
        self.assertEqual(job["reserved_fen"], 400)
        self.assertFalse(
            self.store.first("fixture", "interactive-eight", duration=8)[1]
        )
        with self.assertRaises(ValueError):
            self.store.first("fixture", "interactive-eight", duration=4)
        self.store.update(job["id"], status="succeeded")
        with self.assertRaises(ValueError):
            self.store.first("fixture", "interactive-fifteen", duration=15)
        self.assertEqual(self.store.list()["total"], 1)
        for value in [True, 3, 16, 4.5, "8"]:
            with self.assertRaises(ValueError):
                self.store.first("fixture", "interactive-bad", duration=value)

    @patch("video_platform.cloud.price_is_current", return_value=True)
    def test_custom_duration_reaches_provider_and_remains_on_resume(
        self, _current_price
    ):
        job, _ = self.store.first("fixture", "interactive-custom", duration=7)
        provider = Mock()
        provider.submit.return_value = "12345"
        provider.query.side_effect = httpx.ReadTimeout("fixture")
        run_job(self.store, job["id"], provider)
        provider.submit.assert_called_once_with("fixture", duration=7)
        run_job(self.store, job["id"], provider)
        self.assertEqual(provider.submit.call_count, 1)
        self.assertEqual(self.store.get(job["id"])["parameters"]["duration"], 7)

    def test_explicit_topup_retry_preserves_failure_and_deduplicates(self):
        old, _ = self.store.first("sea")
        self.store.update(
            old["id"], status="failed", error="官方HTTP 402：账户余额不足"
        )
        self.assertTrue(self.store.state()["can_retry_after_topup"])
        new, created = self.store.first("sea", TOPUP_KEY)
        same, again = self.store.first("sea", TOPUP_KEY)
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(new["id"], same["id"])
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(self.store.get(old["id"])["status"], "failed")
        self.assertEqual(self.store.get(old["id"])["reserved_fen"], 0)
        self.assertEqual(self.store.state()["reserved_cny"], 2)
        self.assertEqual(len(self.store.state()["history"]), 2)

    def test_topup_cannot_retry_ambiguous_submission(self):
        old, _ = self.store.first("sea")
        self.store.update(old["id"], status="unknown", error="timeout")
        with self.assertRaises(ValueError):
            self.store.first("sea", TOPUP_KEY)

    def test_interactive_calls_obey_total_cap(self):
        for index in range(5):
            task, created = self.store.first("scene", "interactive-" + str(index))
            self.assertTrue(created)
            self.store.update(task["id"], status="succeeded", result={"fixture": True})
        with self.assertRaises(ValueError):
            self.store.first("scene", "interactive-over-budget")
        self.assertEqual(self.store.state()["reserved_cny"], 10)
        self.assertEqual(self.store.list()["total"], 5)

    @patch("video_platform.cloud.price_is_current", return_value=True)
    def test_ambiguous_post_never_repeated(self, _current_price):
        task, _ = self.store.first("sea")
        provider = Mock()
        provider.submit.side_effect = httpx.ReadTimeout("fixture timeout")
        run_job(self.store, task["id"], provider)
        self.assertEqual(self.store.get(task["id"])["status"], "unknown")
        run_job(self.store, task["id"], provider)
        self.assertEqual(provider.submit.call_count, 1)
        self.assertEqual(self.store.state()["reserved_cny"], 2)

    def test_known_id_resumes_queries_without_submit(self):
        task, _ = self.store.first("sea")
        self.store.update(task["id"], status="waiting", provider_task_id="12345")
        provider = Mock()
        provider.query.side_effect = httpx.ReadTimeout("fixture timeout")
        run_job(self.store, task["id"], provider)
        self.assertEqual(self.store.get(task["id"])["status"], "waiting")
        provider.submit.assert_not_called()
        self.assertEqual(self.store.get(task["id"])["provider_task_id"], "12345")

    def test_failed_download_resume_keeps_reservation_and_id(self):
        task, _ = self.store.first("sea")
        self.store.update(
            task["id"],
            status="failed",
            provider_task_id="12345",
            error="download error",
        )
        resumed, queued = self.store.queue_result_resume(task["id"])
        self.assertTrue(queued)
        self.assertEqual(resumed["provider_task_id"], "12345")
        self.assertFalse(self.store.queue_result_resume(task["id"])[1])
        provider = Mock()
        provider.query.side_effect = httpx.ReadTimeout("fixture")
        run_job(self.store, task["id"], provider)
        provider.submit.assert_not_called()
        self.assertEqual(self.store.state()["reserved_cny"], 2)
        self.assertEqual(self.store.list()["total"], 1)

    def test_resume_refuses_missing_id(self):
        task, _ = self.store.first("sea")
        self.store.update(task["id"], status="unknown")
        with self.assertRaises(ValueError):
            self.store.queue_result_resume(task["id"])

    def test_interrupted_download_cleans_temporary_file(self):
        output = self.root / "video.mp4"

        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield b"partial"
                raise httpx.ReadError("fixture interrupted")

        with patch("video_platform.cloud.httpx.Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.stream.return_value.__enter__.return_value = httpx.Response(
                200, stream=Interrupted()
            )
            with self.assertRaises(httpx.ReadError):
                download_video(
                    "https://algeng-video-infer.oss-cn-shanghai.aliyuncs.com/fixture",
                    output,
                )
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_download_rejects_unverified_hosts_and_credentials(self):
        for url in [
            "http://minimax.cn/file",
            "https://minimax.cn.evil.example/file",
            "https://other.oss-cn-shanghai.aliyuncs.com/file",
            "https://user:pass@minimax.cn/file",
        ]:
            with patch("video_platform.cloud.httpx.Client") as factory:
                client = factory.return_value.__enter__.return_value
                with self.assertRaises(RuntimeError):
                    download_video(url, self.root / "video.mp4")
                client.stream.assert_not_called()

    def test_error_body_cannot_echo_key(self):
        key = "dummy-not-a-real-secret"
        client = Mock()
        client.request.return_value = httpx.Response(401, text=key)
        with patch("video_platform.cloud.httpx.Client") as factory:
            factory.return_value.__enter__.return_value = client
            with self.assertRaises(UpstreamError) as result:
                MiniMax(key).submit("sea")
        self.assertNotIn(key, str(result.exception))

    def test_payload_exact_model_and_duration(self):
        provider = MiniMax("dummy")
        with patch.object(
            provider, "request", return_value={"task_id": "12345"}
        ) as request:
            self.assertEqual(provider.submit("sea"), "12345")
        payload = request.call_args.args[2]
        self.assertEqual(payload["model"], "MiniMax-H3")
        self.assertEqual(payload["duration"], 4)
        self.assertEqual(payload["resolution"], "768P")
        self.assertEqual(payload["content"], [{"type": "text", "text": "sea"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
