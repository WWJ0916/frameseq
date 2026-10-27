"""Standard-library task admission rules, independent of the HTTP/admin framework."""

import sqlite3
import time


def model_enabled(root, model):
    path = root / "data/auth.sqlite3"
    if not path.exists():
        return True
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='model_settings'"
        ).fetchone():
            return True
        row = db.execute(
            "SELECT enabled FROM model_settings WHERE id=?", (model,)
        ).fetchone()
    return row is None or bool(row[0])


def require_model_enabled(root, model):
    if not model_enabled(root, model):
        raise ValueError("此模型已由管理员暂停新任务，已有作品保留。")


def local_daily_limit(root, uid):
    path = root / "data/auth.sqlite3"
    if not path.exists():
        return 10
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memberships'"
        ).fetchone():
            return 10
        row = db.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND status='active' AND starts_at<=? AND expires_at>?",
            (uid, time.time(), time.time()),
        ).fetchone()
    return 20 if row else 10
