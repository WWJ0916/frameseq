"""CPU contract tests only; live H3 evidence is recorded separately."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from video_platform.api import create_app,TaskRequest
from video_platform.store import TaskStore
from video_platform.h3_runtime import H3_ID,H3_PROFILE,H3_PROFILE_ID,h3_ready
from video_platform.management import ManagementStore
from video_platform.store import now

class H3LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'logs').mkdir()
        self.store=TaskStore(self.root/'data/tasks.sqlite3')
        (self.root/'data/worker.json').write_text(json.dumps({'state':'idle','updated_at':now(),'profiles':[H3_PROFILE_ID]}))
    def tearDown(self): self.temp.cleanup()
    def client(self):return TestClient(create_app(self.store,deployment={'auth_required':False}))
    def test_h3_payload_fixed_contract_and_rejects_wan_parameters(self):
        p=TaskRequest(model='local-h3',prompt='纸船',seed=123).parameters()
        self.assertEqual(p,{**H3_PROFILE,'prompt':'纸船','seed':123})
        for args in ({'preset':'story'},{'negative_prompt':'blur'}):
            with self.assertRaises(ValueError):TaskRequest(model='local-h3',prompt='x',**args).parameters()
    def test_local_h3_enqueues_idempotently_and_is_not_wan(self):
        with patch('video_platform.api.ROOT',self.root),patch('video_platform.api.h3_ready',return_value=True),self.client() as c:
            body={'model':'local-h3','prompt':'same prompt','seed':7}
            a=c.post('/api/tasks',json=body,headers={'Idempotency-Key':'h3-test'})
            b=c.post('/api/tasks',json=body,headers={'Idempotency-Key':'h3-test'})
            self.assertEqual(a.status_code,202,a.text)
            self.assertEqual(a.json()['id'],b.json()['id'])
            self.assertEqual(a.json()['parameters']['model_id'],H3_ID)
            self.assertEqual(c.post('/api/tasks',json={**body,'seed':8},headers={'Idempotency-Key':'h3-test'}).status_code,409)
    def test_h3_readiness_gate_rejects_before_enqueue(self):
        with patch('video_platform.api.ROOT',self.root),self.client() as c:
            self.assertEqual(c.post('/api/tasks',json={'model':'local-h3','prompt':'x'}).status_code,409)
            self.assertEqual(self.store.list()['total'],0)
        self.assertFalse(h3_ready(self.root))
    def test_disabled_h3_does_not_fall_back_to_wan_policy(self):
        with ManagementStore(self.root).auth.connect() as db:
            db.execute("INSERT INTO model_settings(id,enabled,revision) VALUES(?,?,?)",('local-h3',0,1))
        with self.assertRaises(ValueError):self.store.create(TaskRequest(model='local-h3',prompt='x').parameters())
        self.assertEqual(self.store.create(TaskRequest(prompt='wan').parameters())['status'],'queued')
    def test_all_cloud_mutations_disabled_without_launch_or_reservation(self):
        from video_platform.cloud import CloudStore
        before=CloudStore(self.root).list()['total']
        with patch('video_platform.api.ROOT',self.root),patch('video_platform.api.subprocess.Popen') as launch,self.client() as c:
            for url in ('/api/cloud/tasks','/api/cloud/first-test','/api/cloud/retry-after-topup'):
                self.assertEqual(c.post(url,json={'prompt':'must not charge'},headers={'Idempotency-Key':'blocked'}).status_code,410)
            self.assertEqual(c.post('/api/cloud/tasks/'+'a'*32+'/resume-result').status_code,410)
            self.assertFalse(c.get('/api/cloud/state').json()['configured'])
            launch.assert_not_called()
        self.assertEqual(CloudStore(self.root).list()['total'],before)
    def test_h3_worker_command_and_seed_are_task_specific(self):
        from video_platform.worker import inference_command
        from video_platform.h3_local import stage_command
        task={'id':'a'*32,'parameters':TaskRequest(model='local-h3',prompt='boat',seed=99).parameters()}
        with patch('video_platform.worker.runtime_config',return_value={'python':'/own/env/bin/python'}):
            cmd=inference_command(task,0)
            self.assertIn('video_platform.h3_local',cmd)
            self.assertNotIn('video_platform.inference',cmd)
            with self.assertRaises(ValueError):inference_command({**task,'parameters':{**task['parameters'],'steps':4}},0)
        cfg={'python':'/own/env/bin/python','runner':'/own/h3/runner.py'}
        cmd=stage_command(cfg,'sample',Path('/own/h3/runs/task-a/sample'),0,'task-'+'a'*32,task['parameters'],[7,8],{'conditioning':'/own/h3/runs/task-a/encode/conditioning.pt'})
        self.assertEqual(cmd[cmd.index('--seed')+1],'99')
        self.assertEqual(cmd.count('--inference-lock-fd'),2)
        self.assertEqual(cmd[cmd.index('--conditioning')+1],'/own/h3/runs/task-a/encode/conditioning.pt')
    def test_worker_cli_registers_options_once_without_starting_gpu(self):
        import subprocess,sys
        result=subprocess.run([sys.executable,'-m','video_platform.worker','--help'],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--auto-gpu',result.stdout)
    def test_worker_can_find_its_h3_child_by_job_id_without_loading_gpu(self):
        import subprocess,sys
        from video_platform.worker import matching_processes,ROOT
        job='task-'+'b'*32
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)','video_platform.h3_local','--job-id',job],cwd=ROOT)
        try:
            self.assertIn(child.pid,matching_processes(job))
            self.assertNotIn(child.pid,matching_processes('task-'+'c'*32))
        finally:
            child.terminate();child.wait(timeout=10)
    def test_h3_admission_waits_for_cool_idle_card_without_claiming_task(self):
        from video_platform.worker import idle_gpu_choice,next_gpu_temperature_limit
        task=self.store.create(TaskRequest(model='local-h3',prompt='x').parameters())
        self.assertEqual(next_gpu_temperature_limit(self.store),59)
        with self.assertRaises(RuntimeError):idle_gpu_choice([(0,0,0,64),(1,0,0,59),(2,1,0,40)],59)
        self.assertEqual(idle_gpu_choice([(0,0,0,64),(1,0,0,57),(2,100,0,40)],59),1)
        self.assertEqual(self.store.get(task['id'])['status'],'queued')
    def test_h3_user_error_keeps_diagnostics_out_of_the_page(self):
        from video_platform.h3_local import failure_message
        message=failure_message('sample','FileNotFoundError: /private/operator/workspace/checkpoint')
        self.assertIn('视频采样',message)
        self.assertNotIn('/private/',message)
        self.assertNotIn('FileNotFoundError',message)
        self.assertIn('温度保护',failure_message('sample','GPU reached 80C'))
        self.assertIn('82℃',failure_message('sample','GPU reached 82C'))
    def test_supervisor_root_cause_survives_rank_termination(self):
        from video_platform.h3_local import stage_failure_detail,failure_message
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            (directory/'supervisor-failure.json').write_text(json.dumps({'status':'failed','error':'GPU reached 82C; supervisor stops only its ranks'}))
            detail=stage_failure_detail(directory,{'status':'failed','error':'KeyboardInterrupt: Operator stopped this SP2 stage'})
            self.assertIn('GPU reached 82C',detail)
            self.assertIn('82℃',failure_message('sample',detail))
            (directory/'supervisor-failure.json').write_text('incomplete')
            self.assertEqual(stage_failure_detail(directory,{'error':'CUDA out of memory'}),'CUDA out of memory')
    def test_stage_handoff_waits_for_transient_cuda_cleanup(self):
        from video_platform.h3_local import wait_for_idle
        pauses=[]
        with patch('video_platform.h3_local.require_gpu_available',side_effect=[RuntimeError('residual activity'),{}]) as probe:
            self.assertTrue(wait_for_idle(1,pause=pauses.append,attempts=3))
            self.assertEqual(probe.call_count,2)
        self.assertEqual(pauses,[2])
    def test_only_preflight_decode_resource_failure_can_reuse_sampling(self):
        from video_platform.h3_local import decode_resource_failure
        report={'status':'failed','error':'GPU1 is occupied or hot; experiment not started'}
        stages={'sample':{'status':'passed'}}
        self.assertTrue(decode_resource_failure('decode',report,stages))
        self.assertFalse(decode_resource_failure('sample',report,stages))
        self.assertFalse(decode_resource_failure('decode',{**report,'torch_version':'2.6.0'},stages))
        self.assertFalse(decode_resource_failure('decode',{'status':'failed','error':'CUDA out of memory'},stages))
        self.assertFalse(decode_resource_failure('decode',report,{}))
    def test_decode_requeue_preserves_task_first_start_and_idempotency(self):
        task=self.store.create(TaskRequest(model='local-h3',prompt='x').parameters(),'resume-test')
        first=self.store.claim();self.store.progress(task['id'],99)
        self.store.finish(task['id'],'failed',error='resource preflight')
        self.store.defer_decode(task['id'])
        second=self.store.claim()
        self.assertEqual(first['started_at'],second['started_at'])
        self.assertEqual(first['parameters'],second['parameters'])
        self.assertEqual(self.store.create(first['parameters'],'resume-test')['id'],task['id'])
        self.assertEqual(self.store.list()['total'],1)
        self.assertEqual(second['progress'],99)
        with self.assertRaises(ValueError):self.store.defer_decode('f'*32)
    def test_checkpoint_resume_command_only_changes_decode_admission(self):
        from video_platform.worker import inference_command,next_gpu_temperature_limit
        task=self.store.create(TaskRequest(model='local-h3',prompt='x').parameters())
        marker=self.root/'runs'/('task-'+task['id'])/'resume-from-sample.json'
        marker.parent.mkdir(parents=True);marker.write_text('{}')
        self.assertEqual(next_gpu_temperature_limit(self.store),78)
        with patch('video_platform.worker.ROOT',self.root),patch('video_platform.worker.runtime_config',return_value={'python':'/own/python'}):
            self.assertIn('--resume-from-sample',inference_command(task,0))

if __name__=='__main__':unittest.main()
