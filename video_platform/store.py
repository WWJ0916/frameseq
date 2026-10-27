"""A small durable FIFO queue. Each operation uses a short SQLite transaction."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from .ownership import owner_id, LEGACY_OWNER, add_owner_column

from .config import ROOT

DB_PATH = ROOT / "data/tasks.sqlite3"


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class QueueFull(ValueError):
    pass


class TaskStore:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            # DELETE journal works on the shared filesystem without WAL's
            # shared-memory assumption. Keep write transactions short.
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    parameters TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT, progress INTEGER NOT NULL DEFAULT 0,
                    error TEXT, metrics TEXT, idempotency_key TEXT UNIQUE
                );
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status, created_at, id);
            """
            )
            db.execute("BEGIN IMMEDIATE")
            add_owner_column(db, "tasks")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        task = dict(row)
        task["parameters"] = json.loads(task["parameters"])
        task["metrics"] = json.loads(task["metrics"]) if task["metrics"] else None
        task.pop("idempotency_key", None)
        return task

    def create(self, parameters, key=None):
        owner = owner_id()
        if key and owner != LEGACY_OWNER:
            key = owner + ":" + key
        payload = json.dumps(parameters, sort_keys=True, ensure_ascii=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                old = db.execute(
                    "SELECT * FROM tasks WHERE idempotency_key=? AND owner_id=?",
                    (key, owner),
                ).fetchone()
                if old:
                    if old["parameters"] != payload:
                        raise Conflict("同一个提交标识不能用于不同参数。")
                    return self.decode(old)
            from .management import require_model_enabled, local_daily_limit

            mid = (
                "local-h3"
                if parameters.get("model_id") == "MiniMax-AI/MiniMax-H3"
                else (
                    "local-cogvideo"
                    if "cogvideo" in parameters.get("model_id", "").lower()
                    else "local-wan"
                )
            )
            require_model_enabled(self.path.parent.parent, mid)
            active = db.execute(
                "SELECT count(*) FROM tasks WHERE status IN ('queued','running')"
            ).fetchone()[0]
            if active >= 10:
                raise QueueFull("当前最多接受10个未完成任务，请等待后再提交。")
            if owner != LEGACY_OWNER:
                if (
                    db.execute(
                        "SELECT count(*) FROM tasks WHERE owner_id=? AND status IN ('queued','running')",
                        (owner,),
                    ).fetchone()[0]
                    >= 2
                ):
                    raise QueueFull("每个预览账号最多2个未完成任务。")
                maximum = local_daily_limit(self.path.parent.parent, owner)
                if (
                    db.execute(
                        "SELECT count(*) FROM tasks WHERE owner_id=? AND date(created_at,'+8 hours')=date(?,'+8 hours')",
                        (owner, now()),
                    ).fetchone()[0]
                    >= maximum
                ):
                    raise QueueFull(
                        f"当前账号每日最多{maximum}条本地任务（北京时间）。"
                    )
            task_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO tasks(id,status,parameters,created_at,idempotency_key,owner_id) VALUES(?,?,?,?,?,?)",
                (task_id, "queued", payload, now(), key, owner),
            )
            return self.decode(
                db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? AND owner_id=?", (task_id, owner_id())
            ).fetchone()
            task = self.decode(row)
            if task:
                task["queue_position"] = (
                    db.execute(
                        "SELECT count(*) FROM tasks WHERE status='queued' AND (created_at,id)<=(?,?)",
                        (task["created_at"], task["id"]),
                    ).fetchone()[0]
                    if task["status"] == "queued"
                    else None
                )
            return task

    def list(self, limit=30, offset=0):
        with self.connect() as db:
            total = db.execute(
                "SELECT count(*) FROM tasks WHERE owner_id=?", (owner_id(),)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM tasks WHERE owner_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (owner_id(), limit, offset),
            ).fetchall()
            return {
                "items": [self.decode(r) for r in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Even a second coordinator cannot claim a different task while
            # another is running. The worker also holds an OS process lock.
            if db.execute("SELECT 1 FROM tasks WHERE status='running'").fetchone():
                return None
            row = db.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE tasks SET status='running',started_at=coalesce(started_at,?) WHERE id=?",
                (now(), row["id"]),
            )
            return self.decode(
                db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            )

    def running(self):
        with self.connect() as db:
            return [
                self.decode(r)
                for r in db.execute("SELECT * FROM tasks WHERE status='running'")
            ]

    def defer_decode(self, task_id):
        """Internal coordinator operation: preserve an H3 sampling checkpoint's row."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if (
                not row
                or row["status"] not in ("running", "failed")
                or row["progress"] < 99
                or json.loads(row["parameters"]).get("model_id")
                != "MiniMax-AI/MiniMax-H3"
            ):
                raise ValueError("Only a completed H3 sample can wait for decode")
            if (
                row["status"] == "failed"
                and db.execute(
                    "SELECT count(*) FROM tasks WHERE status IN ('queued','running')"
                ).fetchone()[0]
                >= 10
            ):
                raise QueueFull("队列已满，请保留采样检查点稍后恢复。")
            db.execute(
                "UPDATE tasks SET status='queued',error=NULL,finished_at=NULL WHERE id=?",
                (task_id,),
            )

    def has_queued(self):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM tasks WHERE status='queued' LIMIT 1"
                ).fetchone()
                is not None
            )

    def progress(self, task_id, completed):
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET progress=max(progress,?) WHERE id=? AND status='running'",
                (completed, task_id),
            )

    def finish(self, task_id, status, error=None, metrics=None):
        if status not in {"succeeded", "failed"}:
            raise ValueError("Invalid terminal status")
        with self.connect() as db:
            db.execute(
                """UPDATE tasks SET status=?,finished_at=?,error=?,metrics=?,
                       progress=CASE WHEN ?='succeeded' THEN 100 ELSE progress END
                       WHERE id=? AND status='running'""",
                (
                    status,
                    now(),
                    error,
                    json.dumps(metrics) if metrics else None,
                    status,
                    task_id,
                ),
            )

    def cancel(self, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? AND owner_id=?", (task_id, owner_id())
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "queued":
                raise Conflict("只支持取消尚未开始的排队任务。")
            db.execute(
                "UPDATE tasks SET status='cancelled',finished_at=? WHERE id=?",
                (now(), task_id),
            )
            return self.decode(
                db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )

    def review_quality(self, task_id, approved, reason, checks=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? AND owner_id=?", (task_id, owner_id())
            ).fetchone()
            if not row or row["status"] != "succeeded":
                raise ValueError("只能复核本人已有结构验收结果。")
            metrics = json.loads(row["metrics"])
            metrics["visual_review"] = {
                "approved": bool(approved),
                "reason": reason,
                "checks": checks or {},
                "reviewed_at": now(),
            }
            db.execute(
                "UPDATE tasks SET metrics=? WHERE id=?",
                (json.dumps(metrics, ensure_ascii=False), task_id),
            )
            return self.decode(
                db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            )
