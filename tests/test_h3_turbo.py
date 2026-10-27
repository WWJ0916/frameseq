import copy,unittest
from video_platform.api import TaskRequest
from video_platform.h3_runtime import H3_PROFILE,H3_PROFILE_ID,H3_LORA_SHA
from video_platform.h3_local import validate_sample_report

class TurboContracts(unittest.TestCase):
    def sample(self):
        return {**H3_PROFILE,'all_ranks_completed':True,'ranks':[
            {'lora':{'sha256':H3_LORA_SHA,'strength':1.0},'lora_applied_exactly_once':True}
            for _ in range(2)]}

    def test_every_new_h3_request_uses_turbo8_euler_and_verified_lora(self):
        p=TaskRequest(model='local-h3',prompt='scene').parameters()
        self.assertEqual(p['steps'],8);self.assertEqual(p['sampler'],'euler')
        self.assertEqual(p['lora_sha256'],H3_LORA_SHA)
        self.assertEqual((p['video_shift'],p['audio_shift']),(12.0,3.0))
        self.assertEqual(p['inference_profile'],H3_PROFILE_ID)

    def test_complete_verified_two_rank_turbo_is_accepted(self):
        validate_sample_report(self.sample(),H3_PROFILE)

    def test_old_steps_or_sampler_cannot_silently_satisfy_turbo(self):
        for key,value in [('steps',20),('sampler','res_multistep'),('video_shift',1),('inference_profile','old')]:
            s=self.sample();s[key]=value
            with self.assertRaises(ValueError):validate_sample_report(s,H3_PROFILE)

    def test_missing_or_double_lora_or_missing_rank_is_rejected(self):
        cases=[]
        s=self.sample();s['ranks'][1]['lora']['sha256']='wrong';cases.append(s)
        s=self.sample();s['ranks'][0]['lora_applied_exactly_once']=False;cases.append(s)
        s=self.sample();s['ranks'].pop();cases.append(s)
        s=self.sample();s['all_ranks_completed']=False;cases.append(s)
        for s in cases:
            with self.assertRaises(ValueError):validate_sample_report(s,H3_PROFILE)
