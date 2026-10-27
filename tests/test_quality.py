from unittest import TestCase
import numpy as np
from video_platform.quality import require_nonblank, frame_quality


class QualityTests(TestCase):
    def test_blank_white_black_and_solid_color_rejected(self):
        for color in [[1, 1, 1], [0, 0, 0], [0.2, 0.5, 0.8]]:
            pixels = np.tile(np.asarray(color, dtype=np.float32), (4, 32, 32, 1))
            with self.assertRaises(RuntimeError):
                require_nonblank(pixels)

    def test_high_key_frame_with_subject_is_not_rejected(self):
        pixels = np.ones((4, 32, 32, 3), dtype=np.float32)
        pixels[:, 10:22, 10:22, :] = [0.1, 0.3, 0.8]
        self.assertFalse(require_nonblank(pixels)["blank_suspected"])
        pixels[0, 0, 0, 0] = np.nan
        with self.assertRaises(RuntimeError):
            frame_quality(pixels)

    def test_nearly_uniform_off_white_diagnostic_is_rejected(self):
        # Regression for an actual rejected clip: tiny texture around off-white
        # escaped the original extreme-white threshold without a visible subject.
        rng = np.random.default_rng(42)
        pixels = np.clip(rng.normal(0.965, 0.006, (4, 32, 32, 3)), 0, 1).astype(
            np.float32
        )
        self.assertTrue(frame_quality(pixels)["blank_suspected"])
        with self.assertRaises(RuntimeError):
            require_nonblank(pixels)

    def test_most_frames_blank_even_with_one_detailed_frame_is_rejected(self):
        pixels = np.ones((16, 32, 32, 3), dtype=np.float32)
        pixels[-1, ::2, :, :] = 0
        result = frame_quality(pixels)
        self.assertGreater(result["spatial_std_mean"], 0.01)
        self.assertGreaterEqual(result["blank_sample_fraction"], 0.9)
        self.assertTrue(result["blank_suspected"])
