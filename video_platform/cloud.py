"""Budgeted H3 cloud tasks, separate from the local GPU queue."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import tempfile
from urllib.parse import urlparse, urljoin
import uuid

import httpx
from scripts.configure_minimax import check_configuration
from .store import ROOT, now
from .ownership import owner_id, LEGACY_OWNER, add_owner_column

CAP_FEN = 1000
FIRST_FEN = 200
PRICE_DATE = "2026-10-21"
FIRST_KEY = "minimax-h3-first-cn-v1"
TOPUP_KEY = FIRST_KEY + "-user-topup-20261020"
PARAMS = {"model": "MiniMax-H3", "duration": 4, "resolution": "768P", "ratio": "16:9"}


def price_is_current():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat() == PRICE_DATE


class CloudStore:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.path = self.root / "data/cloud.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, request_key TEXT UNIQUE, prompt TEXT,
              status TEXT, stage TEXT, provider_task_id TEXT, error TEXT, result TEXT,
              reserved_fen INTEGER, created_at TEXT, updated_at TEXT)"""
            )
            db.execute("BEGIN IMMEDIATE")
            if "parameters" not in {
                row[1] for row in db.execute("PRAGMA table_info(jobs)")
            }:
                db.execute("ALTER TABLE jobs ADD COLUMN parameters TEXT")
            add_owner_column(db, "jobs")

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

    @staticmethod
    def decode(row):
        if row is None:
            return None
        data = dict(row)
        data.pop("request_key", None)
        data["result"] = json.loads(data["result"]) if data["result"] else None
        data["parameters"] = (
            json.loads(data["parameters"]) if data.get("parameters") else dict(PARAMS)
        )
        return data

    def get(self, job_id):
        with self.connect() as db:
            return self.decode(
                db.execute(
                    "SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, owner_id())
                ).fetchone()
            )

    def state(self):
        with self.connect() as db:
            reserved = db.execute(
                "SELECT coalesce(sum(reserved_fen),0) FROM jobs"
            ).fetchone()[0]
            latest = self.decode(
                db.execute(
                    "SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT 1",
                    (owner_id(),),
                ).fetchone()
            )
            history = [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT 20",
                    (owner_id(),),
                )
            ]
            first = db.execute(
                "SELECT * FROM jobs WHERE request_key=?", (FIRST_KEY,)
            ).fetchone()
            retried = db.execute(
                "SELECT 1 FROM jobs WHERE request_key=?", (TOPUP_KEY,)
            ).fetchone()
            can_retry = bool(
                first
                and not retried
                and first["status"] == "failed"
                and not first["provider_task_id"]
                and "HTTP 402" in (first["error"] or "")
            )
        return {
            "budget_cap_cny": CAP_FEN / 100,
            "reserved_cny": reserved / 100,
            "remaining_unreserved_cny": (CAP_FEN - reserved) / 100,
            "actual_billing_verified": False,
            "latest": latest,
            "history": history,
            "can_retry_after_topup": can_retry,
            "price_checked_date": PRICE_DATE,
            "pricing_current": price_is_current(),
        }

    def list(self, limit=100):
        with self.connect() as db:
            return {
                "items": [
                    self.decode(row)
                    for row in db.execute(
                        "SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                        (owner_id(), limit),
                    )
                ],
                "total": db.execute(
                    "SELECT count(*) FROM jobs WHERE owner_id=?", (owner_id(),)
                ).fetchone()[0],
            }

    def first(self, prompt, request_key=FIRST_KEY, duration=4):
        if owner_id() != LEGACY_OWNER:
            raise ValueError("当前云API预算仅向管理员开放。")
        if type(duration) is not int or not 4 <= duration <= 15:
            raise ValueError("H3时长应为4至15秒的整数。")
        parameters = {**PARAMS, "duration": duration}
        reservation = duration * 50
        if request_key not in {FIRST_KEY, TOPUP_KEY} and not re.fullmatch(
            r"interactive-[A-Za-z0-9_-]{1,80}", request_key
        ):
            raise ValueError("当前入口只允许首测及用户充值后的一次续测。")
        prompt = prompt.strip()
        if not 1 <= len(prompt) <= 2000:
            raise ValueError("请输入1至2000字描述。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM jobs WHERE request_key=?", (request_key,)
            ).fetchone()
            if old:
                if (
                    old["prompt"] != prompt
                    or self.decode(old)["parameters"] != parameters
                ):
                    raise ValueError("同一提交标识的描述或时长不同，禁止重复计费。")
                return self.decode(old), False
            from .management import require_model_enabled

            require_model_enabled(self.root, "minimax-h3")
            if db.execute(
                "SELECT 1 FROM jobs WHERE status IN ('queued','running','waiting','unknown')"
            ).fetchone():
                raise ValueError("已有未完成或状态不明的云任务，禁止新增计费请求。")
            if request_key == TOPUP_KEY:
                first = db.execute(
                    "SELECT * FROM jobs WHERE request_key=?", (FIRST_KEY,)
                ).fetchone()
                if (
                    not first
                    or first["status"] != "failed"
                    or first["provider_task_id"]
                    or "HTTP 402" not in (first["error"] or "")
                ):
                    raise ValueError("旧请求不符合402拒绝且无官方ID的续测条件。")
                db.execute(
                    "UPDATE jobs SET reserved_fen=0,stage=?,updated_at=? WHERE id=?",
                    (
                        "旧请求因402被拒绝且无任务ID；用户充值后续测，释放旧预算预留（非账单退款）",
                        now(),
                        first["id"],
                    ),
                )
            reserved = db.execute(
                "SELECT coalesce(sum(reserved_fen),0) FROM jobs"
            ).fetchone()[0]
            if reserved + reservation > CAP_FEN:
                raise ValueError("本轮10元预算预留不足。")
            job_id, stamp = uuid.uuid4().hex, now()
            db.execute(
                """INSERT INTO jobs (id,request_key,prompt,status,stage,provider_task_id,error,result,
                       reserved_fen,created_at,updated_at,parameters,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    request_key,
                    prompt,
                    "queued",
                    f"{duration}秒任务已保存，预留{reservation/100:g}元，等待执行",
                    None,
                    None,
                    None,
                    reservation,
                    stamp,
                    stamp,
                    json.dumps(parameters),
                    owner_id(),
                ),
            )
            return (
                self.decode(
                    db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                ),
                True,
            )

    def update(self, job_id, **fields):
        assert set(fields) <= {"status", "stage", "provider_task_id", "error", "result"}
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        fields["updated_at"] = now()
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET "
                + ",".join(key + "=?" for key in fields)
                + " WHERE id=?",
                [*fields.values(), job_id],
            )

    def queue_result_resume(self, job_id):
        """Only a retained provider ID can enter the query-only recovery path."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner_id=?", (job_id, owner_id())
            ).fetchone()
            if not row or not row["provider_task_id"]:
                raise ValueError(
                    "没有已保存的官方任务ID，不能恢复查询；请先核对控制台。"
                )
            if row["status"] not in {"failed", "waiting"}:
                return self.decode(row), False
            db.execute(
                "UPDATE jobs SET status='queued',stage=?,updated_at=? WHERE id=?",
                ("已有官方ID的结果查询已排队，不提交新生成请求", now(), job_id),
            )
            return (
                self.decode(
                    db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                ),
                True,
            )


