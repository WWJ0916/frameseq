"""Separate CPU-only video API scheduler; local GPU queue is untouched."""

import argparse, fcntl, json, time
from datetime import datetime, timezone
from pathlib import Path
from .config import ROOT
from .store import now
from .video_api_store import VideoAPIStore
from .video_protocols import VideoProvider, ProviderError, download_result


def process_one(
    store, provider_factory=VideoProvider, download=download_result, validate=None
):
    row = store.next()
    if not row:
        return False
    jid = row["id"]
    uid = row["owner_id"]
    submitted = row["status"] == "queued"
    try:
        settings = json.loads(row["settings"])
        p = json.loads(row["parameters"])
        provider = provider_factory(settings, store.unseal(row["sealed_key"], uid, jid))
        if submitted:
            tid = provider.submit(p)
            store.update(
                jid,
                status="waiting",
                stage="已取得供应商任务ID，等待生成",
                provider_task_id=tid,
                next_poll=time.time() + 10,
                error=None,
            )
            return True
        if not row["provider_task_id"]:
            store.update(jid, status="unknown", stage="提交状态不明，禁止自动重新提交")
            return True
        if (
            datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])
        ).total_seconds() > 24 * 3600:
            store.update(
                jid,
                status="failed",
                stage="任务查询已超过24小时，请核对供应商控制台",
                error="保留官方任务ID，未重新生成。",
            )
            return True
        state = provider.query(row["provider_task_id"])
        if state["status"] == "succeeded":
            directory = store.root / "runs" / ("video-api-" + jid)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            output = directory / "video.mp4"
            store.update(
                jid, status="downloading", stage="云端已完成，正在保存和检查视频"
            )
            if not output.exists():
                download(state["url"], output, settings["provider"])
            if validate is None:
                from .cloud import validate_video

                check = validate_video
            else:
                check = validate
            result = check(output, requested_duration=p["duration"])
            result.update(
                execution="customer_video_api",
                provider=settings["provider"],
                model=p["model_id"],
                model_id=p["model_id"],
                billing_verified=False,
                elapsed_seconds=(
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(row["created_at"])
                ).total_seconds(),
            )
            store.update(
                jid,
                status="succeeded",
                stage="API视频已保存，可播放下载",
                result=result,
                error=None,
            )
        elif state["status"] in ("failed", "cancelled", "expired", "unknown"):
            store.update(
                jid,
                status="failed",
                stage="供应商任务未成功，请核对官方控制台",
                error="未自动重新生成。",
            )
        else:
            store.update(
                jid,
                status="waiting",
                stage="供应商正在生成；本地模型仍可独立使用",
                next_poll=time.time() + 15,
                error=None,
            )
    except ProviderError as error:
        status = ("failed" if error.definite else "unknown") if submitted else "waiting"
        store.update(
            jid,
            status=status,
            stage=(
                "提交结果需核对，禁止自动重新提交"
                if submitted
                else "查询或下载暂不可用，保留官方ID"
            ),
            error=str(error),
            next_poll=time.time() + 30,
        )
    except Exception:
        store.update(
            jid,
            status="unknown" if submitted else "waiting",
            stage="执行暂未完成，保留任务，不重复生成",
            error="请核对密钥存储、供应商状态或结果文件。",
            next_poll=time.time() + 60,
        )
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    (ROOT / "logs").mkdir(exist_ok=True)
    lock = (ROOT / "logs/video-api-worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = VideoAPIStore(ROOT)
    store.recover()
    while True:
        enabled = store.enabled()
        path = ROOT / "data/video-api-worker.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "state": "ready" if enabled else "disabled",
                    "updated_at": now(),
                    "pid": __import__("os").getpid(),
                    "uses_gpu": False,
                }
            )
        )
        tmp.replace(path)
        if enabled:
            process_one(store)
        if args.once:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
