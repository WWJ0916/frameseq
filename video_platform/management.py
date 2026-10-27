"""Small operator console. Operational metadata lives beside accounts, not in browser storage."""

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import AuthStore
from .ownership import is_admin, owner_id

MODELS = {
    "local-wan": "Wan2.1 · 1.3B",
    "local-cogvideo": "CogVideoX · 2B",
    "minimax-h3": "H3云API（历史，已停用）",
    "local-h3": "MiniMax H3 · 本地实验",
}


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


class ManagementStore:
    def __init__(self, root):
        self.root = root
        self.auth = AuthStore(root)
        with self.auth.connect() as db:
            db.executescript(
                """
            CREATE TABLE IF NOT EXISTS memberships(id TEXT PRIMARY KEY,user_id TEXT UNIQUE NOT NULL REFERENCES users(id),status TEXT NOT NULL,starts_at REAL NOT NULL,expires_at REAL NOT NULL,note TEXT NOT NULL,revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,starts_at REAL NOT NULL,ends_at REAL NOT NULL,revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS model_settings(id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS management_audit(id TEXT PRIMARY KEY,actor TEXT NOT NULL,event TEXT NOT NULL,target TEXT NOT NULL,detail TEXT NOT NULL,created_at REAL NOT NULL);
            """
            )

    def audit(self, db, event, target, detail):
        db.execute(
            "INSERT INTO management_audit VALUES(?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                owner_id(),
                event,
                target,
                json.dumps(detail, ensure_ascii=False),
                time.time(),
            ),
        )

    def users(self, q="", offset=0, limit=25):
        stamp = time.time()
        with self.auth.connect() as db:
            # instr performs a literal search: wildcard characters never alter query semantics.
            where = " WHERE instr(lower(u.username||u.name),lower(?))>0 "
            total = db.execute("SELECT count(*) FROM users u" + where, (q,)).fetchone()[
                0
            ]
            rows = db.execute(
                """SELECT u.id,u.username,u.name,u.role,u.status,u.created_at,
              m.status AS member_status,m.expires_at AS member_expires_at,m.revision AS member_revision,
              CASE WHEN m.status='active' AND m.starts_at<=? AND m.expires_at>? THEN 1 ELSE 0 END AS member_active
              FROM users u LEFT JOIN memberships m ON m.user_id=u.id"""
                + where
                + " ORDER BY u.created_at,u.id LIMIT ? OFFSET ?",
                (stamp, stamp, q, limit, offset),
            ).fetchall()
        return dict(
            items=[dict(r) for r in rows], total=total, offset=offset, limit=limit
        )

    def user_status(self, uid, status):
        with self.auth.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT role,status FROM users WHERE id=?", (uid,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "账号不存在。")
            if row["role"] == "admin":
                raise HTTPException(409, "此入口不能停用管理员，防止失去管理权限。")
            db.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
            if status == "disabled":
                db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            self.audit(
                db,
                "user_status",
                uid,
                {
                    "before": row["status"],
                    "after": status,
                    "existing_jobs": "unchanged",
                },
            )
        return {"status": status}

    def membership(self, uid, body):
        with self.auth.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                raise HTTPException(404, "账号不存在。")
            old = db.execute(
                "SELECT * FROM memberships WHERE user_id=?", (uid,)
            ).fetchone()
            if (old["revision"] if old else 0) != body.revision:
                raise HTTPException(409, "会员记录已更新，请刷新。")
            stamp = time.time()
            db.execute(
                """INSERT INTO memberships VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
              status=excluded.status,starts_at=excluded.starts_at,expires_at=excluded.expires_at,note=excluded.note,revision=excluded.revision""",
                (
                    old["id"] if old else uuid.uuid4().hex,
                    uid,
                    body.status,
                    stamp,
                    stamp + body.days * 86400,
                    body.note,
                    body.revision + 1,
                ),
            )
            self.audit(
                db,
                "membership_" + body.status,
                uid,
                {
                    "days": body.days,
                    "note": body.note,
                    "source": "manual_trial_no_payment",
                    "revision": body.revision + 1,
                },
            )
        return {"revision": body.revision + 1, "payment_created": False}

    def models(self):
        with self.auth.connect() as db:
            rows = {
                r["id"]: dict(r) for r in db.execute("SELECT * FROM model_settings")
            }
        return [
            dict(
                id=k,
                name=v,
                enabled=bool(rows.get(k, {}).get("enabled", True)),
                revision=rows.get(k, {}).get("revision", 0),
            )
            for k, v in MODELS.items()
        ]

    def model_update(self, mid, body):
        if mid not in MODELS:
            raise HTTPException(404, "只能管理已接入的模型。")
        with self.auth.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM model_settings WHERE id=?", (mid,)
            ).fetchone()
            if (old["revision"] if old else 0) != body.revision:
                raise HTTPException(409, "模型设置已更新，请刷新。")
            db.execute(
                "INSERT INTO model_settings VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,revision=excluded.revision",
                (mid, int(body.enabled), body.revision + 1),
            )
            self.audit(
                db,
                "model_availability",
                mid,
                {"enabled": body.enabled, "revision": body.revision + 1},
            )
        return {"enabled": body.enabled, "revision": body.revision + 1}

    def campaigns(self, public=False):
        with self.auth.connect() as db:
            sql = "SELECT * FROM campaigns"
            rows = db.execute(
                sql
                + (
                    " WHERE status='published' AND starts_at<=? AND ends_at>?"
                    if public
                    else ""
                )
                + " ORDER BY starts_at DESC,id DESC LIMIT 100",
                (time.time(), time.time()) if public else (),
            ).fetchall()
        return [dict(r) for r in rows]

    def campaign(self, body, cid=None):
        cid = cid or uuid.uuid4().hex
        with self.auth.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT revision FROM campaigns WHERE id=?", (cid,)
            ).fetchone()
            if (old["revision"] if old else 0) != body.revision:
                raise HTTPException(409, "活动已更新，请刷新。")
            if body.ends_at <= body.starts_at:
                raise HTTPException(422, "活动结束时间必须晚于开始时间。")
            db.execute(
                "INSERT INTO campaigns VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,status=excluded.status,starts_at=excluded.starts_at,ends_at=excluded.ends_at,revision=excluded.revision",
                (
                    cid,
                    body.title,
                    body.body,
                    body.status,
                    body.starts_at,
                    body.ends_at,
                    body.revision + 1,
                ),
            )
            self.audit(db, "campaign_" + body.status, cid, body.model_dump())
        return {"id": cid, "revision": body.revision + 1}

    def overview(self):
        stamp = time.time()
        with self.auth.connect() as db:
            users = db.execute("SELECT count(*) FROM users").fetchone()[0]
            active = db.execute(
                "SELECT count(*) FROM users WHERE status='active'"
            ).fetchone()[0]
            members = db.execute(
                "SELECT count(*) FROM memberships m JOIN users u ON m.user_id=u.id WHERE m.status='active' AND m.starts_at<=? AND m.expires_at>? AND u.status='active'",
                (stamp, stamp),
            ).fetchone()[0]
            signup = dict(
                db.execute(
                    "SELECT date(created_at,'unixepoch','+8 hours'),count(*) FROM users GROUP BY 1"
                )
            )
        counts = {}
        trend = {}
        total = 0
        for name, table in [("tasks.sqlite3", "tasks"), ("cloud.sqlite3", "jobs")]:
            path = self.root / "data" / name
            if not path.exists():
                continue
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                for status, count in db.execute(
                    "SELECT status,count(*) FROM " + table + " GROUP BY status"
                ):
                    counts[status] = counts.get(status, 0) + count
                    total += count
                for day, count in db.execute(
                    "SELECT date(created_at,'+8 hours'),count(*) FROM "
                    + table
                    + " GROUP BY 1"
                ):
                    trend[day] = trend.get(day, 0) + count
        today = datetime.now(timezone(timedelta(hours=8))).date()
        days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        return dict(
            users=users,
            active_users=active,
            active_trial_members=members,
            paid_members=0,
            payment_connected=False,
            tasks=total,
            task_status=counts,
            trend=[
                dict(date=d, users=signup.get(d, 0), tasks=trend.get(d, 0))
                for d in days
            ],
            note="结构成功数量不等于画质合格数量；会员是人工体验权益，尚无支付订单。",
        )


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserStatus(StrictBody):
    status: Literal["active", "disabled"]


