"""Actual Wan inference, with CPU text encoding and one selected GPU."""

from __future__ import annotations

import argparse
import fcntl
import gc
import json
import os
import re
import subprocess
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


from .config import ROOT
from .gpu_policy import temperature_control_enabled
MODEL_DIR = ROOT / "models/Wan2.1-T2V-1.3B-Diffusers"
MODEL_ID = "Wan-AI/Wan2.1-T2V-1.3B-Diffusers"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def gpu_snapshot(index: int):
    result = subprocess.run(
        ["nvidia-smi", "-i", str(index),
         "--query-gpu=uuid,name,memory.used,utilization.gpu,driver_version,temperature.gpu",
         "--format=csv,noheader,nounits"],
        check=True, capture_output=True, text=True, timeout=15,
    )
    uuid, name, memory, utilization, driver, temperature = [part.strip() for part in result.stdout.strip().split(",")]
    return {"index": index, "uuid": uuid, "name": name, "memory_used_mib": int(memory),
            "utilization_percent": int(utilization), "driver": driver, "temperature_c":int(temperature),
            "query_warnings": result.stderr.strip()}


def require_gpu_available(index: int, allow_self=False):
    snapshot = gpu_snapshot(index)
    if snapshot["memory_used_mib"] == 0 and snapshot["utilization_percent"] == 0:
        if temperature_control_enabled(ROOT) and snapshot['temperature_c']>=78:
            raise RuntimeError(f"GPU {index} is cooling at {snapshot['temperature_c']}C; wait below78C before a new task.")
        return snapshot
    if allow_self:
        processes = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=15,
        )
        selected = [line.split(",") for line in processes.stdout.splitlines()
                    if line.split(",")[0].strip() == snapshot["uuid"]]
        if selected and all(parts[1].strip() == str(os.getpid()) for parts in selected):
            return snapshot
    raise RuntimeError(f"GPU {index} is occupied; no model task was started on it: {snapshot}")


def cool_between_steps(index,emit):
    """Pause only our sampler if it gets hot; never change fans, clocks or other tasks."""
    if not temperature_control_enabled(ROOT):return
    for attempt in range(24):
        snapshot=gpu_snapshot(index)
        if snapshot['temperature_c']<80:return
        emit({'event':'cooling','temperature_c':snapshot['temperature_c'],'pause_seconds':5,'utc':utc_now()})
        time.sleep(5)
    raise RuntimeError('GPU stayed at80C or above for two minutes; stopped this model safely and kept its logs.')


