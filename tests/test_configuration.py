"""A fresh interpreter must resolve the same artifact root across modules."""

import json, os, subprocess, sys, tempfile
from pathlib import Path
from unittest import TestCase


class ConfigurationTests(TestCase):
    def resolved(self, env):
        code = "import json; from video_platform.config import ROOT; from video_platform.store import ROOT as S; from video_platform.inference import ROOT as I; print(json.dumps([str(ROOT),str(S),str(I)]))"
        return json.loads(
            subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
        )

    def test_environment_root_is_shared(self):
        with tempfile.TemporaryDirectory() as d:
            roots = self.resolved({**os.environ, "VIDEO_PLATFORM_ROOT": d})
            self.assertEqual(roots, [str(Path(d).resolve())] * 3)

    def test_default_is_checkout_instead_of_personal_host(self):
        env = dict(os.environ)
        env.pop("VIDEO_PLATFORM_ROOT", None)
        self.assertEqual(
            self.resolved(env), [str(Path(__file__).resolve().parents[1])] * 3
        )
