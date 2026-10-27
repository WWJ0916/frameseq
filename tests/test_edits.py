"""CPU fixture editing tests; the fixtures are not AI-generated footage."""

import hashlib
from pathlib import Path
import shutil
import tempfile
from unittest import TestCase
from unittest.mock import patch
import imageio.v2 as imageio
import numpy as np
from fastapi.testclient import TestClient
from tests.media_fixture import synthetic_video
from video_platform.api import create_app
from video_platform.edits import EditStore, EditRequest, render
from video_platform.store import TaskStore
from video_platform.ownership import as_user


class EditTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir()
        (self.root / "logs").mkdir()
        self.tasks = TaskStore(self.root / "data/tasks.sqlite3")
        self.edits = EditStore(self.root)
        fixture = synthetic_video(self.root)
        self.a = self.video(fixture, "A")
        self.b = self.video(fixture, "B")

    def tearDown(self):
        self.tmp.cleanup()

    def video(self, path, prompt):
        t = self.tasks.create(dict(prompt=prompt))
        self.tasks.claim()
        target = self.root / "runs" / ("task-" + t["id"])
        target.mkdir()
        shutil.copyfile(path, target / "video.mp4")
        self.tasks.finish(
            t["id"],
            "succeeded",
            metrics={
                "validation": dict(
                    duration_seconds=81 / 16,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            },
        )
        return dict(origin="local", task_id=t["id"])

    def body(self, **kw):
        return EditRequest(
            **dict(
                source=self.a, start=1, end=3, prompt="change selected interval", **kw
            )
        )

    def test_plan_dedup_bounds_ownership_and_no_generation(self):
        draft, queued = self.edits.prepare(self.body(), "draft1")
        self.assertFalse(queued)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(
            self.edits.prepare(self.body(), "draft1")[0]["id"], draft["id"]
        )
        bad = self.body()
        bad.end = 9
        with self.assertRaises(ValueError):
            self.edits.prepare(bad, "bad")
        other = self.body()
        other.start = 2
        with self.assertRaises(ValueError):
            self.edits.prepare(other, "draft1")
        with as_user({"id": "another-account", "role": "creator"}):
            self.assertIsNone(self.edits.get(draft["id"]))
            with self.assertRaises(ValueError):
                self.edits.prepare(self.body(), "foreign")
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.edits.subprocess.Popen"
        ) as spawn, TestClient(
            create_app(self.tasks, deployment={"auth_required": False})
        ) as c:
            self.assertFalse(
                c.get("/api/edits/capabilities").json()["ai_local_reshoot"]
            )
            r = c.post(
                "/api/edits",
                headers={"Idempotency-Key": "httpdraft"},
                json=self.body().model_dump(),
            )
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.json()["status"], "draft")
            spawn.assert_not_called()

    def test_replacement_real_frames_duration_hash_and_range(self):
        record, _ = self.edits.prepare(
            self.body(mode="replace", replacement=self.b, replacement_start=1), "splice"
        )
        original = self.root / "runs" / ("task-" + self.a["task_id"]) / "video.mp4"
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        done = render(self.edits, record["id"])
        self.assertEqual(done["status"], "succeeded", done)
        result = done["result"]
        self.assertEqual(result["frames"], 81)
        self.assertEqual(result["replaced_frames"], 32)
        self.assertFalse(result["ai_generated"])
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), before)
        self.assertEqual(
            render(self.edits, record["id"])["result"]["sha256"], result["sha256"]
        )
        with patch("video_platform.api.ROOT", self.root), TestClient(
            create_app(self.tasks, deployment={"auth_required": False})
        ) as c:
            r = c.get(
                "/api/edits/" + record["id"] + "/video", headers={"Range": "bytes=0-31"}
            )
            self.assertEqual(r.status_code, 206)
            self.assertEqual(len(r.content), 32)

    def test_boundary_frame_mapping_with_distinct_replacement(self):
        # A different solid-color CPU fixture makes the temporal splice observable.
        p = self.root / "blue.mp4"
        with imageio.get_writer(p, fps=16, macro_block_size=None) as w:
            for i in range(81):
                w.append_data(np.full((64, 96, 3), (35, 60, 220), dtype=np.uint8))
        b = self.video(p, "blue fixture")
        record, _ = self.edits.prepare(
            self.body(mode="replace", replacement=b), "boundary"
        )
        done = render(self.edits, record["id"])
        self.assertEqual(done["status"], "succeeded")
        r = imageio.get_reader(
            self.root / "runs" / ("edit-" + record["id"]) / "video.mp4"
        )
        try:
            self.assertLess(r.get_data(15)[:, :, 2].mean(), 80)
            self.assertGreater(r.get_data(16)[:, :, 2].mean(), 180)
            self.assertGreater(r.get_data(47)[:, :, 2].mean(), 180)
            self.assertLess(r.get_data(48)[:, :, 2].mean(), 80)
        finally:
            r.close()

    def test_short_or_rejected_or_changed_asset_refused(self):
        short = self.body(mode="replace", replacement=self.b, replacement_start=4)
        with self.assertRaises(ValueError):
            self.edits.prepare(short, "short")
        self.tasks.review_quality(self.b["task_id"], False, "fixture rejection", {})
        with self.assertRaises(ValueError):
            self.edits.prepare(
                self.body(mode="replace", replacement=self.b), "rejected"
            )
        path = self.root / "runs" / ("task-" + self.a["task_id"]) / "video.mp4"
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.edits.prepare(self.body(), "changed")
