"""One real H3 task, with isolated per-stage files and inherited inference locks."""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import traceback
from datetime import datetime
import uuid

from .config import ROOT
from .h3_runtime import H3_PROFILE, H3_PROFILE_ID, runtime_config
from .inference import require_gpu_available, write_json, utc_now
from .gpu_policy import temperature_control_enabled

STAGES = ('encode', 'sample', 'decode')

def wait_for_idle(gpu, pause=time.sleep, attempts=3):
    # NVML can still report activity briefly after a CUDA child has exited.
    for _ in range(attempts):
        try:require_gpu_available(gpu);return True
        except Exception:pause(2)
    return False


def wait_for_dual_idle(gpus,pause=time.sleep,attempts=60):
    # Encoding may heat the leader above the dual sampler's <65C admission.
    # Hold our existing locks while waiting; never relax the sampler preflight.
    for _ in range(attempts):
        try:
            snapshots=[require_gpu_available(g) for g in gpus]
            if not temperature_control_enabled(ROOT) or all(s['temperature_c']<65 for s in snapshots):return True
        except Exception:pass
        pause(5)
    return False

def decode_resource_failure(stage, report, stages):
    return (stage=='decode' and report.get('status')=='failed' and stages.get('sample',{}).get('status')=='passed'
            and not report.get('torch_version')
            and 'is occupied or hot; experiment not started' in report.get('error',''))

def failure_message(stage, detail):
    if 'GPU reached 80C' in detail or 'GPU reached 82C' in detail:
        limit='82℃紧急停止线' if 'GPU reached 82C' in detail else '温度保护阈值'
        return f'显卡触及{limit}，触发温度保护，本地H3已停止；没有调用云API，未生成完整视频。'
    if 'cooling timeout' in detail.lower() or 'did not cool' in detail.lower():
        return '显卡未能在限定时间内降温，本地H3已停止；任务日志保留，没有调用云API。'
    label={'encode':'描述编码','sample':'视频采样','decode':'音视频解码'}.get(stage,'执行')
    return f'本地H3的{label}未完成，详细原因已保存在任务日志；没有调用云API。'


def stage_failure_detail(directory, report):
    """The supervisor's initiating error outranks SIGTERM cleanup in either rank."""
    path=Path(directory)/'supervisor-failure.json'
    try:
        if path.stat().st_size<=1024*1024:
            supervisor=json.loads(path.read_text())
            if supervisor.get('status')=='failed' and isinstance(supervisor.get('error'),str):
                return supervisor['error'][-20000:]
    except (OSError,ValueError,TypeError):pass
    return str(report.get('error') or 'inspect task stage log')[-20000:]

def stage_command(config, stage, directory, gpu, job_id, parameters, lock_fds, previous=None, gpus=None):
    if stage=='sample' and gpus is not None:
        if len(set(gpus))!=2 or len(lock_fds)!=3:raise ValueError('SP2 requires two distinct GPUs and three locks')
        args=[config['python'],'-u',config['dual_runner'],'--gpus',*[str(g) for g in gpus],
              '--job-id',job_id,'--run-dir',str(directory),'--prompt='+parameters['prompt'],
              '--seed',str(parameters['seed']),'--conditioning',previous['conditioning']]
        for descriptor in lock_fds:args.extend(['--inference-lock-fd',str(descriptor)])
        return args
    args=[config['python'],'-u',config['runner'],stage,'--gpu',str(gpu),
          '--job-id',job_id,'--run-dir',str(directory),'--prompt='+parameters['prompt'],
          '--seed',str(parameters['seed'])]
    for descriptor in lock_fds: args.extend(['--inference-lock-fd',str(descriptor)])
    if stage=='sample': args.extend(['--conditioning',previous['conditioning']])
    if stage=='decode': args.extend(['--latents',previous['latents']])
    return args

def validate_sample_report(sample,params):
    if any(sample.get(k)!=params[k] for k in ('width','height','frames','steps','sampler','scheduler','video_shift','audio_shift')):
        raise ValueError('H3 sampler did not execute the requested Turbo8 profile.')
    if sample.get('inference_profile')!=H3_PROFILE_ID or not sample.get('all_ranks_completed'):
        raise ValueError('H3 Turbo8 profile or rank completion differs.')
    for rank in sample.get('ranks',[]):
        if (rank.get('lora',{}).get('sha256')!=params['lora_sha256'] or
            rank.get('lora',{}).get('strength')!=params['lora_strength'] or
            not rank.get('lora_applied_exactly_once')):
            raise ValueError('H3 Turbo8 LoRA provenance differs.')
    if len(sample.get('ranks',[]))!=2:raise ValueError('H3 Turbo8 requires two rank reports.')


