"""Real API/database behind the gateway: two owners, projects and drafts, no GPU."""
import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from video_platform.api import create_app
from video_platform.auth import AuthStore
from video_platform.friend_gateway import create_gateway
from video_platform.store import TaskStore

class CustomerWorkflowTests(unittest.TestCase):
    def test_video_api_settings_generation_and_combined_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);service=root/'service';gateway=root/'gateway'
            auth=AuthStore(service);admin=auth.create_user('operator','fixture-password-admin','Operator',admin=True)
            alice=auth.create_user('alice','fixture-password-alice','Alice',invite=auth.invite(admin['id']))
            (gateway/'.secrets').mkdir(parents=True)
            (gateway/'.secrets/friend-gateway.json').write_text(json.dumps({'enabled':True,'expires_at':time.time()+3600,'public_origin':'https://friends.example.test','allowed_user_ids':[alice['id']],'allowed_usernames':['alice'],'daily_limit':10}))
            (service/'data/video-api-policy.json').write_text('{"enabled":true}')
            from video_platform.store import now
            (service/'data/video-api-worker.json').write_text(json.dumps({'state':'ready','updated_at':now()}))
            with patch('video_platform.api.ROOT',service):
                app=create_app(TaskStore(service/'data/tasks.sqlite3'),deployment={'auth_required':True})
                with TestClient(app),TestClient(create_gateway(gateway,httpx.ASGITransport(app=app)),base_url='https://friends.example.test') as client:
                    origin={'Origin':'https://friends.example.test'}
                    login=client.post('/api/auth/login',headers=origin,json={'username':'alice','password':'fixture-password-alice'});headers={**origin,'X-CSRF-Token':login.json()['csrf']}
                    self.assertEqual(client.get('/video-api').status_code,200)
                    c=client.post('/api/video-api/connections',headers=headers,json={'provider':'minimax','label':'My H3','model':'MiniMax-H3','region':'cn','api_key':'fixture-customer-key'});self.assertEqual(c.status_code,201)
                    payload={'connection_id':c.json()['id'],'prompt':'Boat','duration':5,'resolution':'768P','ratio':'16:9','allow_paid_call':True}
                    h={**headers,'Idempotency-Key':'customer-one'}
                    job=client.post('/api/video-api/tasks',headers=h,json=payload);self.assertEqual(job.status_code,202)
                    self.assertEqual(client.post('/api/video-api/tasks',headers=h,json=payload).json()['id'],job.json()['id'])
                    self.assertEqual(client.post('/api/tasks',headers={**headers,'Idempotency-Key':'local-two'},json={'model':'local-h3','prompt':'Boat'}).status_code,429)
                    self.assertEqual(client.get('/api/experience').json()['used_today'],1)
                    self.assertEqual(client.delete('/api/video-api/connections/'+c.json()['id'],headers=headers).status_code,200)
                    self.assertEqual(client.get('/api/video-api/tasks/'+job.json()['id']).json()['status'],'cancelled')
                    self.assertEqual(client.get('/api/admin/overview').status_code,404)

    def test_projects_and_storyboards_persist_with_csrf_revision_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);service=root/'service';gateway=root/'gateway'
            auth=AuthStore(service)
            admin=auth.create_user('operator','fixture-password-admin','Operator',admin=True)
            users=[auth.create_user(name,'fixture-password-user',name,invite=auth.invite(admin['id'])) for name in ('alice','bob')]
            (gateway/'.secrets').mkdir(parents=True)
            (gateway/'.secrets/friend-gateway.json').write_text(json.dumps({'enabled':True,'expires_at':time.time()+3600,
              'public_origin':'https://friends.example.test','allowed_user_ids':[u['id'] for u in users],
              'allowed_usernames':['alice','bob'],'daily_limit':10}))
            with patch('video_platform.api.ROOT',service):
                app=create_app(TaskStore(service/'data/tasks.sqlite3'),deployment={'auth_required':True})
                with TestClient(app),TestClient(create_gateway(gateway,httpx.ASGITransport(app=app)),base_url='https://friends.example.test') as client:
                    origin={'Origin':'https://friends.example.test'}
                    login=client.post('/api/auth/login',headers=origin,json={'username':'alice','password':'fixture-password-user'})
                    self.assertEqual(login.status_code,200);headers={**origin,'X-CSRF-Token':login.json()['csrf']}
                    body={'title':'客户短剧','episodes':[{'id':'1'*32,'title':'第一集','shots':[{'id':'2'*32,'title':'第一镜','description':'A blue paper boat','seconds':4}]}]}
                    made=client.post('/api/projects',headers=headers,json=body);self.assertEqual(made.status_code,201)
                    project=made.json();path='/api/projects/'+project['id']
                    updated=client.put(path,headers=headers,json={**body,'title':'客户短剧修改','revision':project['revision']})
                    self.assertEqual(updated.status_code,200)
                    self.assertEqual(client.put(path,headers=headers,json={**body,'revision':project['revision']}).status_code,409)
                    self.assertEqual(client.put(path,headers=origin,json=body).status_code,403)
                    draft=client.post('/api/storyboards',headers=headers,json={'title':'客户分镜','shots':[{'description':'Sea','seconds':4}]}).json()
                    boardpath='/api/storyboards/'+draft['id']
                    self.assertEqual(client.get(boardpath).status_code,200)
                    self.assertEqual(client.put(boardpath,headers=headers,json={'title':'修改草稿','shots':[{'description':'Boat','seconds':5}],'revision':draft['revision']}).status_code,200)
                    self.assertEqual(client.get('/api/experience').json()['used_today'],0)
                    client.post('/api/auth/logout',headers=headers)
                    login=client.post('/api/auth/login',headers=origin,json={'username':'bob','password':'fixture-password-user'})
                    self.assertEqual(login.status_code,200)
                    self.assertEqual(client.get(path).status_code,404)
                    self.assertEqual(client.get(boardpath).status_code,404)
                    self.assertEqual(client.get('/api/projects').json()['items'],[])
                    self.assertEqual(client.get('/api/storyboards').json()['items'],[])
                    self.assertEqual(client.get('/api/admin/overview').status_code,404)
                    self.assertEqual(client.get('/api/tasks').json()['items'],[])
