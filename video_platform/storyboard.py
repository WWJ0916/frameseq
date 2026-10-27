"""Editable planning records only. No provider or model execution imports."""

import json
from pathlib import Path
import re
import sqlite3
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator
from .store import ROOT, now
from .ownership import owner_id, add_owner_column


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=2000)
    narration: str = Field(default="", max_length=500)
    seconds: int = Field(default=10, ge=4, le=15, strict=True)

    @field_validator("description")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("镜头描述不能为空。")
        return value.strip()


class Storyboard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    synopsis: str = Field(default="", max_length=2000)
    shots: list[Shot] = Field(min_length=1, max_length=15)
    revision: int = Field(default=0, ge=0, strict=True)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请输入草稿标题。")
        return value.strip()


class RevisionConflict(ValueError):
    pass


class StoryboardStore:
    def __init__(self, root=ROOT):
        self.path = Path(root) / "data/storyboards.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS boards (
              id TEXT PRIMARY KEY, content TEXT NOT NULL, revision INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
            )
            db.execute("BEGIN IMMEDIATE")
            add_owner_column(db, "boards")

    @staticmethod
    def decode(row):
        if row is None:
            return None
        content = json.loads(row["content"])
        total = sum(shot["seconds"] for shot in content["shots"])
        return {
            **content,
            "id": row["id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": "draft_only",
            "total_seconds": total,
            "h3_output_estimate_cny": total * 0.5,
            "price_checked_date": "2026-10-20",
            "execution_supported": False,
            "note": "仅保存分镜草稿，未生成。估价为H3 768P纯输出，不含重做、参考输入、声音与剪辑。",
        }

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def list(self):
        db = self.connect()
        try:
            return [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM boards WHERE owner_id=? ORDER BY updated_at DESC",
                    (owner_id(),),
                )
            ]
        finally:
            db.close()

    def get(self, board_id):
        if not re.fullmatch(r"[0-9a-f]{32}", board_id):
            return None
        db = self.connect()
        try:
            return self.decode(
                db.execute(
                    "SELECT * FROM boards WHERE id=? AND owner_id=?",
                    (board_id, owner_id()),
                ).fetchone()
            )
        finally:
            db.close()

    def save(self, board, board_id=None):
        db = self.connect()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                content = json.dumps(
                    board.model_dump(exclude={"revision"}), ensure_ascii=False
                )
                stamp = now()
                if board_id is None:
                    if board.revision != 0:
                        raise RevisionConflict("新草稿的版本应为0。")
                    if (
                        db.execute(
                            "SELECT count(*) FROM boards WHERE owner_id=?",
                            (owner_id(),),
                        ).fetchone()[0]
                        >= 30
                    ):
                        raise ValueError("当前学习空间最多保存30份草稿。")
                    board_id = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO boards(id,content,revision,created_at,updated_at,owner_id) VALUES(?,?,1,?,?,?)",
                        (board_id, content, stamp, stamp, owner_id()),
                    )
                else:
                    old = self.get(board_id)
                    if old is None:
                        raise LookupError("草稿不存在。")
                    if old["revision"] != board.revision:
                        raise RevisionConflict(
                            "草稿已在其他页面更新，请重新打开后编辑，避免覆盖。"
                        )
                    db.execute(
                        "UPDATE boards SET content=?,revision=revision+1,updated_at=? WHERE id=?",
                        (content, stamp, board_id),
                    )
                result = self.decode(
                    db.execute(
                        "SELECT * FROM boards WHERE id=?", (board_id,)
                    ).fetchone()
                )
            return result
        finally:
            db.close()
