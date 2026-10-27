"""One coordinator, one inference subprocess at a time; release GPU after each task."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .inference import project_inference_active, require_gpu_available, write_json
from .store import ROOT, TaskStore, now
from .h3_runtime import H3_ID,H3_PROFILE,H3_PROFILE_ID,h3_ready,runtime_config
from .model_profiles import COG_PROFILE,COG_PROFILE_ID,WAN_NATIVE,WAN_NATIVE_ID
from .gpu_policy import temperature_control_enabled


def matching_processes(job_id):
    """Identify only our own inference process, including after worker restart."""
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = (entry / "cmdline").read_bytes().decode().split("\0")
            if (entry/'cwd').resolve()!=ROOT.resolve():
                continue
            if (any(module in args for module in ("video_platform.inference","video_platform.cogvideo","video_platform.h3_local")) or any(Path(part).name in {'h3_local_experiment.py','h3_dual_sample.py','h3_turbo_sample.py'} for part in args)) and "--job-id" in args:
                if args[args.index("--job-id") + 1] == job_id:
                    matches.append(int(entry.name))
        except (OSError, UnicodeError, IndexError):
            continue
    return matches


def read_result(job_id):
    try:
        return json.loads((ROOT / "runs" / job_id / "result.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def status(state, **extra):
    write_json(ROOT / "data/worker.json", {"state": state, "updated_at": now(), "pid": os.getpid(), "models":['local-wan','local-cogvideo']+(['local-h3'] if h3_ready(ROOT) else []),"profiles":[COG_PROFILE_ID,WAN_NATIVE_ID]+([H3_PROFILE_ID] if h3_ready(ROOT) else []), **extra})


def idle_gpu_choice(rows, maximum_temperature):
    idle=sorted((temperature,index) for index,memory,util,temperature in rows
                if not memory and not util and (not temperature_control_enabled() or temperature<maximum_temperature))
    if not idle:raise RuntimeError(f'暂无空闲且低于{maximum_temperature}℃的GPU，任务保留在队列中。')
    return idle[0][1]


def next_gpu_temperature_limit(store):
    with store.connect() as db:
        row=db.execute("SELECT id,parameters FROM tasks WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
    params=json.loads(row['parameters']) if row else {}
    if row and params.get('model_id')==H3_ID and (store.path.parent.parent/'runs'/('task-'+row['id'])/'resume-from-sample.json').exists():return 78
    return 59 if params.get('model_id')==H3_ID else 68


def next_task_is_h3(store):
    with store.connect() as db:
        row=db.execute("SELECT parameters FROM tasks WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
    return bool(row and json.loads(row['parameters']).get('model_id')==H3_ID)


def dual_gpu_choice(rows,allowed=(0,1),maximum_temperature=65):
    if len(set(allowed))!=2:raise ValueError('Two distinct permitted H3 GPUs are required')
    found={i:(m,u,t) for i,m,u,t in rows if i in allowed}
    if len(found)!=2 or any(m or u or (temperature_control_enabled() and t>=maximum_temperature) for m,u,t in found.values()):
        raise RuntimeError('双卡H3等待两张允许使用且完全空闲的显卡；不会占用他人显存。')
    return sorted(allowed)


def monitor(store, task, child=None):
    job_id = "task-" + task["id"]
    missing_since = None
    while True:
        result = read_result(job_id)
        if result and result.get("last_event", {}).get("event") == "step":
            event = result["last_event"]
            store.progress(task["id"], min(99, int(100 * event["completed_steps"] / event["total_steps"])))
        alive = child.poll() is None if child is not None else bool(matching_processes(job_id))
        # Wait for the process to exit before accepting its final result, so the
        # next task never overlaps with CUDA teardown from this one.
        if not alive:
            if result and result.get('status')=='deferred':
                store.defer_decode(task['id'])
                return
            if result and result.get("status") == "succeeded":
                try:
                    checked = subprocess.run([sys.executable, '-m', 'scripts.validate_video',
                                              str(ROOT / "runs" / job_id)],
                                             capture_output=True, text=True, timeout=60, check=True)
                    validation = json.loads(checked.stdout)
                    metrics = {k: result[k] for k in (
                        "generation_seconds", "model_load_seconds", "prompt_encoding_seconds",
                        "sampling_and_decode_seconds", "peak_gpu_allocated_bytes", "peak_gpu_reserved_bytes",
                        "video_bytes", "precision", "model_id")}
                    metrics["validation"] = validation
                    if 'quality_checks' in result:metrics['quality_checks']=result['quality_checks']
                    if 'vae_precision' in result:metrics['vae_precision']=result['vae_precision']
                    for key in ('resumed_from_sample','stage_gpus','elapsed_seconds','physical_gpus','parallelism','same_video_sequence_parallel','rank_metrics'):
                        if key in result:metrics[key]=result[key]
                    for key in ('inference_profile','scheduler'):
                        if key in task['parameters']:metrics[key]=task['parameters'][key]
                    store.finish(task["id"], "succeeded", metrics=metrics)
                except Exception as error:
                    store.finish(task["id"], "failed", error=f"结果解码验收失败：{error}")
                return
            if result and result.get("status") == "failed":
                store.finish(task["id"], "failed", error=result.get("error", "推理失败"))
                return
            if missing_since is None:
                missing_since = time.monotonic()
            if time.monotonic() - missing_since > 5:
                exit_code = child.returncode if child is not None else None
                message = "执行进程已退出且没有完整结果；保留日志，可重新提交。"
                if exit_code is not None:
                    message += f" 进程退出码：{exit_code}。"
                # Keep the interrupted model's last observation intact. This
                # separate record belongs to the coordinator, not the model.
                run_dir = ROOT / "runs" / job_id
                run_dir.mkdir(exist_ok=True)
                write_json(run_dir / "worker-outcome.json", {
                    "status": "failed", "observed_at": now(), "task_id": task["id"],
                    "reason": "process_exited_without_terminal_result", "exit_code": exit_code,
                    "exit_code_known": child is not None,
                    "last_model_status": result.get("status") if result else None,
                    "error": message,
                })
                store.finish(task["id"], "failed", error=message)
                return
        else:
            missing_since = None
        status("running", task_id=task["id"])
        time.sleep(1)


def inference_command(task,gpu,precision='float16',h3_gpus=(0,1)):
    params = task["parameters"]
    job_id = "task-" + task["id"]
    if params.get('model_id')==H3_ID:
        if any(params.get(k)!=v for k,v in H3_PROFILE.items()):
            raise ValueError('H3 inference profile drift; no silent fallback.')
        picked=sorted(h3_gpus)
        if len(set(picked))!=2 or gpu!=picked[0]:raise ValueError('Invalid H3 dual GPU leader/allocation')
        command=[runtime_config(ROOT)['python'],'-u','-m','video_platform.h3_local','--gpu',str(gpu),'--gpus',*[str(g) for g in picked],'--job-id',job_id]
        if (ROOT/'runs'/job_id/'resume-from-sample.json').exists():command.append('--resume-from-sample')
        return command
    if params.get('model_id') not in {None,'Wan-AI/Wan2.1-T2V-1.3B-Diffusers','zai-org/CogVideoX-2b'}:
        raise ValueError('Unknown model ID; do not silently route it to Wan.')
    module='video_platform.cogvideo' if params.get('model_id')=='zai-org/CogVideoX-2b' else 'video_platform.inference'
    if module=='video_platform.cogvideo':
        if any(params.get(k)!=v for k,v in COG_PROFILE.items()):
            raise ValueError('CogVideoX inference profile is missing or has drifted; do not silently substitute settings.')
        precision=params['precision']
    elif params.get('preset')=='cinema' or params.get('inference_profile')==WAN_NATIVE_ID:
        if any(params.get(k)!=v for k,v in WAN_NATIVE.items()):
            raise ValueError('Wan native480 profile has drifted; no silent parameter substitution.')
        precision=params['precision']
    command = [sys.executable, "-u", "-m", module, "--gpu", str(gpu),
               "--job-id", job_id, "--precision", precision,
               "--prompt=" + params["prompt"], "--negative-prompt=" + params["negative_prompt"]]
    for field, option in [("width", "width"), ("height", "height"), ("frames", "frames"),
                          ("steps", "steps"), ("seed", "seed"), ("guidance", "guidance"),
                          ("fps", "fps"), ("max_sequence_length", "max-sequence-length")]:
        command.extend(["--" + option, str(params[field])])
    if module=='video_platform.cogvideo':
        command.extend(['--scheduler',params['scheduler'],'--vae-precision',params['vae_precision']])
    return command


def run(store, task, gpu, precision="float32",h3_gpus=(0,1)):
    job_id='task-'+task['id']
    try:
        command=inference_command(task,gpu,precision,h3_gpus=h3_gpus)
        if task['parameters'].get('model_id')==H3_ID:
            directory=ROOT/'runs'/job_id
            directory.mkdir(parents=True,exist_ok=True)
            write_json(directory/'request.json',{'parameters':task['parameters'],'task_id':task['id']})
        with (ROOT / "logs" / f"{job_id}.log").open("a") as stream:
            child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        monitor(store, task, child)
    except Exception as error:
        # If a child did start, leave its record running for restart recovery.
        if matching_processes(job_id):
            raise
        store.finish(task["id"], "failed", error=f"任务启动或监控失败：{error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--auto-gpu",action="store_true",help="Recheck and select one idle card for each task")
    parser.add_argument('--h3-gpus',type=int,nargs=2,default=[0,1],help='Permitted physical GPU pair for same-video H3 sequence parallel')
    parser.add_argument("--precision", choices=["float32", "float16"], default="float32")
    args = parser.parse_args()
    (ROOT / "logs").mkdir(exist_ok=True)
    lock = (ROOT / "logs/queue-worker.lock").open("a")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = TaskStore()
    # Recover or reattach to a running child, never blindly enqueue it again.
    for task in store.running():
        monitor(store, task)
    while True:
        try:paused=json.loads((ROOT/'data/generation-paused.json').read_text()).get('enabled',False)
        except FileNotFoundError:paused=False
        except (OSError,ValueError):paused=True
        if paused:
            status('maintenance',message='生成队列维护中，已有任务和作品保留。')
            if args.once:return
            time.sleep(5);continue
        if not store.has_queued():
            status("idle", gpu=args.gpu, precision=args.precision)
            if args.once:
                status("stopped", gpu=args.gpu)
                return
            time.sleep(2)
            continue
        try:
            if project_inference_active():
                raise RuntimeError("本项目已有模型推理任务，排队等待它结束。")
            selected_gpu=args.gpu
            if args.auto_gpu or next_task_is_h3(store):
                probe=subprocess.run(['nvidia-smi','--query-gpu=index,memory.used,utilization.gpu,temperature.gpu',
                    '--format=csv,noheader,nounits'],capture_output=True,text=True,check=True,timeout=15)
                rows=[tuple(map(int,line.split(','))) for line in probe.stdout.splitlines() if line.strip()]
                selected_gpu=dual_gpu_choice(rows,args.h3_gpus)[0] if next_task_is_h3(store) else idle_gpu_choice(rows,next_gpu_temperature_limit(store))
            require_gpu_available(selected_gpu)
        except Exception as error:
            status("waiting_for_gpu", gpu=args.gpu, message=str(error))
            if args.once:
                return
            time.sleep(5)
            continue
        task = store.claim()
        if task:
            status("running", task_id=task["id"], gpu=selected_gpu)
            run(store, task, selected_gpu, args.precision,h3_gpus=args.h3_gpus)
        else:
            status("idle", gpu=args.gpu, precision=args.precision)
        if args.once:
            status("stopped", gpu=args.gpu)
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
