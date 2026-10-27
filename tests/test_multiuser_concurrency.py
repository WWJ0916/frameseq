"""Isolated CPU contention checks; not GPU throughput benchmarks. Pending execution."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
from unittest import TestCase
from video_platform.ownership import as_user
from video_platform.store import Conflict, QueueFull, TaskStore


class MultiuserConcurrencyTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="multiuser-queue-")
        self.path = Path(self.tmp.name) / "data/tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.parameters = {
            "prompt": "The exact same shared test prompt.",
            "seed": 42,
            "model_id": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_prompt_same_key_isolated_between_owners(self):
        barrier = threading.Barrier(4)

        def submit(index):
            uid = "fixture-owner-" + str(index)
            with as_user({"id": uid, "role": "creator"}):
                barrier.wait(timeout=10)
                return (
                    uid,
                    self.store.create(self.parameters, "identical-client-key")["id"],
                )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(len({tid for uid, tid in results}), 4)
        for uid, tid in results:
            with as_user({"id": uid, "role": "creator"}):
                self.assertEqual(self.store.list()["total"], 1)
                self.assertEqual(self.store.get(tid)["owner_id"], uid)
                for foreign_uid, foreign_id in results:
                    if foreign_uid != uid:
                        self.assertIsNone(self.store.get(foreign_id))
                        self.assertIsNone(self.store.cancel(foreign_id))

    def test_thirty_two_same_owner_retries_make_one_task(self):
        user = {"id": "fixture-owner-retry", "role": "creator"}
        barrier = threading.Barrier(32)

        def submit(_):
            with as_user(user):
                barrier.wait(timeout=10)
                return self.store.create(self.parameters, "same-request")["id"]

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(submit, range(32)))
        self.assertEqual(len(set(results)), 1)
        with as_user(user):
            self.assertEqual(self.store.list()["total"], 1)
            with self.assertRaises(Conflict):
                self.store.create({**self.parameters, "seed": 99}, "same-request")

    def test_parallel_overload_respects_global_and_per_user_limits(self):
        barrier = threading.Barrier(32)

        def submit(index):
            uid = "fixture-overload-" + str(index % 8)
            with as_user({"id": uid, "role": "creator"}):
                barrier.wait(timeout=10)
                try:
                    return self.store.create(self.parameters, "request-" + str(index))
                except QueueFull:
                    return None

        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(submit, range(32)))
        self.assertEqual(sum(r is not None for r in results), 10)
        for index in range(8):
            with as_user({"id": "fixture-overload-" + str(index), "role": "creator"}):
                self.assertLessEqual(self.store.list()["total"], 2)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM tasks WHERE status='queued'"
                ).fetchone()[0],
                10,
            )
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_sixteen_coordinators_claim_one_and_reopen_preserves_queue(self):
        for index in range(8):
            with as_user({"id": "fixture-claim-" + str(index), "role": "creator"}):
                self.store.create(self.parameters, "same-key")
        barrier = threading.Barrier(16)

        def claim(_):
            barrier.wait(timeout=10)
            return self.store.claim()

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(claim, range(16)))
        self.assertEqual(sum(r is not None for r in results), 1)
        reopened = TaskStore(self.path)
        self.assertEqual(len(reopened.running()), 1)
        with reopened.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM tasks WHERE status='queued'"
                ).fetchone()[0],
                7,
            )
            self.assertEqual(
                db.execute("SELECT count(DISTINCT id) FROM tasks").fetchone()[0], 8
            )
