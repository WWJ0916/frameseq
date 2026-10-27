"""Per-instance application pacing only; never changes GPU hardware settings."""
import json
from pathlib import Path
from .config import ROOT


def temperature_control_enabled(root=None):
    path=Path(ROOT if root is None else root)/'data/gpu-temperature-policy.json'
    try:
        return json.loads(path.read_text()).get('application_temperature_control') is not False
    except (OSError,ValueError,TypeError,AttributeError):
        return True
