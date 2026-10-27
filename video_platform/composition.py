"""CPU episode editing of explicitly selected, verified footage; never generate or pad missing video."""
from contextlib import contextmanager
import argparse,fcntl,hashlib,json,math,os,re,sqlite3,subprocess,time,uuid,wave
from pathlib import Path
from .store import ROOT,TaskStore,now
from .projects import ProjectStore
from .ownership import owner_id,as_user

class ExportStore:
    def __init__(self,root=ROOT):
        self.root=Path(root);self.path=self.root/'data/projects.sqlite3'
        ProjectStore(root)
        with self.connect() as db:db.execute('''CREATE TABLE IF NOT EXISTS exports (
          id TEXT PRIMARY KEY,owner_id TEXT,project_id TEXT,episode_id TEXT,fingerprint TEXT UNIQUE,
          status TEXT,plan TEXT,result TEXT,error TEXT,created_at TEXT,updated_at TEXT)''')
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        except Exception:db.rollback();raise
        finally:db.close()
    def decode(self,row):
        if not row:return None
        d=dict(row);d['plan']=json.loads(d['plan']);d['result']=json.loads(d['result']) if d['result'] else None;d.pop('fingerprint');return d
    def get(self,eid):
        with self.connect() as db:return self.decode(db.execute('SELECT * FROM exports WHERE id=? AND owner_id=?',(eid,owner_id())).fetchone())
    def list(self,pid):
        with self.connect() as db:return [self.decode(r) for r in db.execute('SELECT * FROM exports WHERE project_id=? AND owner_id=? ORDER BY created_at DESC',(pid,owner_id()))]
    def prepare(self,pid,eid,revision):
        project=ProjectStore(self.root).get(pid)
        if not project:raise LookupError('项目不存在。')
        if project['revision']!=revision:raise ValueError('项目已更新，请重新打开后再导出。')
        episode=next((e for e in project['episodes'] if e['id']==eid),None)
        if not episode or not episode['shots']:raise ValueError('请先安排本集分镜。')
        from .cloud import CloudStore
        clips=[]
        for shot in episode['shots']:
            if len(shot['narration'] or shot['title'])>60:
                raise ValueError('导出字幕每个镜头最多60字，请先精简旁白。')
            chosen=next((v for v in project['versions'] if v['shot_id']==shot['id'] and v['episode_id']==eid and v['selected']),None)
            if not chosen:raise ValueError('镜头「'+shot['title']+'」尚未选择采用版本。')
            from .video_api_store import VideoAPIStore
            task=CloudStore(self.root).get(chosen['task_id']) if chosen['origin']=='cloud' else VideoAPIStore(self.root).get(chosen['task_id']) if chosen['origin']=='api' else TaskStore(self.root/'data/tasks.sqlite3').get(chosen['task_id'])
            if not task or task['status']!='succeeded':raise ValueError('选片包含尚未成功的任务。')
            metrics=(task.get('result') if chosen['origin']=='cloud' else task.get('metrics')) or {}
            if metrics.get('visual_review',{}).get('approved') is False:
                raise ValueError('选片包含已标记画面异常的版本，请重新选择。')
            check=metrics.get('validation') if isinstance(metrics.get('validation'),dict) else metrics
            actual=check.get('duration_seconds')
            if not actual or actual+.01<shot['seconds']:
                raise ValueError(f"镜头「{shot['title']}」实际素材不足{shot['seconds']}秒，请缩短计划或生成足够长的素材，不会自动补静帧。")
            clips.append(dict(shot_id=shot['id'],title=shot['title'],narration=shot['narration'],seconds=shot['seconds'],origin=chosen['origin'],task_id=task['id'],input_sha256=check['sha256'],input_actual_seconds=actual,model=metrics.get('model_id') or task.get('parameters',{}).get('model') or 'Wan2.1-T2V-1.3B'))
        plan=dict(title=project['title'],episode_title=episode['title'],project_revision=project['revision'],clips=clips,width=960,height=544,fps=16,total_seconds=sum(s['seconds'] for s in clips),captions=True,soundtrack='original_program_synthesized_tones_not_AI_voice')
        fingerprint=hashlib.sha256(json.dumps([owner_id(),pid,eid,plan],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM exports WHERE fingerprint=?',(fingerprint,)).fetchone()
            if old:return self.decode(old),False
            if db.execute("SELECT count(*) FROM exports WHERE status IN ('queued','running')").fetchone()[0]>=2:raise ValueError('已有剪辑任务在处理，请稍后再试。')
            export_id=uuid.uuid4().hex;stamp=now()
            db.execute('INSERT INTO exports VALUES(?,?,?,?,?,?,?,?,?,?,?)',(export_id,owner_id(),pid,eid,fingerprint,'queued',json.dumps(plan,ensure_ascii=False),None,None,stamp,stamp))
            return self.decode(db.execute('SELECT * FROM exports WHERE id=?',(export_id,)).fetchone()),True
    def update(self,eid,status,result=None,error=None):
        with self.connect() as db:db.execute('UPDATE exports SET status=?,result=?,error=?,updated_at=? WHERE id=? AND owner_id=?',(status,json.dumps(result,ensure_ascii=False) if result else None,error,now(),eid,owner_id()))

def font_path(root):
    for path in [root/'assets/NotoSansCJKsc-Regular.otf',Path('/usr/share/fonts/truetype/arphic/ukai.ttc')]:
        if path.is_file():return path
    raise RuntimeError('字幕字体尚未准备；不生成缺字字幕。')

def make_soundtrack(path,seconds):
    import numpy as np
    rate=24000;n=int(seconds*rate);audio=np.zeros(n,dtype=np.float32)
    notes=[261.626,329.628,391.995,523.251,440,391.995,329.628,293.665]
    for i,start in enumerate(np.arange(0,seconds,1.25)):
        length=min(int(2.3*rate),n-int(start*rate));t=np.arange(length)/rate
        envelope=(1-np.exp(-t*18))*np.exp(-t*1.9)
        tone=(np.sin(2*np.pi*notes[i%len(notes)]*t)+.2*np.sin(4*np.pi*notes[i%len(notes)]*t))*.075*envelope
        audio[int(start*rate):int(start*rate)+length]+=tone.astype(np.float32)
    with wave.open(str(path),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(rate);f.writeframes((np.clip(audio,-.8,.8)*32767).astype('<i2').tobytes())

def compose(store,export_id):
    with (store.root/'logs/composition.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _compose_locked(store,export_id)

def _compose_locked(store,export_id):
    record=store.get(export_id)
    if not record or record['status']=='succeeded':return record
    if record['status']!='queued':raise RuntimeError('导出已有执行记录，不自动重复执行。')
    record=store.get(export_id)
    if record['status']!='queued':return record
    store.update(export_id,'running');started=time.perf_counter()
    run=store.root/'runs'/('episode-'+export_id);run.mkdir(exist_ok=False)
    try:
        import imageio.v2 as imageio
        import imageio_ffmpeg
        import numpy as np
        from PIL import Image,ImageDraw,ImageFont,ImageOps
        plan=record['plan'];fps=plan['fps'];width=plan['width'];height=plan['height']
        font=ImageFont.truetype(str(font_path(store.root)),27);small=ImageFont.truetype(str(font_path(store.root)),17)
        silent=run/'silent.mp4';writer=imageio.get_writer(str(silent),fps=fps,codec='libx264',quality=8,macro_block_size=None,ffmpeg_log_level='error')
        total=0;srt=[];clip_records=[]
        try:
            for index,clip in enumerate(plan['clips']):
                if not re.fullmatch('[0-9a-f]{32}',clip['task_id']):raise ValueError('Invalid task reference')
                source=store.root/'runs'/({'cloud':'cloud-','api':'video-api-'}.get(clip['origin'],'task-')+clip['task_id'])/'video.mp4'
                if hashlib.sha256(source.read_bytes()).hexdigest()!=clip['input_sha256']:raise RuntimeError('输入视频校验值改变，停止导出。')
                reader=imageio.get_reader(str(source),format='ffmpeg');source_fps=reader.get_meta_data()['fps'];frames=[]
                needed=int(clip['seconds']*fps)
                try:
                    for j,frame in enumerate(reader):
                        frames.append(frame)
                        if j>=math.floor((needed-1)*source_fps/fps):break
                finally:reader.close()
                if len(frames)<=math.floor((needed-1)*source_fps/fps):raise RuntimeError('输入帧数不足，不补静帧。')
                for j in range(needed):
                    original=Image.fromarray(frames[math.floor(j*source_fps/fps)]).convert('RGB')
                    fitted=ImageOps.contain(original,(width,height),Image.Resampling.LANCZOS)
                    image=Image.new('RGB',(width,height),(8,15,18));image.paste(fitted,((width-fitted.width)//2,(height-fitted.height)//2))
                    draw=ImageDraw.Draw(image,'RGBA');draw.rectangle((0,height-86,width,height),fill=(8,15,18,205))
                    text=clip['narration'] or clip['title'];lines=[text[k:k+30] for k in range(0,len(text),30)]
                    if len(lines)>2:raise ValueError('单镜头字幕过长，请缩短至60字以内。')
                    for line_i,line in enumerate(lines):
                        box=draw.textbbox((0,0),line,font=font);draw.text(((width-(box[2]-box[0]))/2,height-73+line_i*33),line,font=font,fill=(245,248,240,255))
                    draw.rectangle((16,14,360,46),fill=(8,15,18,145));draw.text((26,19),f"{plan['episode_title'][:15]}  /  {index+1:02d}",font=small,fill=(235,241,220,255))
                    if j==0:
                        image.save(run/f'shot-{index+1:02d}.jpg',quality=90)
                        if index==0:image.save(run/'poster.jpg',quality=92)
                    writer.append_data(np.asarray(image));total+=1
                def clock(seconds):
                    ms=round(seconds*1000);return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
                start=(total-needed)/fps;end=total/fps
                srt.append(f'{index+1}\n{clock(start)} --> {clock(end)}\n{text}\n')
                clip_records.append({**clip,'output_frames':needed,'output_start_seconds':start,'output_end_seconds':end,'input_fps':source_fps,'source_resolution':list(frames[0].shape[:2][::-1])})
        finally:writer.close()
        (run/'subtitles.srt').write_text('\n'.join(srt),encoding='utf-8')
        make_soundtrack(run/'soundtrack.wav',total/fps)
        ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(silent),'-i',str(run/'soundtrack.wav'),'-c:v','copy','-c:a','aac','-b:a','96k','-shortest','-movflags','+faststart',str(run/'video.mp4')],check=True,timeout=120)
        video=run/'video.mp4';reader=imageio.get_reader(str(video),format='ffmpeg');meta=reader.get_meta_data();count=0
        try:
            for frame in reader:
                if frame.shape!=(height,width,3):raise RuntimeError('成片尺寸异常。')
                count+=1
        finally:reader.close()
        assert count==total and abs(meta['fps']-fps)<.01
        source_sizes='、'.join(sorted({str(c['source_resolution'][0])+'×'+str(c['source_resolution'][1]) for c in clip_records}))
        result=dict(status='passed',export_id=export_id,shots=len(clip_records),frames=count,fps=fps,duration_seconds=count/fps,width=width,height=height,sha256=hashlib.sha256(video.read_bytes()).hexdigest(),bytes=video.stat().st_size,clips=clip_records,editing_seconds=round(time.perf_counter()-started,3),soundtrack='程序原创合成音符；没有AI配音',limitation=f'分镜草稿；源画面{source_sizes}，统一排版为960×544并保留比例，常规缩放不增加原生细节。跨镜头主体外观和运动一致性有限，非商业成片质量。')
        (run/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));store.update(export_id,'succeeded',result=result);return store.get(export_id)
    except Exception as e:
        store.update(export_id,'failed',error=str(e));raise

def main():
    p=argparse.ArgumentParser();p.add_argument('--export-id',required=True);p.add_argument('--owner-id',required=True);args=p.parse_args()
    if not re.fullmatch('[0-9a-f]{32}',args.export_id) or not re.fullmatch('[a-z0-9-]{1,40}',args.owner_id):p.error('Invalid identity')
    with as_user({'id':args.owner_id,'role':'creator'}):compose(ExportStore(),args.export_id)

if __name__=='__main__':main()
