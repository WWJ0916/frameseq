"""Lightweight H3 contract; no torch import or provider calls in the API."""
import json
from pathlib import Path

H3_ID = 'MiniMax-AI/MiniMax-H3'
H3_PROFILE_ID = 'h3-fl2va-fp16-sp2-turbo8-cached-blockguard79-pair02-v5'
H3_LORA_SHA = '2339acdf19bfe123f46b971ea35d367a84adb85de43627e1eceafa5a5b2b111e'
H3_PROFILE = dict(model_id=H3_ID, inference_profile=H3_PROFILE_ID,
                  parallelism='sequence', gpu_count=2,
                  precision='float16', vae_precision='float16', audio_vae_precision='float32',
                  width=864, height=480, frames=124, fps=24, steps=8, guidance=1,
                  sampler='euler', scheduler='simple', negative_prompt='',
                  lora_sha256=H3_LORA_SHA,lora_strength=1.0,video_shift=12.0,audio_shift=3.0)

def runtime_config(root):
    root = Path(root).resolve()
    record = json.loads((root/'data/h3-local-runtime.json').read_text())
    for key in ('python', 'runner', 'lab_root', 'dual_runner'):
        path = Path(record[key]).resolve(strict=True)
        if not path.is_relative_to(root.parent):
            raise ValueError('H3 runtime paths must remain inside the operator workspace.')
        # Preserve the venv's executable symlink: resolving argv[0] changes sys.prefix.
        record[key] = str(Path(record[key]).absolute())
    if not all(Path(record[key]).is_file() for key in ('python','runner','dual_runner')):
        raise ValueError('H3 executor is missing.')
    if record.get('inference_profile')!=H3_PROFILE_ID or record.get('lora_sha256')!=H3_LORA_SHA:
        raise ValueError('H3 Turbo8 runtime profile or LoRA differs.')
    return record

def h3_verified(root):
    try:
        proof=json.loads((Path(root)/'logs/h3-local-verified.json').read_text())
        return proof.get('status')=='passed' and proof.get('inference_profile')==H3_PROFILE_ID
    except (OSError, ValueError, TypeError):
        return False

def h3_ready(root):
    try:
        runtime_config(root)
        return h3_verified(root)
    except (OSError, ValueError, KeyError, TypeError):
        return False

def h3_catalog(root, worker_profiles=()):
    return dict(id='local-h3',name='MiniMax H3 · 本地实验',provider='本地部署',
                execution='self_hosted',verified=h3_verified(root),preview=True,
                configured=h3_ready(root) and H3_PROFILE_ID in worker_profiles,
                inference_profile=H3_PROFILE_ID,
                description='Turbo LoRA 8步；双卡共同计算同一条视频。',
                specification='864×480 · 124帧 · 24fps · 约5.17秒 · Turbo 8步',
                cost_hint='不调用云API；本地加载、计算、通信和解码计入实际耗时，排队另计',
                unavailable_reason='本地H3环境、验收记录或执行器尚未就绪')
