"""CPU editing integration with a copy of existing footage, not a new model run."""

import hashlib, json, shutil, tempfile
from pathlib import Path
from unittest import TestCase
from video_platform.store import TaskStore
from video_platform.projects import Project, ProjectStore, VersionLink
from video_platform.composition import ExportStore, compose
from video_platform.ownership import as_user
from tests.media_fixture import synthetic_video


class CompositionTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=None)
        self.root = Path(self.temp.name)
        (self.root / "logs").mkdir()
        (self.root / "runs").mkdir()
        self.tasks = TaskStore(self.root / "data/tasks.sqlite3")
        self.projects = ProjectStore(self.root)
        self.exports = ExportStore(self.root)
        self.body = Project(
            title="CPU fixture",
            episodes=[
                dict(
                    id="1" * 32,
                    title="字幕测试",
                    target_seconds=4,
                    shots=[
                        dict(
                            id="2" * 32,
                            title="纸船",
                            description="fixture",
                            narration="这只是已有素材的剪辑测试。",
                            seconds=4,
                            model="local-wan",
                            preset="story",
                        )
                    ],
                )
            ],
        )
        self.project = self.projects.save(self.body)

    def tearDown(self):
        self.temp.cleanup()

    def selected(self):
        source = synthetic_video(self.root)
        task = self.tasks.create(
            dict(prompt="fixture", frames=81, fps=16, width=480, height=272)
        )
        self.tasks.claim()
        target = self.root / "runs" / ("task-" + task["id"])
        target.mkdir()
        shutil.copyfile(source, target / "video.mp4")
        metrics = {
            "validation": {
                "duration_seconds": 5.06,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        }
        self.tasks.finish(task["id"], "succeeded", metrics=metrics)
        task = self.tasks.get(task["id"])
        self.project = self.projects.link(
            self.project["id"],
            VersionLink(
                episode_id="1" * 32,
                shot_id="2" * 32,
                origin="local",
                task_id=task["id"],
                revision=self.project["revision"],
            ),
            task,
        )
        self.project = self.projects.choose(
            self.project["id"],
            self.project["versions"][0]["id"],
            self.project["revision"],
        )
        return target / "video.mp4"

    def test_missing_selection_and_insufficient_footage_refused(self):
        with self.assertRaises(ValueError):
            self.exports.prepare(self.project["id"], "1" * 32, self.project["revision"])
        self.selected()
        self.body.revision = self.project["revision"]
        self.body.episodes[0].shots[0].seconds = 10
        self.project = self.projects.save(self.body, self.project["id"])
        with self.assertRaises(ValueError):
            self.exports.prepare(self.project["id"], "1" * 32, self.project["revision"])
        self.assertEqual(self.exports.list(self.project["id"]), [])

    def test_cpu_export_duration_hash_subtitles_dedup_and_ownership(self):
        source = self.selected()
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        record, created = self.exports.prepare(
            self.project["id"], "1" * 32, self.project["revision"]
        )
        self.assertTrue(created)
        self.assertFalse(
            self.exports.prepare(
                self.project["id"], "1" * 32, self.project["revision"]
            )[1]
        )
        finished = compose(self.exports, record["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["result"]["duration_seconds"], 4)
        self.assertEqual(finished["result"]["frames"], 64)
        output = self.root / "runs" / ("episode-" + record["id"])
        self.assertIn("00:00:04,000", (output / "subtitles.srt").read_text())
        self.assertTrue((output / "soundtrack.wav").exists())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        with as_user({"id": "other-user", "role": "creator"}):
            self.assertIsNone(self.exports.get(record["id"]))

    def test_changed_input_rejected_without_replacing_source(self):
        source = self.selected()
        record, _ = self.exports.prepare(
            self.project["id"], "1" * 32, self.project["revision"]
        )
        source.write_bytes(b"changed fixture")
        with self.assertRaises(RuntimeError):
            compose(self.exports, record["id"])
        self.assertEqual(source.read_bytes(), b"changed fixture")
        self.assertEqual(self.exports.get(record["id"])["status"], "failed")
