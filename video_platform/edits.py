"""Non-destructive temporal editing. Replacement composition is NOT AI inpainting."""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .composition import ExportStore
from .ownership import as_user, owner_id
from .store import ROOT, TaskStore, now


class Asset(BaseModel):
    model_config=ConfigDict(extra='forbid')
    origin:Literal['local','cloud','api']
    task_id:str=Field(pattern=r'^[0-9a-f]{32}$')


class EditRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source:Asset
    start:float=Field(ge=0,le=15,allow_inf_nan=False)
    end:float=Field(gt=0,le=15,allow_inf_nan=False)
    prompt:str=Field(default='',max_length=2000)
    mode:Literal['draft','replace']='draft'
    replacement:Asset|None=None
    replacement_start:float=Field(default=0,ge=0,le=15,allow_inf_nan=False)


def asset(root,ref):
    from .cloud import CloudStore
    from .video_api_store import VideoAPIStore
    task=CloudStore(root).get(ref.task_id) if ref.origin=='cloud' else VideoAPIStore(root).get(ref.task_id) if ref.origin=='api' else TaskStore(root/'data/tasks.sqlite3').get(ref.task_id)
    if not task or task['status']!='succeeded':
        raise ValueError('只能使用自己已完成的视频。')
    metrics=(task.get('result') if ref.origin=='cloud' else task.get('metrics')) or {}
    if metrics.get('visual_review',{}).get('approved') is False:
        raise ValueError('画面已被拒绝的素材不能用于新版本。')
    validation=metrics.get('validation') if isinstance(metrics.get('validation'),dict) else metrics
    seconds=validation.get('duration_seconds')
    path=root/'runs'/({'cloud':'cloud-','api':'video-api-'}.get(ref.origin,'task-')+ref.task_id)/'video.mp4'
    if not path.is_file() or not seconds or not 0<seconds<=16:
        raise ValueError('只支持已验收的16秒内短视频。')
    expected=validation.get('sha256')
    if not expected or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        raise ValueError('源文件校验失败。')
    return dict(**ref.model_dump(),seconds=seconds,sha256=expected),path


