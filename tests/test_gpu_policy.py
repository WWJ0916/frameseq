import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from video_platform.gpu_policy import temperature_control_enabled
from video_platform.worker import dual_gpu_choice,idle_gpu_choice
from video_platform.inference import cool_between_steps,require_gpu_available
from video_platform.h3_local import wait_for_dual_idle


class ApplicationTemperaturePolicy(unittest.TestCase):
    def test_only_explicit_instance_opt_out_disables_application_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);p=root/'data/gpu-temperature-policy.json';p.parent.mkdir()
            self.assertTrue(temperature_control_enabled(root))
            for value in ('{}','broken','{"application_temperature_control":0}','{"application_temperature_control":true}'):
                p.write_text(value);self.assertTrue(temperature_control_enabled(root))
            p.write_text('{"application_temperature_control":false}');self.assertFalse(temperature_control_enabled(root))

    def test_opt_out_bypasses_heat_but_never_other_users_memory_or_utilization(self):
        with patch('video_platform.worker.temperature_control_enabled',return_value=False):
            self.assertEqual(dual_gpu_choice([(0,0,0,83),(1,0,0,84)]),[0,1])
            self.assertEqual(idle_gpu_choice([(0,0,0,83)],65),0)
            for bad in ([(0,10,0,40),(1,0,0,40)],[(0,0,1,40),(1,0,0,40)]):
                with self.assertRaises(RuntimeError):dual_gpu_choice(bad)
        snapshot={'memory_used_mib':0,'utilization_percent':0,'temperature_c':84}
        with patch('video_platform.inference.temperature_control_enabled',return_value=False),patch('video_platform.inference.gpu_snapshot',return_value=snapshot):
            self.assertEqual(require_gpu_available(0),snapshot)
        with patch('video_platform.inference.temperature_control_enabled',return_value=False),patch('video_platform.inference.gpu_snapshot') as probe:
            cool_between_steps(0,lambda event:None);probe.assert_not_called()

    def test_dual_handoff_still_checks_both_cards_when_temperature_control_is_off(self):
        with patch('video_platform.h3_local.temperature_control_enabled',return_value=False),patch('video_platform.h3_local.require_gpu_available',return_value={'temperature_c':85}) as probe:
            self.assertTrue(wait_for_dual_idle([0,1],attempts=1));self.assertEqual(probe.call_count,2)
