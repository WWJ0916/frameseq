"""Independently decode a generated MP4 and compare it with the run record."""

import argparse
import hashlib
import json
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from video_platform.config import ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    run = args.run_directory.resolve()
    root = (ROOT / "runs").resolve()
    if not run.is_relative_to(root):
        raise ValueError("Only this project's recorded runs may be inspected.")
    result = json.loads((run / "result.json").read_text())
    if result["status"] != "succeeded":
        raise ValueError("The generation did not report success.")
    expected = result["parameters"]
    video = run / "video.mp4"
    reader = imageio.get_reader(str(video), format="ffmpeg")
    metadata = reader.get_meta_data()
    count = 0
    changes = []
    deviations = []
    previous = None
    try:
        for frame in reader:
            assert frame.shape == (
                expected["height"],
                expected["width"],
                3,
            ), frame.shape
            assert np.isfinite(frame).all()
            pixels = frame.astype(np.float32) / 255
            deviations.append(float(pixels.std()))
            if previous is not None:
                changes.append(float(np.abs(pixels - previous).mean()))
            previous = pixels
            count += 1
    finally:
        reader.close()
    assert count == expected["frames"], (count, expected["frames"])
    assert abs(metadata["fps"] - expected["fps"]) < 0.01, metadata
    assert min(deviations) > 0.005, deviations
    assert changes and float(np.mean(changes)) > 0.00001, changes
    report = {
        "status": "passed",
        "decoded_frames": count,
        "width": expected["width"],
        "height": expected["height"],
        "fps": metadata["fps"],
        "duration_seconds": metadata.get("duration"),
        "codec": metadata.get("codec"),
        "minimum_frame_pixel_std": min(deviations),
        "decoded_mean_temporal_change": float(np.mean(changes)),
        "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "limitation": "Structural checks do not assess visual quality or prompt alignment.",
    }
    (run / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