class EditStore:
    def __init__(self,root):
        self.root=root
        self.base=ExportStore(root)
        with self.base.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS edits(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,request_key TEXT UNIQUE NOT NULL,status TEXT NOT NULL,plan TEXT NOT NULL,result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')

    def decode(self,row):
        if row is None:return None
        d=dict(row);d.pop('request_key');d['plan']=json.loads(d['plan']);d['result']=json.loads(d['result']) if d['result'] else None
        d['video_url']='/api/edits/'+d['id']+'/video' if d['status']=='succeeded' else None
        return d

    def get(self,eid):
        with self.base.connect() as db:return self.decode(db.execute('SELECT * FROM edits WHERE id=? AND owner_id=?',(eid,owner_id())).fetchone())

    def list(self):
        with self.base.connect() as db:return [self.decode(r) for r in db.execute('SELECT * FROM edits WHERE owner_id=? ORDER BY created_at DESC,id DESC LIMIT 100',(owner_id(),))]

    def prepare(self,body,key):
        source,_=asset(self.root,body.source)
        if body.end<=body.start or body.end>source['seconds']+.001 or body.end-body.start<.1:
            raise ValueError('请选择视频范围内至少0.1秒的片段，结束应晚于开始。')
        replacement=None
        if body.mode=='replace':
            if body.replacement is None:raise ValueError('请明确选择替换素材。')
            replacement,_=asset(self.root,body.replacement)
            if body.replacement_start+body.end-body.start>replacement['seconds']+.001:
                raise ValueError('替换素材不足所选时长，不自动循环或补静帧。')
        elif not body.prompt.strip():raise ValueError('请填写片段修改需求。')
        plan=body.model_dump()
        plan.update(source=source,replacement=replacement,ai_generated=False,audio_policy='preserve_source_audio_if_present',render_note='替换剪辑，全片重新编码；不是AI局部重拍，不保证切口连续或逐像素相同。')
        payload=json.dumps(plan,sort_keys=True,ensure_ascii=False)
        with self.base.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM edits WHERE request_key=?',(owner_id()+':'+key,)).fetchone()
            if old:
                if old['plan']!=payload:raise ValueError('同一个提交标识不能对应不同剪辑。')
                return self.decode(old),False
            if body.mode=='replace' and db.execute("SELECT count(*) FROM edits WHERE status IN ('queued','running')").fetchone()[0]>=2:
                raise ValueError('已有片段剪辑在处理，请稍后再试。')
            if db.execute("SELECT count(*) FROM edits WHERE owner_id=? AND date(created_at,'+8 hours')=date(?,'+8 hours')",(owner_id(),now())).fetchone()[0]>=30:
                raise ValueError('每日最多保存30条片段编辑记录。')
            eid=uuid.uuid4().hex;stamp=now();status='queued' if body.mode=='replace' else 'draft'
            db.execute('INSERT INTO edits VALUES(?,?,?,?,?,?,?,?,?)',(eid,owner_id(),owner_id()+':'+key,status,payload,None,None,stamp,stamp))
            return self.decode(db.execute('SELECT * FROM edits WHERE id=?',(eid,)).fetchone()),body.mode=='replace'

    def update(self,eid,status,result=None,error=None):
        with self.base.connect() as db:db.execute('UPDATE edits SET status=?,result=?,error=?,updated_at=? WHERE id=? AND owner_id=?',(status,json.dumps(result,ensure_ascii=False) if result else None,error,now(),eid,owner_id()))


def render(store,eid):
    with (store.root/'logs/clip-edit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=store.get(eid)
        if not record or record['status']!='queued':return record
        store.update(eid,'running');started=time.perf_counter()
        run=store.root/'runs'/('edit-'+eid)
        try:
            import imageio.v2 as imageio
            import imageio_ffmpeg
            from PIL import Image,ImageOps
            import numpy as np
            run.mkdir(exist_ok=False)
            p=record['plan'];a,source=asset(store.root,Asset(**{k:p['source'][k] for k in ('origin','task_id')}));b,replacement=asset(store.root,Asset(**{k:p['replacement'][k] for k in ('origin','task_id')}))
            if a['sha256']!=p['source']['sha256'] or b['sha256']!=p['replacement']['sha256']:raise ValueError('素材版本发生变化。')
            original=imageio.get_reader(source);other=imageio.get_reader(replacement)
            try:
                meta=original.get_meta_data();fps=float(meta['fps']);w,h=meta['size'];rfps=float(other.get_meta_data()['fps'])
                if w*h>1920*1080 or not 1<=fps<=60:raise ValueError('片段规格超过CPU剪辑上限。')
                start=math.ceil(p['start']*fps-1e-7);end=math.ceil(p['end']*fps-1e-7)
                count=0;changed=0
                with imageio.get_writer(run/'silent.mp4',fps=fps,codec='libx264',macro_block_size=None,ffmpeg_log_level='error',ffmpeg_params=['-threads','2','-crf','18']) as writer:
                    for i,frame in enumerate(original):
                        if i>=1000:raise ValueError('视频帧数超过剪辑上限。')
                        if start<=i<end:
                            ri=int((p['replacement_start']+i/fps-p['start'])*rfps+1e-7)
                            frame=np.asarray(ImageOps.pad(Image.fromarray(other.get_data(ri)),(w,h),method=Image.Resampling.LANCZOS,color=(0,0,0)))
                            changed+=1
                        writer.append_data(frame);count+=1
                if changed==0:raise ValueError('所选区间没有可替换的视频帧。')
            finally:original.close();other.close()
            ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
            result=subprocess.run([ffmpeg,'-v','error','-nostdin','-i',str(run/'silent.mp4'),'-i',str(source),'-map','0:v:0','-map','1:a:0?','-c:v','copy','-c:a','copy','-t',str(count/fps),str(run/'video.mp4')],capture_output=True,timeout=120)
            if result.returncode:raise ValueError('音视频封装失败，日志保留。')
            reader=imageio.get_reader(run/'video.mp4')
            try:
                decoded=sum(1 for _ in reader)
            finally:reader.close()
            if decoded!=count:raise ValueError('输出帧数不匹配。')
            digest=hashlib.sha256((run/'video.mp4').read_bytes()).hexdigest()
            metrics=dict(sha256=digest,frames=count,fps=fps,width=w,height=h,duration_seconds=count/fps,replaced_frames=changed,start_frame=start,end_frame_exclusive=end,elapsed_seconds=round(time.perf_counter()-started,3),ai_generated=False,source_sha256=a['sha256'],replacement_sha256=b['sha256'],audio_policy=p['audio_policy'],limitation=p['render_note'])
            (run/'record.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2))
            store.update(eid,'succeeded',metrics)
        except Exception as error:
            (store.root/'logs'/('edit-'+eid+'.log')).write_text(type(error).__name__+': '+str(error))
            store.update(eid,'failed',error='片段剪辑失败；原素材保留，请管理员检查任务日志。')
        return store.get(eid)


def install_edits(app,root,web_root):
    store=EditStore(root)
    @app.get('/edits',include_in_schema=False)
    def page():return FileResponse(web_root/'edits.html',media_type='text/html')
    router=APIRouter(prefix='/api/edits')
    @router.get('/capabilities')
    def capabilities():return {'temporal_selection':True,'replacement_composition':True,'ai_local_reshoot':False,'note':'当前模型适配器未接通时间局部重拍。H3参考视频接口不等于保证选区外逐帧不变。'}
    @router.get('')
    def listing():return {'items':store.list()}
    @router.post('',status_code=202)
    def create(body:EditRequest,idempotency_key:str=Header(...)):
        if not re.fullmatch('[A-Za-z0-9_-]{1,80}',idempotency_key):raise HTTPException(400,'提交标识无效。')
        try:record,queued=store.prepare(body,idempotency_key)
        except ValueError as error:raise HTTPException(409,str(error))
        if queued:
            try:
                with (root/'logs/edit-worker.log').open('a') as stream:
                    subprocess.Popen([sys.executable,'-u','-m','video_platform.edits','--id',record['id'],'--owner',owner_id()],cwd=root,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
            except OSError:store.update(record['id'],'failed',error='剪辑进程启动失败，未调用AI模型。')
        return store.get(record['id'])
    @router.get('/{eid}')
    def get(eid:str):
        record=store.get(eid)
        if not record:raise HTTPException(404,'编辑版本不存在。')
        return record
    @router.get('/{eid}/video')
    def video(eid:str,download:bool=False):
        record=store.get(eid)
        if not re.fullmatch('[0-9a-f]{32}',eid) or not record or record['status']!='succeeded':raise HTTPException(404,'视频不存在。')
        return FileResponse(root/'runs'/('edit-'+eid)/'video.mp4',media_type='video/mp4',filename='frameseq-edit.mp4' if download else None)
    app.include_router(router)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--id',required=True);parser.add_argument('--owner',required=True);args=parser.parse_args()
    with as_user({'id':args.owner,'role':'creator'}):render(EditStore(ROOT),args.id)
