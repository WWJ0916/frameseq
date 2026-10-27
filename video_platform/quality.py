"""Conservative blank-frame checks; this does not assess story quality or prompt alignment."""

import numpy as np


def frame_quality(frames):
    sample = np.asarray(frames, dtype=np.float32)[:: max(1, len(frames) // 12)]
    if not np.isfinite(sample).all():
        raise RuntimeError("模型输出包含非有限像素。")
    per_frame_spatial = sample.std(axis=(1, 2)).mean(axis=-1)
    spatial = float(per_frame_spatial.mean())
    blank_fraction = float(np.mean(per_frame_spatial < 0.01))
    extreme = float(
        np.mean(np.all(sample > 0.985, axis=-1) | np.all(sample < 0.015, axis=-1))
    )
    blank = (
        spatial < 0.01 or blank_fraction >= 0.9 or (spatial < 0.03 and extreme > 0.95)
    )
    return {
        "check": "blank_frame_heuristic",
        "spatial_std_mean": spatial,
        "blank_sample_fraction": blank_fraction,
        "near_white_or_black_fraction": extreme,
        "blank_suspected": blank,
        "limitation": "只能识别明显纯色异常，不能保证剧情或主体正确。",
    }


def require_nonblank(frames):
    result = frame_quality(frames)
    if result["blank_suspected"]:
        raise RuntimeError(
            "模型输出接近纯色，未作为成功视频接受；日志已保留，请调整参数后另建任务。"
        )
    return result
