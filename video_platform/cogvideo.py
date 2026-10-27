"""Controlled single-card CogVideoX-2B inference; shares admission locks with Wan."""

import argparse, fcntl, gc, json, os, re, time, traceback
from pathlib import Path
from .inference import (
    ROOT,
    require_gpu_available,
    write_json,
    utc_now,
    cool_between_steps,
)


def run(args, run_dir, emit):
    snapshot = require_gpu_available(args.gpu)
    os.environ.update(
        CUDA_VISIBLE_DEVICES=snapshot["uuid"],
        HF_HUB_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
        OMP_NUM_THREADS="4",
        MKL_NUM_THREADS="4",
    )
    import torch, numpy as np
    from PIL import Image
    from diffusers import CogVideoXPipeline
    from diffusers.utils import export_to_video
    import diffusers, transformers

    torch.set_num_threads(4)
    torch.set_num_interop_threads(1)
    context = torch.empty(1, device="cuda:0")
    torch.cuda.synchronize()
    require_gpu_available(args.gpu, allow_self=True)
    if not (ROOT / "logs/cogvideo-download-complete.json").is_file():
        raise RuntimeError("Official 2B weights not yet verified.")
    start = time.perf_counter()
    emit(dict(event="loading_model", utc=utc_now()))
    compute_dtype = torch.float32 if args.precision == "float32" else torch.float16
    pipe = CogVideoXPipeline.from_pretrained(
        str(ROOT / "models/CogVideoX-2b"),
        torch_dtype=compute_dtype,
        local_files_only=True,
        use_safetensors=True,
    )
    if getattr(args, "scheduler", "ddim") == "dpm":
        from diffusers import CogVideoXDPMScheduler

        pipe.scheduler = CogVideoXDPMScheduler.from_config(pipe.scheduler.config)
    encoder = pipe.text_encoder.to(device="cpu", dtype=torch.float32).eval()
    pipe.register_modules(text_encoder=None)
    pipe.vae.to(
        dtype=torch.float16 if args.vae_precision == "float16" else torch.float32
    )
    pipe.vae.enable_tiling()
    pipe.vae.enable_slicing()
    pipe.to("cuda:0")
    pipe.set_progress_bar_config(disable=True)
    load = time.perf_counter() - start
    emit(
        dict(
            event="model_loaded",
            load_seconds=round(load, 3),
            transformer_dtype=str(pipe.transformer.dtype),
            vae_dtype=str(pipe.vae.dtype),
            utc=utc_now(),
        )
    )
    start = time.perf_counter()
    torch.cuda.reset_peak_memory_stats()
    pipe.register_modules(text_encoder=encoder)
    try:
        with torch.inference_mode():
            positive, negative = pipe.encode_prompt(
                prompt=args.prompt,
                negative_prompt=args.negative_prompt,
                do_classifier_free_guidance=True,
                num_videos_per_prompt=1,
                max_sequence_length=226,
                device=torch.device("cpu"),
                dtype=torch.float32,
            )
    finally:
        pipe.register_modules(text_encoder=None)
    encoded = time.perf_counter() - start
    if not torch.isfinite(positive).all() or not torch.isfinite(negative).all():
        raise RuntimeError("提示词编码包含非有限值。")
    positive = positive.to("cuda", dtype=compute_dtype)
    negative = negative.to("cuda", dtype=compute_dtype)

    def callback(p, step, timestep, kwargs):
        if not torch.isfinite(kwargs["latents"]).all():
            raise RuntimeError("CogVideoX采样潜变量溢出；未输出纯色视频。")
        if getattr(args, "save_latents", False) and step + 1 == args.steps:
            from safetensors.torch import save_file

            save_file(
                {"latents": kwargs["latents"].detach().cpu().contiguous()},
                str(run_dir / "diagnostic-latents.safetensors"),
            )
        if (step + 1) % 2 == 0:
            cool_between_steps(args.gpu, emit)
        emit(
            dict(
                event="step",
                completed_steps=step + 1,
                total_steps=args.steps,
                latent_std=kwargs["latents"].float().std().item(),
                latent_abs_max=kwargs["latents"].float().abs().max().item(),
                utc=utc_now(),
            )
        )
        return kwargs

    sample_start = time.perf_counter()
    with torch.inference_mode():
        frames = pipe(
            prompt_embeds=positive,
            negative_prompt_embeds=negative,
            width=720,
            height=480,
            num_frames=49,
            num_inference_steps=args.steps,
            guidance_scale=6,
            generator=torch.Generator("cpu").manual_seed(args.seed),
            max_sequence_length=226,
            output_type="np",
            callback_on_step_end=callback,
        ).frames[0]
    torch.cuda.synchronize()
    sampling = time.perf_counter() - sample_start
    if not np.isfinite(frames).all():
        raise RuntimeError("Non-finite CogVideoX pixels.")
    # Preserve operator-only visual diagnostics even when output is rejected.
    sheet = Image.new("RGB", (720 * 2, 480 * 2))
    for i, index in enumerate([0, 16, 32, 48]):
        sheet.paste(
            Image.fromarray(
                (np.clip(frames[index], 0, 1) * 255).round().astype("uint8")
            ),
            ((i % 2) * 720, (i // 2) * 480),
        )
    sheet.save(run_dir / "contact-sheet.jpg", quality=90)
    if (
        frames.std() < 0.005
        or np.abs(np.diff(frames.astype(np.float32), axis=0)).mean() < 0.00001
    ):
        raise RuntimeError("Degenerate CogVideoX output; diagnostic sheet retained.")
    from .quality import frame_quality

    quality = frame_quality(frames)
    emit({"event": "quality_check", **quality, "utc": utc_now()})
    if quality["blank_suspected"]:
        raise RuntimeError(
            "模型输出接近纯色，未作为成功视频接受；日志已保留，请调整参数后另建任务。"
        )
    export_to_video(frames, str(run_dir / "video.mp4"), fps=8, macro_block_size=1)
    return dict(
        model_id="zai-org/CogVideoX-2b",
        model_path=str(ROOT / "models/CogVideoX-2b"),
        source="self-hosted open weights",
        gpu=snapshot,
        versions=dict(
            torch=torch.__version__,
            diffusers=diffusers.__version__,
            transformers=transformers.__version__,
        ),
        precision=args.precision,
        text_encoder_device="cpu",
        text_encoder_precision="float32",
        vae_precision=args.vae_precision,
        quality_checks=quality,
        model_load_seconds=round(load, 3),
        prompt_encoding_seconds=round(encoded, 3),
        sampling_and_decode_seconds=round(sampling, 3),
        generation_seconds=round(time.perf_counter() - start, 3),
        peak_gpu_allocated_bytes=torch.cuda.max_memory_allocated(),
        peak_gpu_reserved_bytes=torch.cuda.max_memory_reserved(),
        video_bytes=(run_dir / "video.mp4").stat().st_size,
        video_path=str(run_dir / "video.mp4"),
        completed_at=utc_now(),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gpu", type=int, required=True)
    p.add_argument("--job-id", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument(
        "--negative-prompt", default="blurry, low quality, distorted, text, watermark"
    )
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--steps", type=int, default=20)
    p.add_argument("--wait-for-slot", action="store_true")
    # Fixed model contract; shared worker supplies these explicitly.
    for key, default in [
        ("width", 720),
        ("height", 480),
        ("frames", 49),
        ("fps", 8),
        ("max-sequence-length", 226),
    ]:
        p.add_argument("--" + key, type=int, default=default)
    p.add_argument("--guidance", type=float, default=6)
    p.add_argument("--precision", choices=["float16", "float32"], default="float16")
    p.add_argument("--vae-precision", choices=["float16", "float32"], default="float32")
    p.add_argument("--scheduler", choices=["ddim", "dpm"], default="ddim")
    p.add_argument("--save-latents", action="store_true")
    args = p.parse_args()
    if (
        not re.fullmatch("[a-zA-Z0-9_-]{1,80}", args.job_id)
        or not 1 <= len(args.prompt.strip()) <= 2000
        or args.steps not in {20, 50}
    ):
        p.error("Unsupported CogVideoX experiment parameters.")
    if (args.width, args.height, args.frames, args.fps) != (720, 480, 49, 8):
        p.error("This model is fixed to720x480/49frames/8fps.")
    project_lock = (ROOT / "logs/model-inference.lock").open("a")
    gpu_lock = (ROOT / "logs" / f"gpu-{args.gpu}.lock").open("a")
    deadline = time.monotonic() + 2 * 3600
    while True:
        try:
            fcntl.flock(project_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if not args.wait_for_slot or time.monotonic() > deadline:
                raise
            time.sleep(2)
    fcntl.flock(gpu_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # A previous inference can take a moment to release its CUDA context.
    for attempt in range(30):
        try:
            require_gpu_available(args.gpu)
            break
        except RuntimeError:
            if attempt == 29:
                raise
            time.sleep(2)
    run_dir = ROOT / "runs" / args.job_id
    run_dir.mkdir(exist_ok=False)
    state = dict(
        job_id=args.job_id,
        status="running",
        started_at=utc_now(),
        pid=os.getpid(),
        parameters=dict(
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            width=720,
            height=480,
            frames=49,
            fps=8,
            steps=args.steps,
            seed=args.seed,
            guidance=6,
            max_sequence_length=226,
            model_id="zai-org/CogVideoX-2b",
        ),
    )
    state["parameters"].update(
        scheduler=args.scheduler,
        precision=args.precision,
        vae_precision=args.vae_precision,
    )

    def emit(event):
        print(json.dumps(event), flush=True)
        state["last_event"] = event
        write_json(run_dir / "result.json", state)
        with (run_dir / "events.jsonl").open("a") as f:
            f.write(json.dumps(event) + "\n")

    try:
        state.update(run(args, run_dir, emit))
        state["status"] = "succeeded"
        write_json(run_dir / "result.json", state)
    except Exception as error:
        state.update(
            status="failed",
            error=f"{type(error).__name__}: {error}",
            completed_at=utc_now(),
        )
        write_json(run_dir / "result.json", state)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
