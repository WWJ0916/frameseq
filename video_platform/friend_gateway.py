"""Single-process, opt-in gateway for invited friends, separate from the operator API.

No GPU code, provider keys, password storage or generic forward-proxy routes.
The operator's API stays on loopback. Every private request revalidates its session.
"""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from .config import ROOT

COOKIE='frameseq_session'
LOCAL_ORIGIN='http://127.0.0.1:18083'
PAGES={'/','/explore','/login','/studio','/projects','/storyboard','/edits','/video-api'}
WORKSPACES={'/studio','/projects','/storyboard','/edits','/video-api'}
PUBLIC_GET={'/api/auth/me','/api/health','/api/public/catalog','/api/public/campaigns',
            '/api/public/demo','/api/public/demo/video','/api/public/demo/poster','/api/public/demo/subtitles'}
PRIVATE_GET={'/api/models','/api/presets','/api/library','/api/tasks','/api/cloud/state',
             '/api/projects','/api/storyboards','/api/edits','/api/edits/capabilities','/api/experience',
             '/api/video-api/providers','/api/video-api/connections','/api/video-api/tasks'}
HEX=r'[0-9a-f]{32}'
PROJECT=re.compile(r'/api/projects/'+HEX+r'(?:/exports)?')
BOARD=re.compile(r'/api/storyboards/'+HEX)
EDIT=re.compile(r'/api/edits/'+HEX+r'(?:/video)?')
EXPORT=re.compile(r'/api/exports/'+HEX+r'/video')
PROJECT_ACTION=re.compile(r'/api/projects/'+HEX+r'(?:/versions|/versions/'+HEX+r'/choose|/episodes/'+HEX+r'/export)')
TASK=re.compile(r'/api/tasks/[0-9a-f]{32}(?:/(?:video|preview|poster))?')
CANCEL=re.compile(r'/api/tasks/[0-9a-f]{32}/cancel')
VIDEO_CONNECTION=re.compile(r'/api/video-api/connections/'+HEX)
VIDEO_TASK=re.compile(r'/api/video-api/tasks/'+HEX+r'(?:/video)?')
VIDEO_CANCEL=re.compile(r'/api/video-api/tasks/'+HEX+r'/cancel')
ASSET=re.compile(r'/assets/[a-zA-Z0-9_-]+\.(?:css|js|mjs|svg|woff2)')
HOP={'host','connection','keep-alive','transfer-encoding','upgrade','proxy-authorization',
     'proxy-authenticate','te','trailer','content-encoding','content-length','set-cookie'}

def route_allowed(path,method):
    if method in {'GET','HEAD'}:
        if ASSET.fullmatch(path):return not path.startswith('/assets/admin')
        return path in PAGES|PUBLIC_GET|PRIVATE_GET or any(rule.fullmatch(path) for rule in (TASK,PROJECT,BOARD,EDIT,EXPORT,VIDEO_CONNECTION,VIDEO_TASK))
    if method=='PUT':return bool(re.fullmatch(r'/api/projects/'+HEX,path) or BOARD.fullmatch(path) or VIDEO_CONNECTION.fullmatch(path))
    if method=='DELETE':return bool(VIDEO_CONNECTION.fullmatch(path))
    return method=='POST' and (path in {'/api/auth/login','/api/auth/logout','/api/auth/password','/api/tasks','/api/projects','/api/storyboards','/api/edits','/api/video-api/connections','/api/video-api/tasks'} or bool(CANCEL.fullmatch(path) or PROJECT_ACTION.fullmatch(path) or VIDEO_CANCEL.fullmatch(path)))

def admin_route_allowed(path,method):
    """Explicit administrative surface; never a generic /api/admin proxy."""
    if method in {'GET','HEAD'}:
        return path in {'/admin','/assets/admin.css','/assets/admin.js',
            '/api/admin/overview','/api/admin/users','/api/admin/models',
            '/api/admin/campaigns','/api/admin/audit'}
    if method=='PATCH':return bool(re.fullmatch(r'/api/admin/users/'+HEX+r'/status',path))
    if method=='PUT':return bool(re.fullmatch(r'/api/admin/users/'+HEX+r'/membership',path)
        or re.fullmatch(r'/api/admin/models/[a-z0-9_-]{1,64}',path)
        or re.fullmatch(r'/api/admin/campaigns/'+HEX,path))
    return method=='POST' and path=='/api/admin/campaigns'

def invited_role(user,config):
    if user.get('role')=='creator' and user.get('id') in config.get('allowed_user_ids',[]):return 'creator'
    if user.get('role')=='admin' and user.get('id') in config.get('allowed_admin_ids',[]):return 'admin'
    return None

