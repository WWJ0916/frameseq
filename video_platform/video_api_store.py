"""Account-scoped video connections and durable, non-reposting cloud jobs."""

import base64, json, os, re, secrets, sqlite3, time, uuid
from contextlib import contextmanager
from pathlib import Path
from .ownership import owner_id
from .store import Conflict, QueueFull, now
from .video_protocols import PROVIDERS, parameters, validate_connection

ACTIVE = ("queued", "submitting", "waiting", "downloading", "unknown")


def crypto_ready():
    try:
        from Crypto.Cipher import AES

        return True
    except ImportError:
        return False


class VideoAPIStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "data/video-api.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("视频API数据库路径不符合要求。")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS connections(
    id TEXT PRIMARY KEY,owner_id TEXT,settings TEXT,sealed_key TEXT,revision INTEGER,updated_at TEXT);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner_id TEXT,connection_id TEXT,request_key TEXT,
    fingerprint TEXT,settings TEXT,parameters TEXT,sealed_key TEXT,status TEXT,stage TEXT,provider_task_id TEXT,
    error TEXT,result TEXT,created_at TEXT,updated_at TEXT,next_poll REAL DEFAULT 0,UNIQUE(owner_id,request_key));
    CREATE INDEX IF NOT EXISTS video_jobs_queue ON jobs(status,next_poll,updated_at);"""
            )

    def enabled(self):
        try:
            return (
                json.loads((self.root / "data/video-api-policy.json").read_text()).get(
                    "enabled"
                )
                is True
            )
        except (OSError, ValueError):
            return False

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def master(self):
        directory = self.root / ".secrets"
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "video-api-master.key"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
        if path.is_symlink() or (os.name != "nt" and path.stat().st_mode & 0o077):
            raise ValueError("密钥文件权限不符合要求。")
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("密钥存储未就绪。")
        return key

    def seal(self, key, uid, identifier):
        from Crypto.Cipher import AES

        cipher = AES.new(self.master(), AES.MODE_GCM)
        cipher.update((uid + ":" + identifier).encode())
        data, tag = cipher.encrypt_and_digest(key.encode())
        return base64.b64encode(cipher.nonce + tag + data).decode()

    def unseal(self, data, uid, identifier):
        from Crypto.Cipher import AES

        raw = base64.b64decode(data)
        cipher = AES.new(self.master(), AES.MODE_GCM, nonce=raw[:16])
        cipher.update((uid + ":" + identifier).encode())
        return cipher.decrypt_and_verify(raw[32:], raw[16:32]).decode()

    @staticmethod
    def connection(row):
        return {
            "id": row["id"],
            **json.loads(row["settings"]),
            "revision": row["revision"],
            "key_present": bool(row["sealed_key"]),
            "credential_status": "stored_not_verified",
            "updated_at": row["updated_at"],
        }

    def connections(self):
        with self.connect() as db:
            return [
                self.connection(r)
                for r in db.execute(
                    "SELECT * FROM connections WHERE owner_id=? ORDER BY updated_at,id",
                    (owner_id(),),
                )
            ]

    def save(self, settings, key=None, identifier=None, revision=0):
        validate_connection(settings)
        uid = owner_id()
        updating = identifier is not None
        identifier = identifier or uuid.uuid4().hex
        if key is not None and (
            not 8 <= len(key) <= 512 or any(c.isspace() for c in key)
        ):
            raise ValueError("API密钥格式无效。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM connections WHERE id=? AND owner_id=?", (identifier, uid)
            ).fetchone()
            if old is None and (updating or revision):
                raise LookupError("连接不存在。")
            if old and old["revision"] != revision:
                raise Conflict("连接已被修改，请刷新。")
            if (
                old is None
                and db.execute(
                    "SELECT count(*) FROM connections WHERE owner_id=?", (uid,)
                ).fetchone()[0]
                >= 8
            ):
                raise QueueFull("每个账号最多8个视频API连接。")
            if key is None:
                if not old:
                    raise ValueError("首次保存必须填写API密钥。")
                key = self.unseal(old["sealed_key"], uid, identifier)
            sealed = self.seal(key, uid, identifier)
            db.execute(
                "INSERT OR REPLACE INTO connections VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    uid,
                    json.dumps(settings, ensure_ascii=False),
                    sealed,
                    revision + 1,
                    now(),
                ),
            )
            return self.connection(
                db.execute(
                    "SELECT * FROM connections WHERE id=?", (identifier,)
                ).fetchone()
            )

    def delete(self, identifier):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute(
                "SELECT 1 FROM connections WHERE id=? AND owner_id=?",
                (identifier, owner_id()),
            ).fetchone():
                raise LookupError("连接不存在。")
            db.execute(
                "UPDATE jobs SET status='cancelled',sealed_key=NULL,stage='连接已删除，未提交的任务已取消',updated_at=? WHERE owner_id=? AND connection_id=? AND status='queued'",
                (now(), owner_id(), identifier),
            )
            db.execute(
                "DELETE FROM connections WHERE id=? AND owner_id=?",
                (identifier, owner_id()),
            )

    def prepare(self, cid, prompt, duration, resolution, ratio, key, confirmed=False):
        if not confirmed:
            raise ValueError("请确认使用自己的供应商API余额。")
        if not self.enabled() or not crypto_ready():
            raise ValueError("视频API执行服务尚未启用。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key or ""):
            raise ValueError("请保留有效提交标识。")
        uid = owner_id()
        prompt = prompt.strip()
        if not 1 <= len(prompt) <= 2000:
            raise ValueError("请输入1至2000字描述。")
        fingerprint = json.dumps(
            [cid, prompt, duration, resolution, ratio], ensure_ascii=False
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM jobs WHERE owner_id=? AND request_key=?", (uid, key)
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("同一提交标识不能用于不同参数。")
                return self.job(old), False
            c = db.execute(
                "SELECT * FROM connections WHERE id=? AND owner_id=?", (cid, uid)
            ).fetchone()
            if not c:
                raise LookupError("连接不存在。")
            settings = json.loads(c["settings"])
            p = parameters(settings, prompt, duration, resolution, ratio)
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','submitting','waiting','downloading','unknown')"
                ).fetchone()[0]
                >= 20
            ):
                raise QueueFull("API队列已满，请稍后提交。")
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE owner_id=? AND status IN ('queued','submitting','waiting','downloading','unknown')",
                    (uid,),
                ).fetchone()[0]
                >= 2
            ):
                raise QueueFull("每账号最多2个未完成的API任务。")
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE owner_id=? AND date(created_at,'+8 hours')=date(?,'+8 hours')",
                    (uid, now()),
                ).fetchone()[0]
                >= 10
            ):
                raise QueueFull("每账号每日最多10条API任务，状态不明的提交也计入。")
            jid = uuid.uuid4().hex
            sealed = self.seal(self.unseal(c["sealed_key"], uid, cid), uid, jid)
            stamp = now()
            db.execute(
                "INSERT INTO jobs(id,owner_id,connection_id,request_key,fingerprint,settings,parameters,sealed_key,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    jid,
                    uid,
                    cid,
                    key,
                    fingerprint,
                    json.dumps(settings, ensure_ascii=False),
                    json.dumps(p, ensure_ascii=False),
                    sealed,
                    "queued",
                    "等待使用你的API密钥提交",
                    stamp,
                    stamp,
                ),
            )
            return (
                self.job(
                    db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                ),
                True,
            )

    @staticmethod
    def job(row):
        p = json.loads(row["parameters"])
        settings = json.loads(row["settings"])
        return {
            "id": row["id"],
            "status": row["status"],
            "stage": row["stage"],
            "provider_task_id": row["provider_task_id"],
            "parameters": p,
            "connection_id": row["connection_id"],
            "model": p["model_id"],
            "origin": "api",
            "provider": PROVIDERS[settings["provider"]]["name"] + " · 自带密钥API",
            "metrics": json.loads(row["result"]) if row["result"] else {},
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "video_url": (
                f"/api/video-api/tasks/{row['id']}/video"
                if row["status"] == "succeeded"
                else None
            ),
            "preview_url": None,
            "progress": 0 if row["status"] != "succeeded" else 100,
        }

    def get(self, jid):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner_id=?", (jid, owner_id())
            ).fetchone()
            return self.job(row) if row else None

    def list(self, limit=100):
        with self.connect() as db:
            return {
                "items": [
                    self.job(r)
                    for r in db.execute(
                        "SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
                        (owner_id(), limit),
                    )
                ],
                "total": db.execute(
                    "SELECT count(*) FROM jobs WHERE owner_id=?", (owner_id(),)
                ).fetchone()[0],
            }

    def cancel(self, jid):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner_id=?", (jid, owner_id())
            ).fetchone()
            if not row:
                raise LookupError("任务不存在。")
            if row["status"] != "queued":
                raise Conflict("任务已提交，不能在本地假装取消；请到供应商控制台核对。")
            db.execute(
                "UPDATE jobs SET status='cancelled',sealed_key=NULL,stage='已在提交前取消',updated_at=? WHERE id=? AND status='queued'",
                (now(), jid),
            )

    def recover(self):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='unknown',error='执行中断且未保留官方ID，请核对供应商控制台，禁止自动重新提交',updated_at=? WHERE status='submitting' AND provider_task_id IS NULL",
                (now(),),
            )
            db.execute(
                "UPDATE jobs SET status='waiting',next_poll=0 WHERE status='downloading' AND provider_task_id IS NOT NULL"
            )

    def next(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','waiting','downloading') AND next_poll<=? ORDER BY updated_at,id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not r:
                return None
            state = (
                "submitting"
                if r["status"] == "queued"
                else "downloading" if r["status"] == "downloading" else "waiting"
            )
            db.execute(
                "UPDATE jobs SET status=?,next_poll=?,updated_at=? WHERE id=?",
                (state, time.time() + 60, now(), r["id"]),
            )
            return dict(r)

    def update(self, jid, **values):
        assert set(values) <= set(
            "status stage provider_task_id error result next_poll sealed_key".split()
        )
        if "result" in values:
            values["result"] = json.dumps(values["result"], ensure_ascii=False)
        if values.get("status") in ("succeeded", "failed", "cancelled"):
            values["sealed_key"] = None
        values["updated_at"] = now()
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                (*values.values(), jid),
            )