class UpstreamError(RuntimeError):
    def __init__(self, status):
        self.status = status
        messages = {
            401: "鉴权失败，请核对国内站Key及权限",
            402: "账户余额不足，需要本人查看控制台",
            403: "当前账户无访问权限",
            422: "参数或内容被官方拒绝",
            429: "官方限流",
        }
        super().__init__(
            f"官方HTTP {status}：{messages.get(status, '请求未完成')}；未自动重新提交。"
        )


class MiniMax:
    def __init__(self, key):
        self.key = key

    def request(self, method, path, body=None):
        # Never redirect a bearer credential, use proxies, retry a POST or print
        # headers/raw provider error bodies (which can contain reflected input).
        with httpx.Client(
            timeout=60, follow_redirects=False, trust_env=False
        ) as client:
            response = client.request(
                method,
                "https://api.minimax.cn" + path,
                headers={"Authorization": "Bearer " + self.key},
                json=body,
            )
        if response.status_code != 200:
            raise UpstreamError(response.status_code)
        if len(response.content) > 1024**2:
            raise RuntimeError("官方响应过大；未自动重新提交。")
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("官方响应不是有效JSON；未自动重新提交。") from None

    def submit(self, prompt, duration=4):
        if type(duration) is not int or not 4 <= duration <= 15:
            raise ValueError("H3时长应为4至15秒的整数。")
        payload = {
            **PARAMS,
            "duration": duration,
            "content": [{"type": "text", "text": prompt}],
        }
        data = self.request("POST", "/v2/video_generation", payload)
        task_id = str(data.get("task_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise RuntimeError("官方未返回可识别任务ID；需要人工核对，不重新提交。")
        return task_id

    def query(self, task_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("Invalid task ID")
        data = self.request("GET", "/v2/query/video_generation/" + task_id)
        task = data.get("task")
        if not isinstance(task, dict):
            raise RuntimeError("官方查询响应缺少任务对象。")
        return task


def download_video(url, path):
    allowed = (
        "hailuoai.com",
        "minimax.cn",
        "minimax.io",
        "minimaxi.com",
        "minimax.chat",
    )
    # Observed in the authenticated successful H3 task, DNS checked public;
    # allow the exact provider bucket rather than every Aliyun OSS host.
    exact_hosts = {"algeng-video-infer.oss-cn-shanghai.aliyuncs.com"}
    with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or not (
                    host in exact_hosts
                    or any(
                        host == domain or host.endswith("." + domain)
                        for domain in allowed
                    )
                )
            ):
                raise RuntimeError("结果下载域名待核实，未访问该地址。")
            with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    raise RuntimeError(
                        f"结果下载HTTP {response.status_code}；没有重新生成。"
                    )
                size = 0
                # A disconnected download must not leave an exclusive .part
                # filename that prevents recovery of the same official result.
                part = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=path.parent,
                        prefix=".h3-",
                        suffix=".part",
                        delete=False,
                    ) as stream:
                        part = Path(stream.name)
                        for block in response.iter_bytes():
                            size += len(block)
                            if size > 128 * 1024**2:
                                raise RuntimeError("结果超过本次128MiB下载上限。")
                            stream.write(block)
                    part.replace(path)
                finally:
                    if part is not None and part.exists():
                        part.unlink()
                return
    raise RuntimeError("结果下载跳转过多。")


def validate_video(path, requested_duration=4):
    import imageio.v2 as imageio
    import numpy as np
    from PIL import Image, ImageDraw

    reader = imageio.get_reader(path, format="ffmpeg")
    meta = reader.get_meta_data()
    count, frames, shape = 0, [], None
    try:
        for frame in reader:
            if shape is None:
                shape = frame.shape
            if frame.shape != shape or not np.isfinite(frame).all():
                raise RuntimeError("视频帧结构异常。")
            if count in {
                int(meta["fps"] * requested_duration * fraction)
                for fraction in (0, 0.25, 0.5, 0.75)
            }:
                frames.append(Image.fromarray(frame))
            count += 1
    finally:
        reader.close()
    duration = count / meta["fps"]
    if count < 2 or abs(duration - requested_duration) > 0.5:
        raise RuntimeError(
            f"输出帧数或时长不符合请求的{requested_duration}秒（容差0.5秒）。"
        )
    sheet = Image.new("RGB", (640, 384), "#eef2f8")
    for index, frame in enumerate(frames):
        frame.thumbnail((320, 192))
        sheet.paste(frame, ((index % 2) * 320, (index // 2) * 192))
    sheet.save(path.parent / "contact-sheet.jpg", quality=92)
    return {
        "validation": "passed",
        "frames": count,
        "width": shape[1],
        "height": shape[0],
        "fps": meta["fps"],
        "duration_seconds": round(duration, 3),
        "requested_duration_seconds": requested_duration,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "limitation": "结构验收不证明剧情、角色或动作一致性。",
    }


def run_job(store, job_id, provider=None):
    job = store.get(job_id)
    if not job or job["status"] in {"succeeded", "failed"}:
        return
    if provider is None:
        configured = check_configuration(store.root)
        if not configured["configured"] or configured["region"] != "cn":
            raise RuntimeError("国内站凭据尚未配置。")
        credentials = json.loads((store.root / ".secrets/minimax.json").read_text())
        provider = MiniMax(credentials["api_key"])
    post_uncertain = False
    try:
        task_id = job["provider_task_id"]
        if not task_id:
            if job["status"] != "queued":
                store.update(
                    job_id,
                    status="unknown",
                    stage="此前提交结果不明，需要控制台核对，禁止自动重新提交",
                )
                return
            if not price_is_current():
                raise RuntimeError("价格核对日期已过期，需要先重新核对再执行。")
            store.update(
                job_id,
                status="running",
                stage=f"正在向MiniMax提交本条{job['parameters']['duration']}秒任务，不自动重复提交",
            )
            post_uncertain = True
            task_id = provider.submit(
                job["prompt"], duration=job["parameters"]["duration"]
            )
            store.update(
                job_id,
                provider_task_id=task_id,
                stage="已取得官方任务ID，正在等待云端生成",
            )
            post_uncertain = False
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            try:
                task = provider.query(task_id)
            except (httpx.HTTPError, UpstreamError):
                store.update(
                    job_id,
                    status="waiting",
                    stage="结果查询暂未完成，保留官方ID，可继续查询，不重新生成",
                )
                return
            state = str(task.get("status", "unknown"))
            if state == "succeeded":
                if task.get("model") != PARAMS["model"]:
                    raise RuntimeError("官方返回模型与请求不一致，需人工核对。")
                store.update(job_id, stage="云端已完成，正在下载并完整解码验收")
                output = store.root / "runs" / ("cloud-" + job_id)
                output.mkdir(exist_ok=True)
                video = output / "video.mp4"
                if not video.exists():
                    download_video(task.get("content", {}).get("url", ""), video)
                result = validate_video(
                    video, requested_duration=job["parameters"]["duration"]
                )
                usage = task.get("usage", {})
                result.update(
                    model=task["model"],
                    provider="minimax_official_api",
                    execution="cloud_api_not_self_hosted",
                    usage={
                        key: value
                        for key, value in usage.items()
                        if isinstance(value, (int, float))
                    },
                    estimated_output_cost_cny=job["reserved_fen"] / 100,
                    billing_verified=False,
                    elapsed_seconds=round(
                        (
                            datetime.now(timezone.utc)
                            - datetime.fromisoformat(job["created_at"])
                        ).total_seconds(),
                        3,
                    ),
                )
                (output / "result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2)
                )
                store.update(
                    job_id,
                    status="succeeded",
                    stage="H3真实视频已下载并通过完整解码，可播放",
                    result=result,
                    error=None,
                )
                return
            if state in {"failed", "cancelled", "canceled", "expired"}:
                raise RuntimeError("官方任务未成功，保留任务ID，不自动重新生成。")
            safe = state if re.fullmatch(r"[a-z_]{1,32}", state) else "processing"
            store.update(
                job_id,
                status="running",
                stage="官方任务状态：" + safe + "；等待结果（不虚构百分比）",
            )
            time.sleep(5)
        store.update(
            job_id,
            status="waiting",
            stage="等待超时，已保留官方ID；可继续查询，不重新提交",
        )
    except Exception as error:
        uncertain = post_uncertain and not (
            isinstance(error, UpstreamError)
            and error.status in {400, 401, 402, 403, 404, 422, 429}
        )
        message = (
            str(error)
            if isinstance(error, (UpstreamError, RuntimeError))
            else "网络或验收异常；不显示敏感响应，请核对保留的任务状态。"
        )
        store.update(
            job_id,
            status="unknown" if uncertain else "failed",
            stage=(
                "提交结果不明，禁止自动重新提交"
                if uncertain
                else "云任务未完成，未自动重试计费生成"
            ),
            error=message,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--resume-result", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.job_id):
        parser.error("Invalid job ID")
    lock = (ROOT / "logs/cloud-first.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    store = CloudStore()
    if args.resume_result:
        job = store.get(args.job_id)
        if not job or not job["provider_task_id"]:
            raise RuntimeError(
                "Result resume requires a retained official ID; no new submission allowed."
            )
        if job["status"] == "succeeded":
            return
        with (ROOT / "logs/cloud-recovery.jsonl").open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "at": now(),
                        "job_id": job["id"],
                        "old_status": job["status"],
                        "old_error": job["error"],
                        "action": "query_and_download_existing_id_only",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        store.update(
            job["id"], status="waiting", stage="恢复已有官方ID的查询和下载，不重新生成"
        )
    run_job(store, args.job_id)


if __name__ == "__main__":
    main()
