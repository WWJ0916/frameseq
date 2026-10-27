"""BYOK contract/safety tests. All providers and downloads are simulated; no bills."""

import json, socket, tempfile, time, unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from video_platform.video_protocols import (
    VideoProvider,
    ProviderError,
    parameters,
    download_result,
    safe_result_target,
    base_url,
)
from video_platform.video_api_store import VideoAPIStore
from video_platform.video_api_worker import process_one
from video_platform.ownership import as_user
from video_platform.api import create_app
from video_platform.store import TaskStore, Conflict, QueueFull, now
from video_platform.auth import AuthStore

SETTINGS = {
    "provider": "minimax",
    "label": "My H3",
    "model": "MiniMax-H3",
    "region": "cn",
    "workspace": "",
}
ALICE = {"id": "alice", "role": "creator"}
BOB = {"id": "bob", "role": "creator"}


def address(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


class VideoAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = VideoAPIStore(self.root)
        (self.root / "data/video-api-policy.json").write_text('{"enabled":true}')
        (self.root / "logs").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def connection(self):
        return self.store.save(SETTINGS, "fixture-video-key-secret")

    def prepare(self, c, key="same-key", **kw):
        return self.store.prepare(c["id"], "A blue boat", 5, "768P", "16:9", key, **kw)

    def test_encrypted_storage_no_key_in_metadata_and_owner_update_delete_isolation(
        self,
    ):
        with as_user(ALICE):
            c = self.connection()
        self.assertNotIn(b"fixture-video-key-secret", self.store.path.read_bytes())
        self.assertNotIn("secret", json.dumps(c))
        self.assertEqual(c["credential_status"], "stored_not_verified")
        with as_user(BOB):
            self.assertEqual(self.store.connections(), [])
            for revision in (0, c["revision"]):
                with self.assertRaises(LookupError):
                    self.store.save(SETTINGS, "new-fixture-key", c["id"], revision)
            with self.assertRaises(LookupError):
                self.store.delete(c["id"])
        with as_user(ALICE):
            self.assertEqual(len(self.store.connections()), 1)
            updated = self.store.save(
                {**SETTINGS, "label": "Renamed"}, None, c["id"], c["revision"]
            )
            self.assertEqual(updated["revision"], 2)
            with self.assertRaises(Conflict):
                self.store.save(SETTINGS, None, c["id"], c["revision"])
        with self.store.connect() as db:
            sealed = db.execute(
                "SELECT sealed_key FROM connections WHERE id=?", (c["id"],)
            ).fetchone()[0]
        with self.assertRaises(ValueError):
            self.store.unseal(sealed, "bob", c["id"])
        if __import__("os").name != "nt":
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_explicit_consent_idempotency_limit_and_private_job(self):
        with as_user(ALICE):
            c = self.connection()
            with self.assertRaises(ValueError):
                self.prepare(c)
            job, created = self.prepare(c, confirmed=True)
            self.assertTrue(created)
            same, created = self.prepare(c, confirmed=True)
            self.assertFalse(created)
            self.assertEqual(job["id"], same["id"])
            with self.assertRaises(Conflict):
                self.store.prepare(
                    c["id"], "other prompt", 5, "768P", "16:9", "same-key", True
                )
            self.prepare(c, "second", confirmed=True)
            with self.assertRaises(QueueFull):
                self.prepare(c, "third", confirmed=True)
        with as_user(BOB):
            self.assertIsNone(self.store.get(job["id"]))
            self.assertEqual(self.store.list()["items"], [])
            with self.assertRaises(LookupError):
                self.store.cancel(job["id"])

    def test_concurrent_same_submission_is_one_job(self):
        with as_user(ALICE):
            c = self.connection()

        def submit(_):
            with as_user(ALICE):
                return self.prepare(c, confirmed=True)[0]["id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(submit, range(8)))
        self.assertEqual(len(set(ids)), 1)

    def test_revocation_cancels_unsent_but_retains_submitted_poll_credentials(self):
        with as_user(ALICE):
            c = self.connection()
            queued, _ = self.prepare(c, confirmed=True)
            waiting, _ = self.prepare(c, "second", confirmed=True)
        self.store.update(
            waiting["id"], status="waiting", provider_task_id="official-id"
        )
        with as_user(ALICE):
            self.store.delete(c["id"])
            self.assertEqual(self.store.get(queued["id"])["status"], "cancelled")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE id=?", (waiting["id"],)
            ).fetchone()
            self.assertEqual(
                self.store.unseal(row["sealed_key"], "alice", waiting["id"]),
                "fixture-video-key-secret",
            )

    def test_cancel_submitting_conflicts_and_restart_does_not_repost(self):
        with as_user(ALICE):
            c = self.connection()
            job, _ = self.prepare(c, confirmed=True)
        self.store.next()
        with as_user(ALICE):
            with self.assertRaises(Conflict):
                self.store.cancel(job["id"])
        self.store.recover()
        with as_user(ALICE):
            self.assertEqual(self.store.get(job["id"])["status"], "unknown")
        self.assertIsNone(self.store.next())

    def test_timeout_submit_is_never_automatically_repeated(self):
        with as_user(ALICE):
            c = self.connection()
            job, _ = self.prepare(c, confirmed=True)
        calls = []

        class Ambiguous:
            def __init__(self, *args):
                pass

            def submit(self, p):
                calls.append(p)
                raise ProviderError("Timeout")

        process_one(self.store, Ambiguous)
        process_one(self.store, Ambiguous)
        self.assertEqual(len(calls), 1)
        with as_user(ALICE):
            self.assertEqual(self.store.get(job["id"])["status"], "unknown")

    def test_mock_worker_complete_preserves_task_id_and_wipes_key(self):
        with as_user(ALICE):
            c = self.connection()
            job, _ = self.prepare(c, confirmed=True)
        calls = []

        class Fake:
            def __init__(self, *args):
                pass

            def submit(self, p):
                calls.append("POST")
                return "official-id"

            def query(self, tid):
                calls.append(tid)
                return {
                    "status": "succeeded",
                    "url": "https://cdn.minimax.cn/video.mp4",
                }

        def download(url, path, provider):
            path.write_bytes(b"fixture-not-real-video")

        def validate(path, requested_duration):
            return {"sha256": "fixture", "duration_seconds": requested_duration}

        process_one(self.store, Fake, download, validate)
        self.store.update(job["id"], next_poll=0)
        process_one(self.store, Fake, download, validate)
        with as_user(ALICE):
            result = self.store.get(job["id"])
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["metrics"]["execution"], "customer_video_api")
        self.assertEqual(calls, ["POST", "official-id"])
        # API footage keeps its actual model when carried into episode export.
        from video_platform.projects import Project, ProjectStore, VersionLink
        from video_platform.composition import ExportStore
        with as_user(ALICE):
            projects=ProjectStore(self.root)
            project=projects.save(Project(title='API episode',episodes=[{'id':'1'*32,'title':'Episode','shots':[{'id':'2'*32,'title':'Boat','seconds':4}]}]))
            project=projects.link(project['id'],VersionLink(episode_id='1'*32,shot_id='2'*32,origin='api',task_id=job['id'],revision=project['revision']),result)
            project=projects.choose(project['id'],project['versions'][0]['id'],project['revision'])
            export,_=ExportStore(self.root).prepare(project['id'],'1'*32,project['revision'])
            self.assertEqual(export['plan']['clips'][0]['model'],'MiniMax-H3')
            self.assertEqual(export['plan']['clips'][0]['origin'],'api')
        with self.store.connect() as db:
            self.assertIsNone(
                db.execute(
                    "SELECT sealed_key FROM jobs WHERE id=?", (job["id"],)
                ).fetchone()[0]
            )

    def test_provider_contracts_and_regions(self):
        configs = [
            SETTINGS,
            {
                "provider": "seedance",
                "model": "doubao-seedance-2-5-260628",
                "region": "cn-beijing",
            },
            {
                "provider": "wan",
                "model": "wan3.0-video",
                "region": "cn-beijing",
                "workspace": "workspace123",
            },
        ]
        for c in configs:
            calls = []

            def handle(req):
                calls.append(req)
                if req.method == "POST":
                    return httpx.Response(
                        200,
                        json={
                            "task_id": "task-1",
                            "id": "task-1",
                            "output": {"task_id": "task-1"},
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "task": {
                            "status": "succeeded",
                            "content": {"url": "https://cdn.minimax.cn/v.mp4"},
                        },
                        "status": "succeeded",
                        "content": {"video_url": "https://cdn.volces.com/v.mp4"},
                        "output": {
                            "task_status": "SUCCEEDED",
                            "video_url": "https://cdn.aliyuncs.com/v.mp4",
                        },
                    },
                )

            resolution = (
                "720p"
                if c["provider"] == "seedance"
                else "720P" if c["provider"] == "wan" else "768P"
            )
            p = parameters(c, "boat", 5, resolution, "16:9")
            client = VideoProvider(c, "fixture-api-key", httpx.MockTransport(handle))
            self.assertEqual(client.submit(p), "task-1")
            self.assertEqual(client.query("task-1")["status"], "succeeded")
            self.assertEqual(
                calls[0].headers["Authorization"], "Bearer fixture-api-key"
            )
            self.assertEqual(
                str(calls[0].url.copy_with(path="", query=None)), base_url(c)
            )
            body = json.loads(calls[0].content)
            self.assertEqual(body["model"], c["model"])
            if c["provider"] == "wan":
                self.assertEqual(body["input"]["prompt"], "boat")
                self.assertEqual(calls[0].headers["X-DashScope-Async"], "enable")
            else:
                self.assertEqual(body["content"], [{"type": "text", "text": "boat"}])
                self.assertEqual(body["duration"], 5)

    def test_bad_region_model_spec_and_workspace_rejected(self):
        for c in [
            {**SETTINGS, "region": "evil.com"},
            {**SETTINGS, "model": "unknown"},
            {
                "provider": "wan",
                "model": "wan3.0-video",
                "region": "cn-beijing",
                "workspace": "../localhost",
            },
        ]:
            with self.assertRaises(ValueError):
                parameters(c, "boat", 5, "768P", "16:9")
        with self.assertRaises(ValueError):
            parameters(SETTINGS, "boat", 5, "480P", "16:9")

    def test_signed_download_is_credential_free_pinned_and_private_redirect_blocked(
        self,
    ):
        requests = []

        def handle(req):
            requests.append(req)
            return httpx.Response(200, content=b"fixture")

        path = self.root / "download.mp4"
        download_result(
            "https://cdn.minimax.cn/video.mp4?signed=fixture",
            path,
            "minimax",
            httpx.MockTransport(handle),
            address,
        )
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertEqual(requests[0].url.host, "8.8.8.8")
        self.assertEqual(requests[0].headers["host"], "cdn.minimax.cn")
        self.assertEqual(requests[0].extensions["sni_hostname"], "cdn.minimax.cn")
        self.assertNotIn("authorization", requests[0].headers)

        def redirect(req):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

        with self.assertRaises(ProviderError):
            download_result(
                "https://cdn.minimax.cn/v.mp4",
                self.root / "blocked.mp4",
                "minimax",
                httpx.MockTransport(redirect),
                address,
            )
        with self.assertRaises(ProviderError):
            safe_result_target(
                "https://cdn.minimax.cn/v.mp4",
                "minimax",
                lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))],
            )

    def test_http_zero_paid_calls_catalog_scope_csrf_and_artifact_isolation(self):
        auth = AuthStore(self.root)
        admin = auth.create_user(
            "operator", "fixture-admin-password", "Admin", admin=True
        )
        auth.create_user(
            "alice", "fixture-alice-password", "Alice", auth.invite(admin["id"])
        )
        auth.create_user(
            "bobby", "fixture-bobby-password", "Bob", auth.invite(admin["id"])
        )
        with patch("video_platform.api.ROOT", self.root), patch(
            "video_platform.video_protocols.VideoProvider.request",
            side_effect=AssertionError("No provider calls"),
        ):
            with TestClient(
                create_app(
                    TaskStore(self.root / "data/tasks.sqlite3"), {"auth_required": True}
                )
            ) as client:
                self.assertEqual(
                    client.get("/api/video-api/connections").status_code, 401
                )
                login = client.post(
                    "/api/auth/login",
                    json={"username": "alice", "password": "fixture-alice-password"},
                ).json()
                h = {"X-CSRF-Token": login["csrf"]}
                self.assertEqual(
                    client.post(
                        "/api/video-api/connections",
                        json={**SETTINGS, "api_key": "fixture-http-key"},
                    ).status_code,
                    403,
                )
                r = client.post(
                    "/api/video-api/connections",
                    headers=h,
                    json={**SETTINGS, "api_key": "fixture-http-key"},
                )
                self.assertEqual(r.status_code, 201)
                cid = r.json()["id"]
                self.assertNotIn("fixture-http-key", r.text)
                models = client.get("/api/models").json()["models"]
                self.assertTrue(any(m["id"] == "video-api:" + cid for m in models))
                self.assertTrue(any(m["id"] == "local-h3" for m in models))
                (self.root / "data/video-api-worker.json").write_text(
                    json.dumps({"state": "ready", "updated_at": now()})
                )
                payload = dict(
                    connection_id=cid,
                    prompt="boat",
                    duration=5,
                    resolution="768P",
                    ratio="16:9",
                )
                self.assertEqual(
                    client.post(
                        "/api/video-api/tasks",
                        headers={**h, "Idempotency-Key": "same"},
                        json=payload,
                    ).status_code,
                    422,
                )
                r = client.post(
                    "/api/video-api/tasks",
                    headers={**h, "Idempotency-Key": "same"},
                    json={**payload, "allow_paid_call": True},
                )
                self.assertEqual(r.status_code, 202)
                jid = r.json()["id"]
                self.assertEqual(
                    client.get("/api/library").json()["items"][0]["origin"], "api"
                )
                client.post(
                    "/api/auth/login",
                    json={"username": "bobby", "password": "fixture-bobby-password"},
                )
                self.assertEqual(
                    client.get("/api/video-api/tasks/" + jid).status_code, 404
                )
                self.assertEqual(
                    client.get("/api/video-api/tasks/" + jid + "/video").status_code,
                    404,
                )
                self.assertEqual(
                    client.get("/api/video-api/connections").json()["items"], []
                )
                self.assertFalse(
                    any(
                        m["id"] == "video-api:" + cid
                        for m in client.get("/api/models").json()["models"]
                    )
                )
