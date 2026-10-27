"""Authenticated customer-owned video keys; legacy operator cloud stays disabled."""

import json, re
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from .video_api_store import VideoAPIStore, crypto_ready
from .video_protocols import PROVIDERS
from .store import Conflict, QueueFull


class ConnectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["minimax", "seedance", "wan"]
    label: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=32)
    workspace: str = Field(default="", max_length=64)
    api_key: SecretStr | None = None
    revision: int = Field(default=0, ge=0, strict=True)

    def settings(self):
        return self.model_dump(exclude={"api_key", "revision"})


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connection_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    prompt: str = Field(min_length=1, max_length=2000)
    duration: int = Field(default=5, ge=2, le=15, strict=True)
    resolution: str = Field(min_length=1, max_length=10)
    ratio: str = Field(default="16:9", max_length=10)
    allow_paid_call: bool = False


def ready(root):
    try:
        r = json.loads((root / "data/video-api-worker.json").read_text())
        return (
            r["state"] == "ready"
            and (
                datetime.now(timezone.utc) - datetime.fromisoformat(r["updated_at"])
            ).total_seconds()
            < 90
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def install_video_api(app, root, web_root):
    store = VideoAPIStore(root)
    router = APIRouter(prefix="/api/video-api")

    def operation(fn):
        try:
            return fn()
        except LookupError:
            raise HTTPException(404, "记录不存在。") from None
        except Conflict as e:
            raise HTTPException(409, str(e)) from None
        except QueueFull as e:
            raise HTTPException(429, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        except (ImportError, ModuleNotFoundError):
            raise HTTPException(503, "密钥加密存储尚未就绪。") from None

    @app.get("/video-api", include_in_schema=False)
    def page():
        return FileResponse(web_root / "video-api.html", media_type="text/html")

    @router.get("/providers")
    def providers():
        return {
            "providers": [{"id": pid, **p} for pid, p in PROVIDERS.items()],
            "enabled": store.enabled() and crypto_ready(),
            "worker_ready": ready(root),
            "maximum_platform_seconds": 15,
            "credential_verification": "保存不调用收费接口，实际权限以供应商返回为准。",
        }

    @router.get("/connections")
    def connections():
        return {
            "items": store.connections(),
            "enabled": store.enabled() and crypto_ready(),
            "worker_ready": ready(root),
        }

    @router.post("/connections", status_code=201)
    def create(body: ConnectionBody):
        return operation(
            lambda: store.save(
                body.settings(),
                body.api_key.get_secret_value() if body.api_key else None,
            )
        )

    @router.put("/connections/{cid}")
    def update(cid: str, body: ConnectionBody):
        if not re.fullmatch(r"[0-9a-f]{32}", cid):
            raise HTTPException(404, "记录不存在。")
        return operation(
            lambda: store.save(
                body.settings(),
                body.api_key.get_secret_value() if body.api_key else None,
                cid,
                body.revision,
            )
        )

    @router.delete("/connections/{cid}")
    def delete(cid: str):
        operation(lambda: store.delete(cid))
        return {"deleted": True}

    @router.get("/tasks")
    def tasks(limit: int = Query(100, ge=1, le=100)):
        return store.list(limit)

    @router.post("/tasks", status_code=202)
    def create_task(
        body: VideoRequest, idempotency_key: str | None = Header(default=None)
    ):
        if not ready(root):
            raise HTTPException(
                503,
                "视频API队列尚未就绪，未提交计费请求。",
                headers={"X-Task-Accepted": "no"},
            )
        task, _ = operation(
            lambda: store.prepare(
                body.connection_id,
                body.prompt,
                body.duration,
                body.resolution,
                body.ratio,
                idempotency_key,
                body.allow_paid_call,
            )
        )
        return task

    @router.get("/tasks/{jid}")
    def detail(jid: str):
        task = store.get(jid)
        if not task:
            raise HTTPException(404, "任务不存在。")
        return task

    @router.post("/tasks/{jid}/cancel")
    def cancel(jid: str):
        operation(lambda: store.cancel(jid))
        return detail(jid)

    @router.get("/tasks/{jid}/video")
    def video(jid: str, download: bool = False):
        task = detail(jid)
        if task["status"] != "succeeded":
            raise HTTPException(409, "视频尚未保存完成。")
        path = root / "runs" / ("video-api-" + task["id"]) / "video.mp4"
        if not path.is_file():
            raise HTTPException(404, "视频文件不存在。")
        return FileResponse(
            path,
            media_type="video/mp4",
            filename="video-" + task["id"] + ".mp4",
            content_disposition_type="attachment" if download else "inline",
        )

    app.include_router(router)


def catalog(root):
    store = VideoAPIStore(root)
    return [
        {
            "id": "video-api:" + c["id"],
            "name": c["label"] + " · API",
            "provider": PROVIDERS[c["provider"]]["name"],
            "execution": "customer_video_api",
            "configured": store.enabled() and crypto_ready() and ready(root),
            "verified": False,
            "credential_configured": True,
            "credential_status": "stored_not_verified",
            "connection_id": c["id"],
            "model_id": c["model"],
            "provider_id": c["provider"],
            "specification": c["model"] + " · 自带密钥 · 最多15秒",
            "description": "使用你自己的供应商API余额，保存配置不代表已验证密钥或余额。",
            "min_seconds": max(
                PROVIDERS[c["provider"]]["min_seconds"],
                5 if c["model"] == "MiniMax-H3-Max" else 2,
            ),
            "max_seconds": 15,
            "resolutions": (
                {
                    "MiniMax-H3": ["768P", "2K"],
                    "MiniMax-H3-Highspeed": ["768P"],
                    "MiniMax-H3-Lite": ["480P", "768P"],
                    "MiniMax-H3-Max": ["480P", "768P"],
                }.get(c["model"])
                or PROVIDERS[c["provider"]]["resolutions"]
            ),
            "cost_hint": "供应商按实际调用计费；本平台不代充值，不自动从本地模型切换到收费API。",
            "unavailable_reason": "视频API服务尚未就绪",
        }
        for c in store.connections()
    ]
