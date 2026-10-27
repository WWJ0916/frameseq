"""Small CPU-created test animation. NOT output of any AI model."""

from pathlib import Path
import imageio.v2 as imageio
import numpy as np


def synthetic_video(directory):
    path = Path(directory) / "synthetic-fixture.mp4"
    if path.exists():
        return path
    with imageio.get_writer(
        str(path),
        fps=16,
        codec="libx264",
        macro_block_size=None,
        ffmpeg_log_level="error",
    ) as writer:
        for i in range(81):
            frame = np.full((64, 96, 3), 32, dtype=np.uint8)
            frame[20:36, i % 75 : i % 75 + 16] = (210, 60, 40)
            writer.append_data(frame)
    return path
