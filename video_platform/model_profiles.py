"""Versioned local inference contracts shared by API and worker."""

import json

COG_PROFILE_ID = "cog2b-dpm20-fp16-v1"
COG_NEGATIVE = "blurry, low quality, distorted, text, watermark"
COG_PROFILE = dict(
    inference_profile=COG_PROFILE_ID,
    model_id="zai-org/CogVideoX-2b",
    precision="float16",
    vae_precision="float16",
    scheduler="dpm",
    width=720,
    height=480,
    frames=49,
    fps=8,
    steps=20,
    guidance=6,
    max_sequence_length=226,
)


def cog_profile_verified(root):
    try:
        record = json.loads((root / "logs/cogvideo-profile-verified.json").read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(record, dict)
        and record.get("status") == "passed"
        and record.get("inference_profile") == COG_PROFILE_ID
    )


WAN_NATIVE_ID = "wan1.3b-native480-v1"
WAN_NATIVE = dict(
    inference_profile=WAN_NATIVE_ID,
    model_id="Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
    precision="float16",
    vae_precision="float32",
    scheduler="unipc-flow3",
    width=832,
    height=480,
    frames=81,
    fps=16,
    steps=40,
    guidance=5.0,
    max_sequence_length=512,
)


def wan_native_verified(root):
    try:
        record = json.loads((root / "logs/wan-native480-verified.json").read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(record, dict)
        and record.get("status") == "passed"
        and record.get("profile") == WAN_NATIVE
    )