def run(job_id, gpu, config=None, resume_from_sample=False, gpus=None):
    gpus=sorted(gpus or [])
    if len(set(gpus))!=2 or gpu!=gpus[0] or any(g not in range(6) for g in gpus):raise ValueError('H3 SP2 needs two distinct physical GPUs, leader first')
    if not re.fullmatch(r'task-[0-9a-f]{32}',job_id): raise ValueError('Invalid task ID')
    run_dir=ROOT/'runs'/job_id
    run_dir.mkdir(parents=True,exist_ok=True)
    record=json.loads((run_dir/'request.json').read_text())
    params=record['parameters']
    if any(params.get(key)!=value for key,value in H3_PROFILE.items()):
        raise ValueError('H3 fixed inference contract differs; no silent substitution.')
    config=config or runtime_config(ROOT)
    old={}
    if (run_dir/'result.json').exists():
        old=json.loads((run_dir/'result.json').read_text())
        if old.get('status')=='succeeded':raise ValueError('Completed video is immutable; no rerun')
        if resume_from_sample:shutil.copyfile(run_dir/'result.json',run_dir/('prior-result-'+uuid.uuid4().hex[:8]+'.json'))
    prior_seconds=old.get('generation_seconds',0) if resume_from_sample else 0
    result={'status':'running','parameters':params,'model_id':H3_PROFILE['model_id'],
            'precision':'float16','vae_precision':'float16','physical_gpu':gpu,'physical_gpus':gpus,
            'parallelism':'sequence','same_video_sequence_parallel':True,
            'started_at':old.get('started_at',utc_now()) if resume_from_sample else utc_now(),
            'new_paid_requests':0,'inference_profile':H3_PROFILE_ID,'resumed_from_sample':resume_from_sample}
    started=time.monotonic()
    def update(**values):
        result.update(values);result['updated_at']=utc_now()
        write_json(run_dir/'result.json',result)
    update(stage='waiting_for_gpu',stage_label='检查空闲GPU')
    try:
        with contextlib.ExitStack() as stack:
            handles=[]
            for filename in ['model-inference.lock']+[f'gpu-{g}.lock' for g in gpus]:
                handle=stack.enter_context((ROOT/'logs'/filename).open('a'))
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                handles.append(handle)
            stages={};report_paths={}
            # Each task gets a private directory under the configured H3 lab root.
            stage_root=Path(config['lab_root'])/'runs'/job_id
            stage_root.mkdir(parents=True,exist_ok=resume_from_sample)
            previous=None
            if resume_from_sample:
                marker=json.loads((run_dir/'resume-from-sample.json').read_text())
                for stage in ('encode','sample'):
                    path=stage_root/stage/'report.json';stages[stage]=json.loads(path.read_text());report_paths[stage]=str(path)
                    if stages[stage]['status']!='passed':raise ValueError('Checkpoint is not a completed stage')
                previous=stages['sample']
                if marker['source_sample_run_id']!=previous['run_id'] or previous['prompt']!=params['prompt'] or previous['seed']!=params['seed']:
                    raise ValueError('Checkpoint provenance differs from this task')
                if any(previous.get(k)!=params[k] for k in ('width','height','frames','steps')):raise ValueError('Checkpoint profile differs')
                result['last_event']={'event':'step','completed_steps':params['steps'],'total_steps':params['steps']}
            for stage in (('decode',) if resume_from_sample else STAGES):
                directory=stage_root/(stage+'-resume-'+uuid.uuid4().hex[:8] if resume_from_sample else stage)
                # The second card is cooler on the current permitted pair. Keep
                # text encoding and AV decoding off the hotter SP leader.
                stage_gpu=gpu if stage=='sample' else gpus[1]
                update(stage='waiting_for_gpu',stage_label='等待显卡资源就绪')
                ready=wait_for_dual_idle(gpus) if stage=='sample' else wait_for_idle(stage_gpu)
                if not ready and stage=='decode' and stages.get('sample',{}).get('status')=='passed':
                    attempts=json.loads((run_dir/'resume-from-sample.json').read_text()).get('attempts',0) if (run_dir/'resume-from-sample.json').exists() else 0
                    if attempts>=5:raise RuntimeError('No idle GPU for decode after five attempts; sampling checkpoint retained')
                    write_json(run_dir/'resume-from-sample.json',{'source_sample_run_id':stages['sample']['run_id'],'attempts':attempts+1})
                    update(status='deferred',stage='waiting_for_gpu',stage_label='采样完成，等待空闲资源解码',generation_seconds=prior_seconds+round(time.monotonic()-started,3))
                    return
                if not ready:raise RuntimeError('GPU is not idle; task has not started this stage')
                inherited=[h.fileno() for h in handles] if stage=='sample' else [handles[0].fileno(),handles[1+gpus.index(stage_gpu)].fileno()]
                cmd=stage_command(config,stage,directory,stage_gpu,job_id,params,inherited,previous,gpus=gpus if stage=='sample' else None)
                update(stage=stage,stage_label={'encode':'编码描述','sample':'本地H3采样','decode':'解码音视频'}[stage])
                with (run_dir/f'{stage}.log').open('w') as stream:
                    child=subprocess.Popen(cmd,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,
                        pass_fds=tuple(h.fileno() for h in handles))
                    try:
                        while child.poll() is None:
                            path=directory/'report.json'
                            try: progress=json.loads(path.read_text())
                            except (OSError, ValueError): progress={}
                            event=None
                            if 'step' in progress:
                                event={'event':'step','completed_steps':progress['step'],'total_steps':progress['total_steps']}
                            updates={'stage_detail':progress.get('stage'),'temperature_c':progress.get('temperature_c'),
                                     'temperature_by_gpu_c':progress.get('temperature_by_gpu_c'),
                                     'cooling':progress.get('stage') in ('cooling_between_steps','cooling_between_blocks')}
                            if event is not None: updates['last_event']=event
                            update(**updates)
                            time.sleep(2)
                    except BaseException:
                        # Stop only this exact child; never kill a shared GPU process by name.
                        child.terminate()
                        try: child.wait(timeout=20)
                        except subprocess.TimeoutExpired: child.kill();child.wait()
                        raise
                previous=json.loads((directory/'report.json').read_text())
                if decode_resource_failure(stage,previous,stages):
                    attempts=json.loads((run_dir/'resume-from-sample.json').read_text()).get('attempts',0) if (run_dir/'resume-from-sample.json').exists() else 0
                    if attempts>=5:raise RuntimeError('Decode resource retry limit reached; sampling checkpoint retained')
                    write_json(run_dir/'resume-from-sample.json',{'source_sample_run_id':stages['sample']['run_id'],'attempts':attempts+1})
                    update(status='deferred',stage='waiting_for_gpu',stage_label='采样完成，等待空闲资源解码',generation_seconds=prior_seconds+round(time.monotonic()-started,3))
                    return
                if child.returncode or previous.get('status')!='passed':
                    raise RuntimeError(f'H3 {stage} failed: '+stage_failure_detail(directory,previous))
                if stage=='sample':validate_sample_report(previous,params)
                stages[stage]=previous
                report_paths[stage]=str(directory/'report.json')
            decoded=stages['decode']
            if decoded.get('source_run_id')!=stages['sample']['run_id'] or decoded.get('smoke_only'):
                raise ValueError('Decoded video provenance does not match this full task.')
            source=Path(decoded['video']).resolve()
            if not source.is_relative_to(stage_root.resolve()): raise ValueError('Video escaped this task directory')
            shutil.copyfile(source,run_dir/'video.mp4')
            sample=stages['sample']
            update(status='succeeded',stage='completed',stage_label='已完成音视频解码',cooling=False,
                generation_seconds=prior_seconds+round(time.monotonic()-started,3),model_load_seconds=None,
                physical_gpu=sample['physical_gpu'],physical_gpus=gpus,
                stage_gpus={stage:r.get('physical_gpus',[r['physical_gpu']]) for stage,r in stages.items()},
                rank_metrics=[{k:r.get(k) for k in ('rank','physical_gpu','forward_seconds','forward_count','cooldown_total_seconds','peak_allocated_bytes','peak_reserved_bytes')} for r in sample.get('ranks',[])],
                elapsed_seconds=(datetime.fromisoformat(utc_now())-datetime.fromisoformat(result['started_at'])).total_seconds(),
                prompt_encoding_seconds=stages['encode']['elapsed_seconds'],
                sampling_and_decode_seconds=sample['elapsed_seconds']+decoded['elapsed_seconds'],
                peak_gpu_allocated_bytes=max(s['peak_allocated_bytes'] for s in stages.values()),
                peak_gpu_reserved_bytes=max(s['peak_reserved_bytes'] for s in stages.values()),
                video_bytes=(run_dir/'video.mp4').stat().st_size,video_sha256=decoded['video_sha256'],
                stage_reports=report_paths,
                finished_at=utc_now())
    except Exception:
        detail=traceback.format_exc()
        update(status='failed',stage='failed',error=failure_message(result.get('stage'),detail),
            error_detail=detail,generation_seconds=prior_seconds+round(time.monotonic()-started,3))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--job-id',required=True)
    parser.add_argument('--gpu',required=True,type=int)
    parser.add_argument('--gpus',type=int,nargs=2,required=True)
    parser.add_argument('--resume-from-sample',action='store_true')
    args=parser.parse_args()
    run(args.job_id,args.gpu,resume_from_sample=args.resume_from_sample,gpus=args.gpus)

if __name__=='__main__': main()
