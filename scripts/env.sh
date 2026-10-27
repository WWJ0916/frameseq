#!/usr/bin/env bash
# Source from this checkout. This file contains no credential or personal host.
export VIDEO_PLATFORM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export TMPDIR="$VIDEO_PLATFORM_ROOT/tmp"
export HF_HOME="$VIDEO_PLATFORM_ROOT/cache/huggingface"
export MODELSCOPE_CACHE="$VIDEO_PLATFORM_ROOT/cache/modelscope"
export TORCH_HOME="$VIDEO_PLATFORM_ROOT/cache/torch"
export PIP_CACHE_DIR="$VIDEO_PLATFORM_ROOT/cache/pip"
export XDG_CACHE_HOME="$VIDEO_PLATFORM_ROOT/cache"
export TOKENIZERS_PARALLELISM=false
export HF_HUB_DISABLE_XET=1
mkdir -p "$TMPDIR" "$VIDEO_PLATFORM_ROOT/cache"
