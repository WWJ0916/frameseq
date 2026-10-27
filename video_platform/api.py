"""Per-account preview HTTP API; model inference runs in separate processes."""
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Literal

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .inference import GenerationParameters
from .store import Conflict, QueueFull, ROOT, TaskStore
from .storyboard import Storyboard, StoryboardStore, RevisionConflict
from .projects import Project, ProjectStore, VersionLink, VersionChoice
from .ownership import is_admin
from .model_profiles import COG_PROFILE,COG_PROFILE_ID,COG_NEGATIVE,cog_profile_verified
from .model_profiles import WAN_NATIVE,WAN_NATIVE_ID,wan_native_verified
from .h3_runtime import H3_PROFILE,H3_PROFILE_ID,h3_verified,h3_ready,h3_catalog

PRESETS = {
    "fast": {"label": "快速验证 · 320×192 / 17帧", "width": 320, "height": 192, "frames": 17, "steps": 20},
    "preview": {"label": "清晰预览 · 480×272 / 33帧", "width": 480, "height": 272, "frames": 33, "steps": 40},
    "story": {"label": "分镜片段 · 480×272 / 81帧 / 约5秒", "width": 480, "height": 272, "frames": 81, "steps": 40},
    "cinema": {"label": "原生480P · 832×480 / 81帧 / 实验规格", "width":832,"height":480,"frames":81,"steps":40},
}


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)
    negative_prompt: str = Field(default="blurry, low quality, distorted, text, watermark, static image", max_length=2000)
    preset: Literal["fast", "preview", "story", "cinema"] = "fast"
    model: Literal['local-wan','local-cogvideo','local-h3'] = 'local-wan'
    seed: int = Field(default=42, ge=0, le=2147483647, strict=True)

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("请输入视频描述。")
        return value

    def parameters(self):
        if self.model=='local-h3':
            if self.preset!='fast' or ('negative_prompt' in self.model_fields_set and self.negative_prompt.strip()):
                raise ValueError('本地H3当前为约5.17秒固定规格，不支持Wan预设或负面描述。')
            return dict(H3_PROFILE,prompt=self.prompt,seed=self.seed)
        if self.model=='local-cogvideo':
            negative=self.negative_prompt if 'negative_prompt' in self.model_fields_set else COG_NEGATIVE
            return dict(COG_PROFILE,prompt=self.prompt,negative_prompt=negative,seed=self.seed)
        if self.preset=='cinema':
            return dict(WAN_NATIVE,preset='cinema',prompt=self.prompt,negative_prompt=self.negative_prompt,seed=self.seed)
        preset = {k: v for k, v in PRESETS[self.preset].items() if k != "label"}
        return asdict(GenerationParameters(prompt=self.prompt, negative_prompt=self.negative_prompt,
                                           seed=self.seed, max_sequence_length=512, **preset))


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: str = Field(min_length=3, max_length=1000)


class CloudRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)
    duration: int = Field(default=4, ge=4, le=15, strict=True)


