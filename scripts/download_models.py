"""Plan by default; download selected official weights only with --download."""

import argparse, json, shutil
from fnmatch import fnmatch
from video_platform.config import ROOT

MODELS = {
    "wan": ("Wan-AI/Wan2.1-T2V-1.3B-Diffusers", "Wan2.1-T2V-1.3B-Diffusers", "wan"),
    "cogvideo": ("zai-org/CogVideoX-2b", "CogVideoX-2b", "cogvideo"),
    "qwen": ("Qwen/Qwen2.5-1.5B-Instruct", "Qwen2.5-1.5B-Instruct", "qwen"),
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("model", choices=MODELS)
    p.add_argument("--revision", default="main")
    p.add_argument("--download", action="store_true")
    a = p.parse_args()
    from huggingface_hub import HfApi, snapshot_download

    repo, folder, marker = MODELS[a.model]
    info = HfApi().model_info(repo, revision=a.revision, files_metadata=True)
    patterns = [
        "*.json",
        "*.safetensors",
        "*.txt",
        "*.model",
        "*.tiktoken",
        "LICENSE*",
        "README.md",
    ]
    files = [
        f
        for f in info.siblings
        if any(fnmatch(f.rfilename, pattern) for pattern in patterns)
    ]
    if not files or any(f.size is None for f in files):
        raise RuntimeError("Cannot determine complete download size.")
    total = sum(f.size for f in files)
    target = ROOT / "models" / folder
    plan = dict(
        model=repo,
        revision=info.sha,
        bytes=total,
        free_bytes=shutil.disk_usage(ROOT).free,
        destination=str(target),
        download_requested=a.download,
    )
    print(json.dumps(plan, indent=2))
    if not a.download:
        return
    if plan["free_bytes"] < total + 30 * 1024**3:
        raise RuntimeError("Insufficient space: retain 30GiB of headroom.")
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo,
        revision=info.sha,
        local_dir=target,
        allow_patterns=[f.rfilename for f in files],
    )
    for f in files:
        path = target / f.rfilename
        if path.stat().st_size != f.size:
            raise RuntimeError("Downloaded size mismatch: " + f.rfilename)
        if path.suffix == ".safetensors":
            from safetensors import safe_open

            with safe_open(path, framework="pt", device="cpu") as tensor:
                if not tensor.keys():
                    raise RuntimeError("Empty weights: " + f.rfilename)
    (ROOT / "logs").mkdir(exist_ok=True)
    plan.update(
        status="complete",
        source="official Hugging Face repository",
        note="Download is not proof of successful inference.",
    )
    (ROOT / "logs" / f"{marker}-download-complete.json").write_text(
        json.dumps(plan, indent=2)
    )


if __name__ == "__main__":
    main()
