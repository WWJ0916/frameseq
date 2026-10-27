"""One configurable artifact root; defaults to this checkout, never a personal host."""

import os
from pathlib import Path

ROOT = (
    Path(os.environ.get("VIDEO_PLATFORM_ROOT", Path(__file__).resolve().parents[1]))
    .expanduser()
    .resolve()
)