def project_inference_active():
    """Admission check across GPU indices; one project model job at a time."""
    with (ROOT / "logs/model-inference.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def validate_token_budget(tokenizer, prompt, negative_prompt, maximum):
    """Reject silent prompt truncation before the pipeline's bounded encoding."""
    for label, text in [("画面描述", prompt), ("负面描述", negative_prompt)]:
        count = len(tokenizer(text, truncation=False)["input_ids"])
        if count > maximum:
            raise ValueError(f"{label}编码长度{count}超过本地模型上限{maximum}，请精简后重新提交。没有开始视频采样。")


@dataclass
class GenerationParameters:
    prompt: str
    negative_prompt: str = "blurry, low quality, distorted, text, watermark, static image"
    width: int = 320
    height: int = 192
    frames: int = 17
    steps: int = 20
    guidance: float = 5.0
    seed: int = 42
    fps: int = 16
    max_sequence_length: int = 128

    def validate(self):
        if not self.prompt.strip() or len(self.prompt) > 2000:
            raise ValueError("Prompt must contain 1 to 2000 characters.")
        if not (128 <= self.width <= 832 and 128 <= self.height <= 480):
            raise ValueError("Unsupported dimensions for this single-GPU prototype.")
        if self.width % 16 or self.height % 16:
            raise ValueError("Width and height must be multiples of 16.")
        if not 5 <= self.frames <= 81 or (self.frames - 1) % 4:
            raise ValueError("Frame count must be 4*k+1, between 5 and 81.")
        if not 1 <= self.steps <= 50 or not 1.0 <= self.guidance <= 10.0:
            raise ValueError("Invalid steps or guidance.")
        if not 1 <= self.fps <= 30 or self.max_sequence_length not in {128, 256, 512}:
            raise ValueError("Invalid playback rate or text context length.")


class WanGenerator:
    """Keep the large text encoder on CPU and the video components on one GPU."""

    def __init__(self, gpu: int, precision="float32", emit: Callable | None = None):
        self.emit = emit or (lambda event: print(json.dumps(event), flush=True))
        self.gpu = require_gpu_available(gpu)
        import psutil
        if psutil.virtual_memory().available < 48 * 1024**3:
            raise RuntimeError("Less than 48GiB of system RAM is currently available.")
        os.environ["CUDA_VISIBLE_DEVICES"] = self.gpu["uuid"]
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        os.environ["OMP_NUM_THREADS"] = "4"
        os.environ["MKL_NUM_THREADS"] = "4"
        import torch

        # Establish an attributable CUDA context before loading libraries that
        # may initialize CUDA without a visible compute-process entry.
        require_gpu_available(gpu)
        self._context_probe = torch.empty(1, device="cuda:0")
        torch.cuda.synchronize(0)
        require_gpu_available(gpu, allow_self=True)
        import diffusers
        import transformers
        from diffusers import WanPipeline, UniPCMultistepScheduler

        self.torch = torch
        self.precision = precision
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        if not (ROOT / "logs/wan-download-complete.json").is_file():
            raise RuntimeError("Official model download has not passed file checks.")
        started = time.perf_counter()
        self.emit({"event": "loading_model", "utc": utc_now()})
        self.pipe = WanPipeline.from_pretrained(
            str(MODEL_DIR), torch_dtype=torch.float32,
            local_files_only=True, use_safetensors=True,
        )
        self.pipe.scheduler = UniPCMultistepScheduler.from_config(self.pipe.scheduler.config, flow_shift=3.0)
        self.encoder = self.pipe.text_encoder.eval()
        self.pipe.register_modules(text_encoder=None)
        require_gpu_available(gpu, allow_self=True)
        if precision == "float16":
            self.pipe.transformer.to(dtype=torch.float16)
        self.pipe.vae.to(dtype=torch.float32)
        self.pipe.vae.enable_tiling()
        self.pipe.to("cuda:0")
        self.pipe.set_progress_bar_config(disable=True)
        self.load_seconds = time.perf_counter() - started
        self.versions = {"torch": torch.__version__, "cuda_runtime": torch.version.cuda,
                         "diffusers": diffusers.__version__, "transformers": transformers.__version__}
        self.emit({"event": "model_loaded", "load_seconds": round(self.load_seconds, 3),
                   "precision": precision, "versions": self.versions, "utc": utc_now()})

    def generate(self, parameters: GenerationParameters, output_directory: Path):
        parameters.validate()
        import imageio_ffmpeg
        import numpy as np
        from PIL import Image
        from diffusers.utils import export_to_video

        torch = self.torch
        started = time.perf_counter()
        torch.cuda.reset_peak_memory_stats(0)
        self.emit({"event": "encoding_prompt", "utc": utc_now()})
        validate_token_budget(self.pipe.tokenizer, parameters.prompt,
                              parameters.negative_prompt, parameters.max_sequence_length)
        self.pipe.register_modules(text_encoder=self.encoder)
        try:
            with torch.inference_mode():
                embeddings, negative_embeddings = self.pipe.encode_prompt(
                    prompt=parameters.prompt, negative_prompt=parameters.negative_prompt,
                    do_classifier_free_guidance=parameters.guidance > 1,
                    max_sequence_length=parameters.max_sequence_length,
                    device=torch.device("cpu"), dtype=torch.float32,
                )
        finally:
            self.pipe.register_modules(text_encoder=None)
        text_seconds = time.perf_counter() - started
        dtype = self.pipe.transformer.dtype
        embeddings = embeddings.to("cuda:0", dtype=dtype)
        if negative_embeddings is not None:
            negative_embeddings = negative_embeddings.to("cuda:0", dtype=dtype)

        def on_step(pipeline, step, timestep, callback_kwargs):
            if (step+1)%4==0:cool_between_steps(self.gpu['index'],self.emit)
            self.emit({"event": "step", "completed_steps": step + 1,
                       "total_steps": parameters.steps, "utc": utc_now()})
            return callback_kwargs

        sampling_started = time.perf_counter()
        with torch.inference_mode():
            frames = self.pipe(
                prompt_embeds=embeddings, negative_prompt_embeds=negative_embeddings,
                width=parameters.width, height=parameters.height,
                num_frames=parameters.frames, num_inference_steps=parameters.steps,
                guidance_scale=parameters.guidance,
                generator=torch.Generator(device="cpu").manual_seed(parameters.seed),
                max_sequence_length=parameters.max_sequence_length,
                callback_on_step_end=on_step, output_type="np",
            ).frames[0]
        torch.cuda.synchronize(0)
        sampling_seconds = time.perf_counter() - sampling_started
        if not np.isfinite(frames).all():
            raise RuntimeError("The model produced non-finite pixel values.")
        pixel_std = float(frames.std())
        temporal_change = float(np.abs(np.diff(frames.astype(np.float32), axis=0)).mean())
        if pixel_std < 0.005 or temporal_change < 0.00001:
            raise RuntimeError(f"Degenerate or static output: std={pixel_std}, change={temporal_change}")
        os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
        video_path = output_directory / "video.mp4"
        export_to_video(frames, str(video_path), fps=parameters.fps)
        selected = np.linspace(0, len(frames) - 1, 4, dtype=int)
        sheet = Image.new("RGB", (parameters.width * 2, parameters.height * 2))
        for position, index in enumerate(selected):
            image = Image.fromarray(np.round(np.clip(frames[index], 0, 1) * 255).astype(np.uint8))
            sheet.paste(image, ((position % 2) * parameters.width, (position // 2) * parameters.height))
        sheet.save(output_directory / "contact-sheet.jpg", quality=92)
        result = {"model_id": MODEL_ID, "model_path": str(MODEL_DIR), "source": "self-hosted open weights",
                  "gpu": self.gpu, "versions": self.versions, "precision": self.precision,
                  "text_encoder_device": "cpu", "text_encoder_precision": "float32",
                  "vae_precision": "float32", "parameters": asdict(parameters),
                  "model_load_seconds": round(self.load_seconds, 3),
                  "prompt_encoding_seconds": round(text_seconds, 3),
                  "sampling_and_decode_seconds": round(sampling_seconds, 3),
                  "generation_seconds": round(time.perf_counter() - started, 3),
                  "peak_gpu_allocated_bytes": torch.cuda.max_memory_allocated(0),
                  "peak_gpu_reserved_bytes": torch.cuda.max_memory_reserved(0),
                  "pixel_std": pixel_std, "mean_temporal_pixel_change": temporal_change,
                  "video_bytes": video_path.stat().st_size, "video_path": str(video_path),
                  "completed_at": utc_now()}
        del embeddings, negative_embeddings, frames
        gc.collect()
        torch.cuda.empty_cache()
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", type=int, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--negative-prompt", default=GenerationParameters.negative_prompt)
    parser.add_argument("--guidance", type=float, default=5.0)
    parser.add_argument("--fps", type=int, default=16)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=192)
    parser.add_argument("--frames", type=int, default=17)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-sequence-length", type=int, default=128)
    parser.add_argument("--precision", choices=["float32", "float16"], default="float32")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", args.job_id):
        raise ValueError("Invalid job identifier.")
    parameters = GenerationParameters(prompt=args.prompt, negative_prompt=args.negative_prompt,
                                      guidance=args.guidance, fps=args.fps, width=args.width, height=args.height,
                                      frames=args.frames, steps=args.steps, seed=args.seed,
                                      max_sequence_length=args.max_sequence_length)
    parameters.validate()
    run_dir = ROOT / "runs" / args.job_id
    run_dir.mkdir(parents=True, exist_ok=False)
    lock_handle = ROOT.joinpath("logs", f"gpu-{args.gpu}.lock").open("a")
    project_lock = ROOT.joinpath("logs/model-inference.lock").open("a")
    state = {"job_id": args.job_id, "status": "running", "started_at": utc_now(),
             "parameters": asdict(parameters), "pid": os.getpid()}
    write_json(run_dir / "result.json", state)

    def emit(event):
        print(json.dumps(event), flush=True)
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
        state["last_event"] = event
        write_json(run_dir / "result.json", state)

    try:
        fcntl.flock(project_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        generator = WanGenerator(args.gpu, precision=args.precision, emit=emit)
        state.update(generator.generate(parameters, run_dir))
        state["status"] = "succeeded"
        write_json(run_dir / "result.json", state)
        print(json.dumps(state, indent=2), flush=True)
    except Exception as error:
        state.update(status="failed", error=f"{type(error).__name__}: {error}", completed_at=utc_now())
        write_json(run_dir / "result.json", state)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
