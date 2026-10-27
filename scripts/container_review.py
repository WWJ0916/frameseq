"""Independent CPU review instance. Never launch inference or provider requests."""

import argparse
from contextlib import asynccontextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
MARKER = ".frameseq-review.json"
NOTICE = """<aside role="note" style="position:sticky;bottom:0;z-index:9999;background:#17382c;color:#fff;padding:12px 20px;font:14px/1.6 sans-serif;text-align:center">
Docker / CPU 审阅版 · 可以保存项目与分镜、管理本机测试账号；模型生成和视频导出已关闭，不会调用收费 API。
<a href="/review/" style="color:#c8f280;margin-left:12px">观看已有真实样例 ↗</a></aside>"""


def initialize(root):
    """Bootstrap only a new, explicitly marked review volume; preserve restarts."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".review-init.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = root / MARKER
        if not marker.exists():
            if any(
                (root / p).exists()
                for p in ("data/auth.sqlite3", ".secrets/deployment.json")
            ):
                raise RuntimeError(
                    "Refusing to convert existing application data into a review volume."
                )
            marker.write_text(
                json.dumps({"mode": "cpu_review", "generation": False}),
                encoding="utf-8",
            )
        elif json.loads(marker.read_text(encoding="utf-8")).get("mode") != "cpu_review":
            raise RuntimeError("Not a FrameSeq review volume.")
        env = dict(
            os.environ, VIDEO_PLATFORM_ROOT=str(root), PYTHONDONTWRITEBYTECODE="1"
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.bootstrap_accounts",
                "--enable",
                "--username",
                "operator",
            ],
            cwd=SOURCE,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        if not (root / ".secrets/admin-bootstrap.json").is_file():
            raise RuntimeError(
                "Review account exists but bootstrap credential is missing; inspect this volume locally."
            )
        from video_platform.management import ManagementStore

        management = ManagementStore(root)
        with management.auth.connect() as db:
            for mid in ("local-wan", "local-cogvideo", "local-h3", "minimax-h3"):
                db.execute("INSERT OR IGNORE INTO model_settings VALUES(?,0,1)", (mid,))
    return {"mode": "cpu_review", "account": "operator", "generation_enabled": False}


def blocked_operation(method, path):
    if method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return False
    path = path.rstrip("/")
    return (
        path == "/api/tasks"
        or path.startswith("/api/cloud/")
        or path.startswith("/api/agent/")
        or path == "/api/assistant/draft"
        or path == "/api/edits"
        or (
            path.startswith("/api/projects/") and path.endswith(("/export", "/exports"))
        )
    )


def create_review_app(core=None, preview=None):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    if core is None:
        from video_platform.api import create_app

        core = create_app()

    @asynccontextmanager
    async def lifespan(app):
        async with core.router.lifespan_context(core):
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def review_boundary(request, call_next):
        if blocked_operation(request.method, request.url.path):
            return JSONResponse(
                {
                    "detail": "这是独立审阅包：模型生成、需求模型和视频导出已关闭。可以保存项目/分镜、查看后台和已有真实样例。",
                    "mode": "cpu_review",
                },
                status_code=503,
            )
        response = await call_next(request)
        if response.status_code == 200 and response.headers.get(
            "content-type", ""
        ).startswith("text/html"):
            content = b"".join([part async for part in response.body_iterator]).decode(
                "utf-8"
            )
            if "</body>" in content:
                content = content.replace("</body>", NOTICE + "</body>", 1)
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                not in {"content-length", "content-type", "etag", "last-modified"}
            }
            response = HTMLResponse(content, headers=headers)
        response.headers["X-FrameSeq-Mode"] = "cpu_review"
        return response

    @app.get("/healthz")
    def health():
        core.state.store.list(limit=1)
        return {"status": "ok", "mode": "cpu_review", "generation_enabled": False}

    app.mount(
        "/review", StaticFiles(directory=preview or SOURCE / "preview", html=True)
    )
    app.mount("/", core)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "init", "check"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    from video_platform.config import ROOT

    if args.action == "check":
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{args.port}/healthz", timeout=5
        ) as response:
            result = json.load(response)
        assert result["mode"] == "cpu_review" and not result["generation_enabled"]
        print(json.dumps(result))
        return
    print(json.dumps(initialize(ROOT)))
    if args.action == "serve":
        import uvicorn

        uvicorn.run(create_review_app(), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