class Admission:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS submissions(uid TEXT,day INTEGER,key TEXT,body TEXT,status TEXT,PRIMARY KEY(uid,key))')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:db.close()
    def reserve(self,uid,key,body,active,maximum=2):
        day=int((time.time()+8*3600)//86400)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT body,status FROM submissions WHERE uid=? AND key=?',(uid,key)).fetchone()
            if old:
                if old[0]!=body:return 409
                if old[1]!='rejected':return 0
            if active:return 429
            count=db.execute("SELECT count(*) FROM submissions WHERE uid=? AND day=? AND status!='rejected'",(uid,day)).fetchone()[0]
            if count>=maximum:return 429
            db.execute('INSERT OR REPLACE INTO submissions VALUES(?,?,?,?,?)',(uid,day,key,body,'reserved'))
        return 0
    def finish(self,uid,key,status):
        with self.connect() as db:db.execute("UPDATE submissions SET status=CASE WHEN status='accepted' THEN status ELSE ? END WHERE uid=? AND key=?",(status,uid,key))
    def used(self,uid):
        day=int((time.time()+8*3600)//86400)
        with self.connect() as db:return db.execute("SELECT count(*) FROM submissions WHERE uid=? AND day=? AND status!='rejected'",(uid,day)).fetchone()[0]

def create_gateway(root=ROOT,transport=None):
    root=Path(root);config_path=root/'.secrets/friend-gateway.json'
    admission=Admission(root/'data/friend-gateway.sqlite3')
    submit_lock=asyncio.Lock()
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @app.api_route('/{path:path}',methods=['GET','HEAD','POST','PUT','PATCH','DELETE','OPTIONS'])
    async def proxy(request:Request,path:str):
        try:config=json.loads(config_path.read_text())
        except (OSError,ValueError):return JSONResponse({'detail':'试用入口未启用。'},503)
        origin=config.get('public_origin')
        origins={LOCAL_ORIGIN}|({origin} if origin else set())
        if not config.get('enabled'):return JSONResponse({'detail':'试用入口已关闭。'},503)
        if time.time()>=config.get('expires_at',0):return JSONResponse({'detail':'临时试用已到期。'},503)
        if request.headers.get('host') not in {urlsplit(o).netloc for o in origins}:
            return JSONResponse({'detail':'访问域名不匹配。'},400)
        maximum=config.get('daily_limit',2)
        if type(maximum) is not int or not 1<=maximum<=100:return JSONResponse({'detail':'体验额度配置无效。'},503)
        admin_route=admin_route_allowed(request.url.path,request.method)
        if not route_allowed(request.url.path,request.method) and not admin_route:return JSONResponse({'detail':'此入口未开放该功能。'},404)
        unsafe=request.method not in {'GET','HEAD','OPTIONS'}
        if unsafe and request.headers.get('origin') not in origins:
            return JSONResponse({'detail':'请求来源不匹配。'},403)
        raw=b''
        async for chunk in request.stream():
            raw+=chunk
            if len(raw)>1024*1024:return JSONResponse({'detail':'请求体过大。'},413)
        headers={'Accept':request.headers.get('accept','*/*')}
        for name in ('content-type','x-csrf-token','idempotency-key','range','if-range'):
            if name in request.headers:headers[name]=request.headers[name]
        if request.cookies.get(COOKIE):headers['Cookie']=COOKIE+'='+request.cookies[COOKIE]
        # Rewrite only after validating the original Host and Origin above.
        if unsafe:headers['Origin']='http://127.0.0.1:18080'
        client=httpx.AsyncClient(base_url='http://127.0.0.1:18080',trust_env=False,
                                 follow_redirects=False,timeout=30,transport=transport)
        response=None
        try:
            session=(await client.get('/api/auth/me',headers=headers)).json()
            user=session.get('user') or {}
            role=invited_role(user,config);allowed=bool(role)
            if user and not allowed:return JSONResponse({'detail':'此账号未获此入口邀请。'},403)
            if admin_route and role!='admin':
                if not user and request.url.path=='/admin':return RedirectResponse('/login?next=/admin',303)
                return JSONResponse({'detail':'此入口未开放该功能。'},404)
            if request.url.path=='/api/auth/login' and request.method=='POST':
                try:body=json.loads(raw)
                except ValueError:return JSONResponse({'detail':'登录内容格式错误。'},400)
                if str(body.get('username','')).strip().lower() not in config.get('allowed_usernames',[])+config.get('allowed_admin_usernames',[]):
                    return JSONResponse({'detail':'此账号未获试用邀请。'},403)
            public=request.url.path in PAGES|PUBLIC_GET or bool(ASSET.fullmatch(request.url.path))
            if not allowed and not public and request.url.path!='/api/auth/login':
                return JSONResponse({'detail':'请先登录。'},401)
            if request.url.path in WORKSPACES and not allowed:return RedirectResponse('/login?next='+request.url.path,303)
            if request.url.path=='/api/experience':
                used=admission.used(user['id'])
                return JSONResponse({'daily_limit':maximum,'used_today':used,'remaining_today':max(0,maximum-used),
                    'max_pending':1,'expires_at':config['expires_at'],'features':['projects','storyboards','edits','local_generation']+(['administration'] if role=='admin' else [])})
            url=request.url.path+('?' + request.url.query if request.url.query else '')
            if request.url.path in {'/api/tasks','/api/video-api/tasks'} and request.method=='POST':
                if not allowed:return JSONResponse({'detail':'请先登录。'},401)
                key=headers.get('idempotency-key','')
                if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',key):return JSONResponse({'detail':'请从工作台提交并保留提交标识。'},400)
                try:body=json.loads(raw)
                except ValueError:return JSONResponse({'detail':'提交格式错误。'},400)
                # Match TaskRequest's legacy default when old Wan pages omit model.
                if request.url.path=='/api/tasks' and body.get('model','local-wan') not in {'local-wan','local-cogvideo','local-h3'}:
                    return JSONResponse({'detail':'仅开放本地模型。'},422)
                canonical=json.dumps(body,sort_keys=True,ensure_ascii=True,separators=(',',':'))
                if request.url.path=='/api/video-api/tasks':canonical='video-api:'+canonical
                async with submit_lock:
                    history=await client.get('/api/tasks?limit=100',headers=headers);history.raise_for_status()
                    active=any(t['status'] in {'running','queued'} for t in history.json()['items'])
                    remote_history=await client.get('/api/video-api/tasks?limit=100',headers=headers)
                    if remote_history.status_code!=404:
                        remote_history.raise_for_status()
                        active=active or any(t['status'] in {'queued','submitting','waiting','downloading','unknown'} for t in remote_history.json()['items'])
                    status=admission.reserve(user['id'],key,canonical,active,maximum=maximum)
                    if status:return JSONResponse({'detail':f'每账号每天最多{maximum}条，最多1条未完成任务；重复提交须保持相同参数。'},status)
                    response=await client.post(url,headers=headers,content=raw)
                    explicitly_rejected=response.status_code<500 or (response.status_code==503 and response.headers.get('x-task-accepted')=='no')
                    admission.finish(user['id'],key,'accepted' if response.status_code<400 else 'rejected' if explicitly_rejected else 'unknown')
            else:
                response=await client.send(client.build_request(request.method,url,headers=headers,content=raw),stream=True)
            if request.url.path=='/api/auth/login' and response.status_code==200:
                login=json.loads(await response.aread());logged=login.get('user') or {}
                if not invited_role(logged,config):
                    return JSONResponse({'detail':'此账号未获试用邀请。'},403)
            out={k:v for k,v in response.headers.items() if k.lower() not in HOP}
            out.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'same-origin'})
            if 'text/html' in response.headers.get('content-type',''):
                content=(await response.aread()).decode('utf-8')
                hidden_admin="a[href='/admin']," if role!='admin' else ''
                content=content.replace('</head>',"<style>"+hidden_admin+"a[href^='/assistant'],a[href='/assets/h3-live.html'],#create-invite,#register-form,.auth-tabs,#switch-mode,#auth-guidance{display:none!important}</style></head>")
                content=content.replace('<body>',f"<body><aside data-friend-preview role='status' style='padding:12px 20px;background:#18302a;color:#ecfff6;text-align:center'>创作体验 · 每账号每天{maximum}条、同时1条未完成任务 · 项目、分镜草稿与作品独立保存</aside>")
                result=Response(content,status_code=response.status_code,headers=out)
            elif response.is_stream_consumed:
                result=Response(response.content,status_code=response.status_code,headers=out)
            else:
                async def cleanup():await response.aclose();await client.aclose()
                result=StreamingResponse(response.aiter_bytes(),status_code=response.status_code,headers=out,background=BackgroundTask(cleanup))
            for value in response.headers.get_list('set-cookie'):
                if value.lower().startswith(COOKIE+'='):
                    if request.headers['host']!=urlsplit(LOCAL_ORIGIN).netloc and '; secure' not in value.lower():value+='; Secure'
                    result.headers.append('set-cookie',value)
            return result
        except (httpx.HTTPError,ValueError):
            if response is not None:await response.aclose()
            await client.aclose()
            return JSONResponse({'detail':'模型服务暂时不可达，请稍后重试；不要重复提交。'},502)
        finally:
            if response is None or response.is_stream_consumed:await client.aclose()
    return app