class MemberChange(StrictBody):
    status: Literal["active", "revoked"] = "active"
    days: int = Field(default=7, ge=1, le=365, strict=True)
    note: str = Field(min_length=1, max_length=200)
    revision: int = Field(default=0, ge=0, strict=True)


class ModelChange(StrictBody):
    enabled: bool = Field(strict=True)
    revision: int = Field(ge=0, strict=True)


class Campaign(StrictBody):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(min_length=1, max_length=1000)
    status: Literal["draft", "published", "archived"] = "draft"
    starts_at: float = Field(ge=0, le=4102444800, allow_inf_nan=False)
    ends_at: float = Field(ge=0, le=4102444800, allow_inf_nan=False)
    revision: int = Field(default=0, ge=0, strict=True)

    @field_validator("title", "body")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("内容不能为空。")
        return value.strip()


def install_management(app, root, web_root):
    store = ManagementStore(root)
    app.state.management = store

    def admin():
        if not app.state.auth_required or not is_admin():
            raise HTTPException(403, "仅已登录管理员可以访问管理后台。")

    router = APIRouter(prefix="/api/admin", dependencies=[Depends(admin)])

    @app.get("/admin", include_in_schema=False, dependencies=[Depends(admin)])
    def page():
        return FileResponse(web_root / "admin.html", media_type="text/html")

    @app.get("/api/public/campaigns")
    def announcements():
        return {"items": store.campaigns(public=True)}

    @router.get("/overview")
    def overview():
        return store.overview()

    @router.get("/users")
    def users(
        q: str = Query("", max_length=60),
        offset: int = Query(0, ge=0),
        limit: int = Query(25, ge=1, le=100),
    ):
        return store.users(q, offset, limit)

    @router.patch("/users/{uid}/status")
    def status(uid: str, body: UserStatus):
        return store.user_status(uid, body.status)

    @router.put("/users/{uid}/membership")
    def membership(uid: str, body: MemberChange):
        return store.membership(uid, body)

    @router.get("/models")
    def models():
        return {"items": store.models()}

    @router.put("/models/{mid}")
    def model(mid: str, body: ModelChange):
        return store.model_update(mid, body)

    @router.get("/campaigns")
    def campaigns():
        return {"items": store.campaigns()}

    @router.post("/campaigns", status_code=201)
    def new_campaign(body: Campaign):
        return store.campaign(body)

    @router.put("/campaigns/{cid}")
    def campaign(cid: str, body: Campaign):
        with store.auth.connect() as db:
            if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (cid,)).fetchone():
                raise HTTPException(404, "活动不存在。")
        return store.campaign(body, cid)

    @router.get("/audit")
    def audit(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        with store.auth.connect() as db:
            rows = db.execute(
                "SELECT a.*,u.username AS actor_name FROM management_audit a LEFT JOIN users u ON a.actor=u.id ORDER BY a.created_at DESC,a.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = db.execute("SELECT count(*) FROM management_audit").fetchone()[0]
        return dict(items=[dict(r) for r in rows], total=total, offset=offset)

    app.include_router(router)