def create_app(store=None, deployment=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.store = store or TaskStore()
        yield

    app = FastAPI(title="开源视频生成实验平台", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    if deployment is None:
        config=ROOT/'.secrets/deployment.json'
        deployment=json.loads(config.read_text()) if config.exists() else {'auth_required':True}
    auth_required=deployment.get('auth_required',True)
    from .auth import install_auth
    install_auth(app,ROOT,required=bool(auth_required),public_origin=deployment.get('public_origin'))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]+deployment.get('allowed_hosts',[]))
    @app.exception_handler(RequestValidationError)
    async def safe_validation(request,error):
        # Never echo password/key input in validation bodies or traces.
        return JSONResponse({'detail':'输入未通过检查，请核对字段格式、范围和长度。'},status_code=422)
    web_root = Path(__file__).resolve().parent.parent / "web"
    from .management import install_management, model_enabled
    install_management(app, ROOT, web_root)
    from .edits import install_edits
    install_edits(app, ROOT, web_root)
    from .video_api import install_video_api, catalog as video_api_catalog
    from .video_api_store import VideoAPIStore
    install_video_api(app, ROOT, web_root)
    app.mount("/assets", StaticFiles(directory=web_root), name="assets")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(web_root / ("explore.html" if auth_required else "studio-v3.html"), media_type="text/html")

    @app.get('/explore',include_in_schema=False)
    def explore():
        return FileResponse(web_root/'explore.html',media_type='text/html')

    @app.get('/login',include_in_schema=False)
    def login_page():
        return FileResponse(web_root/'login.html',media_type='text/html')

    @app.get('/api/public/catalog')
    def public_catalog():
        cog_verified=cog_profile_verified(ROOT)
        cog_suspended=(ROOT/'logs/cogvideo-suspended.json').exists()
        return {'models':[
            {'id':'local-wan','name':'Wan2.1 · 1.3B','execution':'self_hosted','verified':(ROOT/'logs/fp16-api-report.json').exists(),'description':'自行部署的开放权重模型，适合本地试验与分镜草稿。'},
            {'id':'local-cogvideo','name':'CogVideoX · 2B'+(' · 实验暂停' if cog_suspended else ' · 实验预览'),'execution':'self_hosted','verified':cog_verified,'preview':True,'description':'画面异常排查中，通用生成暂停，已有结果和证据保留。' if cog_suspended else '实验模型，有真实生成样例但通用画面质量有限；本机验收后才开放。'},
            {'id':'local-h3','name':'MiniMax H3 · 本地实验','execution':'self_hosted','verified':h3_verified(ROOT),'preview':True,'description':'双卡共同生成同一视频，约5.17秒480P；社区FP16兼容路径，含降温等待，生成较慢。'}],
            'access':'invite_preview','publicly_deployed':False}

    def public_demo_record():
        path=ROOT/'logs/public-demo.json'
        return json.loads(path.read_text()) if path.exists() else None

    @app.get('/api/public/demo')
    def public_demo():
        record=public_demo_record()
        return record or {'status':'not_installed','title':'原创示例尚未导入','note':'此实例尚未配置原创示例；不会自动生成或下载视频。'}

    def public_demo_artifact(filename,media_type,download=False):
        record=public_demo_record()
        if not record or record.get('status')!='ready':raise HTTPException(404,'示例成片尚未完成。')
        # Exactly one operator-approved demo export, never a user supplied path.
        export_id=record.get('export_id','')
        if not re.fullmatch('[0-9a-f]{32}',export_id):raise HTTPException(404,'示例不存在。')
        path=ROOT/'runs'/('episode-'+export_id)/filename
        if not path.is_file():raise HTTPException(404,'示例文件暂不可用。')
        return FileResponse(path,media_type=media_type,filename='frameseq-demo.mp4' if download else None,content_disposition_type='attachment' if download else 'inline')

    @app.get('/api/public/demo/video')
    def public_demo_video(download:bool=False):return public_demo_artifact('video.mp4','video/mp4',download)
    @app.get('/api/public/demo/poster')
    def public_demo_poster():return public_demo_artifact('poster.jpg','image/jpeg')
    @app.get('/api/public/demo/subtitles')
    def public_demo_subtitles():return public_demo_artifact('subtitles.srt','text/plain; charset=utf-8')

    @app.get("/assistant", include_in_schema=False)
    def local_assistant():
        return FileResponse(web_root / "index.html", media_type="text/html")

    @app.get("/studio", include_in_schema=False)
    def studio():
        return FileResponse(web_root / "studio-v3.html", media_type="text/html")

    @app.get("/studio-classic", include_in_schema=False)
    def studio_classic():
        return FileResponse(web_root / "studio-v3.html", media_type="text/html")

    @app.get("/storyboard", include_in_schema=False)
    def storyboard_page():
        return FileResponse(web_root / "storyboard.html", media_type="text/html")

    @app.get("/projects", include_in_schema=False)
    def projects_page():
        return FileResponse(web_root / "projects.html", media_type="text/html")

    def project_operation(action):
        try:
            return action()
        except RevisionConflict as error:
            raise HTTPException(409, str(error))
        except LookupError as error:
            raise HTTPException(404, str(error))
        except ValueError as error:
            raise HTTPException(422, str(error))

    def enriched_project(project):
        if project is None:
            raise HTTPException(404, "项目不存在。")
        from .cloud import CloudStore
        for version in project["versions"]:
            task = (CloudStore(ROOT).get(version["task_id"]) if version["origin"] == "cloud"
                    else VideoAPIStore(ROOT).get(version["task_id"]) if version["origin"] == "api"
                    else app.state.store.get(version["task_id"]))
            version["status"] = task["status"] if task else "missing"
            metrics = (task.get("result") if version["origin"] == "cloud" else task.get("metrics")) if task else {}
            metrics = metrics or {}
            version["actual_seconds"] = metrics.get("duration_seconds") or (metrics.get("validation") or {}).get("duration_seconds")
            version['quality_rejected']=metrics.get('visual_review',{}).get('approved') is False
            prefix = {"cloud":"/api/cloud/tasks/", "api":"/api/video-api/tasks/"}.get(version["origin"],"/api/tasks/")
            version["video_url"] = prefix + version["task_id"] + "/video" if task and task["status"] == "succeeded" else None
        return project

    @app.get("/api/projects")
    def projects_list():
        return {"items": [enriched_project(p) for p in ProjectStore(ROOT).list()]}

    @app.get("/api/projects/{project_id}")
    def project_get(project_id: str):
        return enriched_project(ProjectStore(ROOT).get(project_id))

    @app.post("/api/projects", status_code=201)
    def project_create(body: Project):
        return enriched_project(project_operation(lambda: ProjectStore(ROOT).save(body)))

    @app.put("/api/projects/{project_id}")
    def project_update(project_id: str, body: Project):
        return enriched_project(project_operation(lambda: ProjectStore(ROOT).save(body, project_id)))

    @app.post("/api/projects/{project_id}/versions", status_code=201)
    def project_link(project_id: str, body: VersionLink):
        from .cloud import CloudStore
        task = CloudStore(ROOT).get(body.task_id) if body.origin == "cloud" else VideoAPIStore(ROOT).get(body.task_id) if body.origin == "api" else app.state.store.get(body.task_id)
        if task is None:
            raise HTTPException(404, "只能关联实际存在的生成任务。")
        parameters = {**task["parameters"], **({"prompt": task["prompt"]} if body.origin == "cloud" else {})}
        return enriched_project(project_operation(lambda: ProjectStore(ROOT).link(project_id, body, {**task, "parameters": parameters})))

    @app.post("/api/projects/{project_id}/versions/{version_id}/choose")
    def project_choose(project_id: str, version_id: str, body: VersionChoice):
        project = enriched_project(ProjectStore(ROOT).get(project_id))
        version = next((v for v in project["versions"] if v["id"] == version_id), None)
        if not version:
            raise HTTPException(404, "生成版本不存在。")
        if version["status"] != "succeeded" or version.get('quality_rejected'):
            raise HTTPException(409, "只有已成功的视频才能设为采用版本。")
        return enriched_project(project_operation(lambda: ProjectStore(ROOT).choose(project_id, version_id, body.revision)))

    @app.get('/api/projects/{project_id}/exports')
    def project_exports(project_id:str):
        if not ProjectStore(ROOT).get(project_id):raise HTTPException(404,'项目不存在。')
        from .composition import ExportStore
        return {'items':ExportStore(ROOT).list(project_id)}

    @app.post('/api/projects/{project_id}/episodes/{episode_id}/export',status_code=202)
    def episode_export(project_id:str,episode_id:str,body:VersionChoice):
        from .composition import ExportStore
        from .ownership import owner_id
        store=ExportStore(ROOT)
        record,created=project_operation(lambda:store.prepare(project_id,episode_id,body.revision))
        if created:
            try:
                with (ROOT/'logs/composition.log').open('a') as stream:
                    subprocess.Popen([sys.executable,'-u','-m','video_platform.composition','--export-id',record['id'],'--owner-id',owner_id()],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            except OSError:store.update(record['id'],'failed',error='剪辑进程未启动，没有调用模型。')
        return store.get(record['id'])

    @app.get('/api/exports/{export_id}/video')
    def export_video(export_id:str,download:bool=False):
        from .composition import ExportStore
        if not re.fullmatch('[0-9a-f]{32}',export_id):raise HTTPException(404,'导出不存在。')
        record=ExportStore(ROOT).get(export_id)
        if not record:raise HTTPException(404,'导出不存在。')
        if record['status']!='succeeded':raise HTTPException(409,'成片尚未通过验收。')
        path=ROOT/'runs'/('episode-'+export_id)/'video.mp4'
        if not path.is_file():raise HTTPException(404,'成片文件缺失。')
        return FileResponse(path,media_type='video/mp4',filename='episode-'+export_id+'.mp4' if download else None,content_disposition_type='attachment' if download else 'inline')

    @app.get("/api/storyboards")
    def storyboard_list():
        return {"items": StoryboardStore(ROOT).list(), "execution_supported": False}

    @app.get("/api/storyboards/{board_id}")
    def storyboard_get(board_id: str):
        board = StoryboardStore(ROOT).get(board_id)
        if board is None:
            raise HTTPException(404, "草稿不存在。")
        return board

    def save_storyboard(body, board_id=None):
        try:
            return StoryboardStore(ROOT).save(body, board_id)
        except RevisionConflict as error:
            raise HTTPException(409, str(error))
        except LookupError as error:
            raise HTTPException(404, str(error))
        except ValueError as error:
            raise HTTPException(422, str(error))

    @app.post("/api/storyboards", status_code=201)
    def storyboard_create(body: Storyboard):
        return save_storyboard(body)

    @app.put("/api/storyboards/{board_id}")
    def storyboard_update(board_id: str, body: Storyboard):
        return save_storyboard(body, board_id)

    def local_runtime(task):
        if task['status']!='running':return {}
        try: record=json.loads((ROOT/'runs'/('task-'+task['id'])/'result.json').read_text())
        except (OSError,ValueError):return {}
        return {'stage':'显卡降温等待' if record.get('cooling') else record.get('stage_label','本地生成中'),
                'runtime':{key:record[key] for key in ('stage','stage_detail','cooling','temperature_c','temperature_by_gpu_c','last_event','physical_gpu','physical_gpus','parallelism') if key in record}}

    def task_or_404(task_id):
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise HTTPException(404, "任务不存在。")
        task = app.state.store.get(task_id)
        if not task:
            raise HTTPException(404, "任务不存在。")
        task.update(local_runtime(task))
        task["video_url"] = f"/api/tasks/{task_id}/video" if task["status"] == "succeeded" else None
        task["preview_url"] = f"/api/tasks/{task_id}/preview" if task["status"] == "succeeded" else None
        return task

    @app.get("/api/health")
    def health():
        worker_path = ROOT / "data/worker.json"
        try:
            worker = json.loads(worker_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            worker = {"state": "not_started"}
        if "updated_at" in worker:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(worker["updated_at"])).total_seconds()
            if age > 15:
                worker = {"state": "unavailable", "last_update": worker["updated_at"]}
        app.state.store.list(limit=1)
        if not is_admin():worker={key:worker[key] for key in ('state','models','profiles') if key in worker}
        return {"status": "ok", "model": "Wan2.1-T2V-1.3B", "execution": "self-hosted open weights",
                "worker": worker, "agent": {"weights_ready": (ROOT / "logs/qwen-download-complete.json").exists(),
                                              "model": "Qwen2.5-1.5B-Instruct", "device": "cpu"}}

    @app.get("/api/presets")
    def presets():
        return PRESETS

    @app.get("/api/cloud/state")
    def cloud_state():
        from .cloud import CloudStore
        if not is_admin():
            return {'configured':False,'admin_only':True,'budget_cap_cny':0,'reserved_cny':0,'remaining_unreserved_cny':0,
                    'actual_billing_verified':False,'latest':None,'history':[],'can_retry_after_topup':False,'pricing_current':False}
        return {**CloudStore(ROOT).state(),"configured":False,"disabled":True,
                "new_submissions_enabled":False,"pricing_current":False,
                "reason":"H3已改为本地推理，云API入口停用；旧作品保留。"}

    @app.get("/api/models")
    def models():
        cloud = cloud_state()
        verified = any(task["status"] == "succeeded" for task in cloud["history"])
        result = {"models": [
            {"id": "local-wan", "name": "Wan2.1 · 1.3B", "provider": "本地部署",
             "execution": "self_hosted", "verified": (ROOT/'logs/fp16-api-report.json').exists(), "configured": (ROOT/'logs/wan-download-complete.json').exists(),
             "native480_verified":wan_native_verified(ROOT),
             "description": "已在课题组单卡真实运行，适合验证完整任务流程。",
             "specification": "320×192 /17帧；480×272 /33或81帧"+("；原生832×480 /81帧（实验）" if wan_native_verified(ROOT) else "；原生480P验收中"), "cost_hint": "共享GPU依次执行，不调用商业API"},
            {"id": "local-cogvideo", "name": "CogVideoX · 2B · 实验预览", "provider": "本地部署",
             "execution": "self_hosted", "verified": cog_profile_verified(ROOT),"preview":True,"inference_profile":COG_PROFILE_ID,
             "configured": cog_profile_verified(ROOT) and not (ROOT/'logs/cogvideo-suspended.json').exists() and COG_PROFILE_ID in health()['worker'].get('profiles',[]),
             "unavailable_reason": "画面异常排查中，暂不接受新任务" if (ROOT/'logs/cogvideo-suspended.json').exists() else "模型或执行器尚未就绪",
             "description": "实验预览，通用画质有限；请明确主体和场景，优先尝试主体居中的小幅运动。异常输出会失败并保留记录。",
             "specification": "720×480 · 49帧 · 8fps · 约6秒 · 实验模型", "cost_hint":"本地GPU推理，无商业API费用；可能较慢"},
            h3_catalog(ROOT,health()['worker'].get('profiles',[]))
        ], "cloud":cloud,"note":"新任务均使用本地开放权重；H3云API停用，历史作品保留。"}
        for item in result['models']:
            item['enabled']=model_enabled(ROOT,item['id'])
            if not item['enabled']:
                item['configured']=False
                item['unavailable_reason']='管理员已暂停新任务，已有作品保留'
        result['models'].extend(video_api_catalog(ROOT))
        result['note']='本地模型与账号自己的视频API并存；API仅在明确选择并确认费用后提交。'
        return result

    @app.get("/api/library")
    def library(limit: int = Query(24, ge=1, le=100)):
        from .cloud import CloudStore
        local = app.state.store.list(limit=100)
        cloud = CloudStore(ROOT).list(limit=100)
        items = []
        for task in local["items"]:
            task = dict(task)
            task.update(local_runtime(task))
            task.update(origin="local", model=task['parameters'].get('model_id','Wan2.1-T2V-1.3B'), provider="本地部署",
                        metrics=task.get("metrics") or {},
                        video_url=f"/api/tasks/{task['id']}/video" if task["status"] == "succeeded" else None,
                        preview_url=f"/api/tasks/{task['id']}/poster" if task["status"] == "succeeded" else None)
            items.append(task)
        for task in cloud["items"]:
            task = dict(task)
            task.update(origin="cloud", model="MiniMax-H3", provider="MiniMax 官方API",
                        metrics=task.get("result") or {},
                        parameters={**task["parameters"], "prompt": task["prompt"]},
                        video_url=f"/api/cloud/tasks/{task['id']}/video" if task["status"] == "succeeded" else None,
                        preview_url=f"/api/cloud/tasks/{task['id']}/poster" if task["status"] == "succeeded" else None)
            items.append(task)
        video_api = VideoAPIStore(ROOT).list(limit=100)
        items.extend(video_api['items'])
        items.sort(key=lambda task: (task["created_at"], task["id"]), reverse=True)
        return {"items": items[:limit], "total": local["total"] + cloud["total"] + video_api['total']}

    @app.post("/api/cloud/tasks", status_code=202)
    def cloud_create(body: CloudRequest, idempotency_key: str | None = Header(default=None)):
        raise HTTPException(410,"H3云API提交已停用，请使用本地H3。")

    @app.post("/api/cloud/first-test", status_code=202)
    def cloud_first(body: CloudRequest):
        return create_cloud(body)

    @app.post("/api/cloud/retry-after-topup", status_code=202)
    def cloud_retry(body: CloudRequest):
        from .cloud import TOPUP_KEY
        return create_cloud(body, TOPUP_KEY)

    def create_cloud(body, request_key=None):
        raise HTTPException(410,"H3云API提交已停用，请选择MiniMax H3本地实验。")

    @app.post("/api/cloud/tasks/{job_id}/resume-result", status_code=202)
    def cloud_resume_result(job_id: str):
        raise HTTPException(410,"云API查询已停用，已有本地视频仍可播放下载。")

    @app.get("/api/cloud/tasks/{job_id}/video")
    def cloud_video(job_id: str, download: bool = False):
        return cloud_artifact(job_id, "video.mp4", "video/mp4", download)

    @app.get("/api/cloud/tasks/{job_id}/preview")
    def cloud_preview(job_id: str):
        return cloud_artifact(job_id, "contact-sheet.jpg", "image/jpeg")

    @app.get("/api/cloud/tasks/{job_id}/poster")
    def cloud_poster(job_id: str):
        return cloud_artifact(job_id, "poster.jpg", "image/jpeg")

    def cloud_artifact(job_id, filename, media_type, download=False):
        from .cloud import CloudStore
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(404, "云任务不存在。")
        task = CloudStore(ROOT).get(job_id)
        if not task:
            raise HTTPException(404, "云任务不存在。")
        if task["status"] != "succeeded":
            raise HTTPException(409, "真实视频尚未通过验收。")
        path = ROOT / "runs" / ("cloud-" + job_id) / filename
        if filename == "poster.jpg":
            from .posters import ensure_poster
            try:
                path = ensure_poster(path.parent / "video.mp4")
            except (OSError, ValueError, RuntimeError):
                raise HTTPException(503, "视频封面暂不可用，可稍后重试。")
        if not path.is_file():
            raise HTTPException(404, "云任务视频缺失。")
        return FileResponse(path, media_type=media_type, filename=f"h3-{job_id}.mp4" if download else None,
                            content_disposition_type="attachment" if download else "inline")

    @app.post("/api/agent/plan")
    def agent_plan(body: AgentRequest):
        from .agent import Busy, InvalidModelOutput, plan_request
        try:
            return plan_request(body.request)
        except Busy as e:
            raise HTTPException(429, str(e))
        except InvalidModelOutput as e:
            raise HTTPException(502, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))

    @app.post("/api/agent/plans/{plan_id}/submit", status_code=202)
    def agent_submit(plan_id: str):
        from .agent import submit_plan
        try:
            task = submit_plan(plan_id, app.state.store)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except QueueFull as e:
            raise HTTPException(429, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return task_or_404(task["id"])

    @app.post("/api/tasks", status_code=202)
    def submit(body: TaskRequest, idempotency_key: str | None = Header(default=None)):
        try:maintenance=json.loads((ROOT/'data/generation-paused.json').read_text()).get('enabled',False)
        except FileNotFoundError:maintenance=False
        except (OSError,ValueError):maintenance=True
        if maintenance:raise HTTPException(503,'生成服务正在维护，尚未创建或扣除任务；请稍后重试。',headers={'X-Task-Accepted':'no','Retry-After':'60'})
        if idempotency_key and not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", idempotency_key):
            raise HTTPException(400, "提交标识格式错误。")
        if body.model=='local-h3' and (not h3_ready(ROOT) or H3_PROFILE_ID not in health()['worker'].get('profiles',[])):
            raise HTTPException(409,'本地H3环境或执行器尚未就绪，未创建任务。')
        if body.model=='local-wan' and body.preset=='cinema':
            if WAN_NATIVE_ID not in health()['worker'].get('profiles',[]):
                raise HTTPException(409,'原生480P执行器版本未就绪，未创建任务。')
            if not wan_native_verified(ROOT) and not is_admin():
                raise HTTPException(409,'原生480P仍在管理员实验验收中，未创建任务。')
        if body.model=='local-cogvideo' and (ROOT/'logs/cogvideo-suspended.json').exists():
            raise HTTPException(409,'CogVideoX画面异常排查中，暂不接受新任务；已有任务和证据保留。')
        if body.model=='local-cogvideo' and (not cog_profile_verified(ROOT) or COG_PROFILE_ID not in health()['worker'].get('profiles',[])):
            raise HTTPException(409,'CogVideoX当前推理配置尚未完成本机验收，或执行器版本未就绪；未创建任务。')
        try:
            task = app.state.store.create(body.parameters(), idempotency_key)
        except Conflict as e:
            raise HTTPException(409, str(e))
        except QueueFull as e:
            raise HTTPException(429, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return task_or_404(task["id"])

    @app.get("/api/tasks")
    def history(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        return app.state.store.list(limit, offset)

    @app.get("/api/tasks/{task_id}")
    def detail(task_id: str):
        return task_or_404(task_id)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        task_or_404(task_id)
        try:
            app.state.store.cancel(task_id)
        except Conflict as e:
            raise HTTPException(409, str(e))
        return task_or_404(task_id)

    def artifact(task_id, name, media_type, download=False):
        task = task_or_404(task_id)
        if task["status"] != "succeeded":
            raise HTTPException(409, "任务尚未成功生成结果。")
        # No user-controlled file path enters the file response.
        target = ROOT / "runs" / f"task-{task_id}" / name
        if name == "poster.jpg":
            from .posters import ensure_poster
            try:
                target = ensure_poster(target.parent / "video.mp4")
            except (OSError, ValueError, RuntimeError):
                raise HTTPException(503, "视频封面暂不可用，可稍后重试。")
        if not target.is_file():
            raise HTTPException(404, "结果文件缺失，请检查服务器日志。")
        return FileResponse(target, media_type=media_type, filename=f"video-{task_id}.mp4" if download else None,
                            content_disposition_type="attachment" if download else "inline")

    @app.get("/api/tasks/{task_id}/video")
    def video(task_id: str, download: bool = False):
        return artifact(task_id, "video.mp4", "video/mp4", download)

    @app.get("/api/tasks/{task_id}/preview")
    def preview(task_id: str):
        return artifact(task_id, "contact-sheet.jpg", "image/jpeg")

    @app.get("/api/tasks/{task_id}/poster")
    def poster(task_id: str):
        return artifact(task_id, "poster.jpg", "image/jpeg")

    return app


app = create_app()
