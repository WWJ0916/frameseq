"""Series planning with stable shot IDs and immutable links to real task versions."""
import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .store import ROOT, now
from .storyboard import RevisionConflict
from .ownership import owner_id,add_owner_column

ID = r"^[0-9a-f]{32}$"


class ProjectShot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    narration: str = Field(default="", max_length=500)
    seconds: int = Field(default=4, ge=4, le=15, strict=True)
    model: str = Field(default="local-h3",pattern=r'^(?:minimax-h3|local-wan|local-cogvideo|local-h3|video-api:[0-9a-f]{32})$')
    preset: Literal["fast", "preview", "story", "cinema"] = "fast"


class Episode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ID)
    title: str = Field(min_length=1, max_length=120)
    synopsis: str = Field(default="", max_length=2000)
    target_seconds: int = Field(default=60, ge=4, le=1800, strict=True)
    shots: list[ProjectShot] = Field(default_factory=list, max_length=60)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    synopsis: str = Field(default="", max_length=3000)
    style: str = Field(default="", max_length=2000)
    characters: str = Field(default="", max_length=3000)
    episodes: list[Episode] = Field(default_factory=list, max_length=50)
    revision: int = Field(default=0, ge=0, strict=True)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("项目名称不能为空。")
        return value.strip()

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [e.id for e in self.episodes] + [s.id for e in self.episodes for s in e.shots]
        if len(ids) != len(set(ids)):
            raise ValueError("剧集和镜头编号不能重复。")
        if any(not e.title.strip() or any(not s.title.strip() for s in e.shots) for e in self.episodes):
            raise ValueError("剧集和镜头标题不能为空。")
        return self


class VersionLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    episode_id: str = Field(pattern=ID)
    shot_id: str = Field(pattern=ID)
    origin: Literal["cloud", "local", "api"]
    task_id: str = Field(pattern=ID)
    note: str = Field(default="", max_length=300)
    revision: int = Field(ge=1, strict=True)


class VersionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)


class ProjectStore:
    def __init__(self, root=ROOT):
        self.path = Path(root) / "data/projects.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, content TEXT NOT NULL, revision INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS versions (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL, episode_id TEXT NOT NULL, shot_id TEXT NOT NULL,
              origin TEXT NOT NULL, task_id TEXT NOT NULL, snapshot TEXT NOT NULL, note TEXT NOT NULL,
              selected INTEGER NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(project_id,episode_id,shot_id,origin,task_id))""")
            db.execute('BEGIN IMMEDIATE')
            add_owner_column(db,'projects')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def decode(self, db, row):
        if row is None:
            return None
        result = {**json.loads(row["content"]), **{k: row[k] for k in ("id", "revision", "created_at", "updated_at")}}
        versions = []
        for version in db.execute("SELECT * FROM versions WHERE project_id=? ORDER BY created_at,id", (row["id"],)):
            version = dict(version)
            version["snapshot"] = json.loads(version["snapshot"])
            version["selected"] = bool(version["selected"])
            versions.append(version)
        result["versions"] = versions
        return result

    def get(self, project_id):
        with self.connect() as db:
            return self.decode(db, db.execute("SELECT * FROM projects WHERE id=? AND owner_id=?", (project_id,owner_id())).fetchone())

    def list(self):
        with self.connect() as db:
            return [self.decode(db, row) for row in db.execute("SELECT * FROM projects WHERE owner_id=? ORDER BY updated_at DESC,id",(owner_id(),))]

    @staticmethod
    def require_revision(db, project_id, revision):
        row = db.execute("SELECT * FROM projects WHERE id=? AND owner_id=?", (project_id,owner_id())).fetchone()
        if row is None:
            raise LookupError("项目不存在。")
        if row["revision"] != revision:
            raise RevisionConflict("项目已在另一页面更新，请重新载入后编辑；当前输入未被覆盖。")
        return json.loads(row["content"])

    def save(self, body, project_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            content = body.model_dump(exclude={"revision"})
            stamp = now()
            if project_id is None:
                if body.revision != 0:
                    raise RevisionConflict("新项目的版本应为0。")
                if db.execute("SELECT count(*) FROM projects WHERE owner_id=?",(owner_id(),)).fetchone()[0] >= 30:
                    raise ValueError("当前学习空间最多30个项目。")
                project_id = uuid.uuid4().hex
                db.execute("INSERT INTO projects (id,content,revision,created_at,updated_at,owner_id) VALUES(?,?,1,?,?,?)", (project_id, json.dumps(content, ensure_ascii=False), stamp, stamp,owner_id()))
            else:
                self.require_revision(db, project_id, body.revision)
                pairs = {(e.id, s.id) for e in body.episodes for s in e.shots}
                linked = db.execute("SELECT episode_id,shot_id FROM versions WHERE project_id=?", (project_id,)).fetchall()
                if any((r[0], r[1]) not in pairs for r in linked):
                    raise ValueError("含生成版本的分镜不能移除；请保留镜头及其历史。")
                db.execute("UPDATE projects SET content=?,revision=revision+1,updated_at=? WHERE id=?",
                           (json.dumps(content, ensure_ascii=False), stamp, project_id))
            return self.decode(db, db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def link(self, project_id, body, task):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute('SELECT 1 FROM projects WHERE id=? AND owner_id=?',(project_id,owner_id())).fetchone():
                raise LookupError('项目不存在。')
            # A network retry of an already linked task is harmless even after revision changes.
            old = db.execute("""SELECT id FROM versions WHERE project_id=? AND episode_id=? AND shot_id=?
                             AND origin=? AND task_id=?""", (project_id, body.episode_id, body.shot_id, body.origin, body.task_id)).fetchone()
            if old:
                return self.decode(db, db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
            content = self.require_revision(db, project_id, body.revision)
            episode = next((e for e in content["episodes"] if e["id"] == body.episode_id), None)
            shot = next((s for s in episode["shots"] if s["id"] == body.shot_id), None) if episode else None
            if shot is None:
                raise LookupError("所属剧集或分镜不存在。")
            snapshot = {"parameters": task["parameters"], "shot": shot, "task_created_at": task["created_at"]}
            db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,0,?)", (
                uuid.uuid4().hex, project_id, body.episode_id, body.shot_id, body.origin, body.task_id,
                json.dumps(snapshot, ensure_ascii=False), body.note, now()))
            db.execute("UPDATE projects SET revision=revision+1,updated_at=? WHERE id=?", (now(), project_id))
            return self.decode(db, db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def choose(self, project_id, version_id, revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.require_revision(db, project_id, revision)
            version = db.execute("SELECT * FROM versions WHERE project_id=? AND id=?", (project_id, version_id)).fetchone()
            if version is None:
                raise LookupError("生成版本不存在。")
            db.execute("UPDATE versions SET selected=0 WHERE project_id=? AND episode_id=? AND shot_id=?",
                       (project_id, version["episode_id"], version["shot_id"]))
            db.execute("UPDATE versions SET selected=1 WHERE id=?", (version_id,))
            db.execute("UPDATE projects SET revision=revision+1,updated_at=? WHERE id=?", (now(), project_id))
            return self.decode(db, db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
