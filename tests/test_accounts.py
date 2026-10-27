"""Real SQLite + HTTP account isolation, CSRF and access tests; no external calls."""
from pathlib import Path
import tempfile,time
from unittest import TestCase
from unittest.mock import patch
from fastapi.testclient import TestClient
from video_platform.api import create_app,TaskRequest
from video_platform.auth import AuthStore,hash_password,check_password,COOKIE,digest
from video_platform.store import TaskStore
from video_platform.projects import Project,ProjectStore
from video_platform.ownership import as_user

class AccountTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=None);self.root=Path(self.temp.name);(self.root/'logs').mkdir()
        self.store=TaskStore(self.root/'data/tasks.sqlite3');self.auth=AuthStore(self.root)
        self.admin=self.auth.create_user('operator','fixture-admin-password','Operator',admin=True)
        self.a=self.auth.create_user('alice','fixture-alice-password','Alice',self.auth.invite(self.admin['id']))
        self.b=self.auth.create_user('bobby','fixture-bobby-password','Bobby',self.auth.invite(self.admin['id']))
    def tearDown(self):self.temp.cleanup()
    def app(self):return create_app(self.store,deployment={'auth_required':True})
    def login(self,c,username='alice',password='fixture-alice-password'):
        response=c.post('/api/auth/login',json=dict(username=username,password=password));self.assertEqual(response.status_code,200);return {'X-CSRF-Token':response.json()['csrf']}
    def test_password_hashing_and_single_use_invite(self):
        encoded=hash_password('long-fixture-password');self.assertNotIn('long-fixture-password',encoded);self.assertTrue(check_password('long-fixture-password',encoded));self.assertFalse(check_password('wrong',encoded));self.assertNotEqual(encoded,hash_password('long-fixture-password'))
        invite=self.auth.invite(self.admin['id']);self.auth.create_user('charlie','fixture-charlie-password','C',invite)
        with self.assertRaises(ValueError):self.auth.create_user('david','fixture-david-password','D',invite)
    def test_anonymous_private_routes_and_public_preview(self):
        with patch('video_platform.api.ROOT',self.root),TestClient(self.app()) as c:
            self.assertEqual(c.get('/api/tasks').status_code,401);self.assertEqual(c.get('/api/projects').status_code,401)
            self.assertEqual(c.get('/studio',follow_redirects=False).status_code,303)
            self.assertEqual(c.get('/').status_code,200);self.assertIn('explore.js',c.get('/').text)
            self.assertFalse(c.get('/api/auth/me').json()['authenticated']);self.assertEqual(c.get('/api/public/catalog').status_code,200)
            self.assertEqual(c.get('/assets/h3-progress.json',follow_redirects=False).status_code,303)
    def test_missing_configuration_keeps_authentication_required(self):
        with patch('video_platform.api.ROOT',self.root),TestClient(create_app(self.store)) as c:
            self.assertEqual(c.get('/api/tasks').status_code,401)
            self.assertTrue(c.get('/api/auth/me').json()['required'])
    def test_csrf_cookie_logout_and_paid_guard(self):
        with patch('video_platform.api.ROOT',self.root),patch('video_platform.api.subprocess.Popen') as spawn,TestClient(self.app()) as c:
            h=self.login(c);cookie=c.cookies.get(COOKIE);self.assertTrue(cookie)
            self.assertEqual(c.post('/api/projects',json={'title':'private'}).status_code,403)
            self.assertEqual(c.post('/api/projects',headers={**h,'Origin':'https://evil.example'},json={'title':'private'}).status_code,403)
            self.assertEqual(c.post('/api/projects',headers=h,json={'title':'private'}).status_code,201)
            for page in ('/studio','/projects','/storyboard','/edits','/video-api'):
                response=c.get(page)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.headers.get('cache-control'),'no-store')
            self.assertEqual(c.post('/api/cloud/tasks',headers={**h,'Idempotency-Key':'x'},json={'prompt':'paid'}).status_code,403)
            self.assertTrue(c.get('/api/cloud/state').json()['admin_only'])
            self.assertEqual(c.get('/assets/h3-progress.json').status_code,404)
            self.assertEqual(c.get('/assets/h3-live.html').status_code,404)
            self.assertNotIn('task_id',c.get('/api/health').json()['worker'])
            self.assertEqual(c.post('/api/auth/invites',headers=h).status_code,403)
            self.assertEqual(c.post('/api/auth/logout',headers=h).status_code,200);self.assertEqual(c.get('/api/tasks').status_code,401);spawn.assert_not_called()
            with self.auth.connect() as db:self.assertFalse(db.execute('SELECT 1 FROM sessions WHERE id=?',(digest(cookie),)).fetchone())
    def test_cross_account_task_project_and_artifact_isolation(self):
        with as_user(self.a):
            task=self.store.create(TaskRequest(prompt='A private prompt').parameters(),key='same-key')
            project=ProjectStore(self.root).save(Project(title='A private story'))
        with as_user(self.b):
            self.assertIsNone(self.store.get(task['id']));self.assertIsNone(ProjectStore(self.root).get(project['id']))
            own=self.store.create(TaskRequest(prompt='B private prompt').parameters(),key='same-key');self.assertNotEqual(task['id'],own['id'])
        with patch('video_platform.api.ROOT',self.root),TestClient(self.app()) as c:
            h=self.login(c,'bobby','fixture-bobby-password')
            self.assertEqual(c.get('/api/tasks/'+task['id']).status_code,404)
            self.assertEqual(c.get('/api/tasks/'+task['id']+'/video').status_code,404)
            self.assertEqual(c.get('/api/projects/'+project['id']).status_code,404)
            self.assertEqual(c.put('/api/projects/'+project['id'],headers=h,json={'title':'attack','revision':1}).status_code,404)
            items=c.get('/api/library').json()['items'];self.assertEqual(len(items),1);self.assertEqual(items[0]['id'],own['id'])
    def test_user_queue_limits_and_session_expiry(self):
        with as_user(self.a):
            self.store.create(TaskRequest(prompt='one').parameters());self.store.create(TaskRequest(prompt='two').parameters())
            from video_platform.store import QueueFull
            with self.assertRaises(QueueFull):self.store.create(TaskRequest(prompt='three').parameters())
        token,user,csrf=self.auth.login('alice','fixture-alice-password')
        with self.auth.connect() as db:db.execute('UPDATE sessions SET expires=?',(time.time()-1,))
        self.assertIsNone(self.auth.session(token))
    def test_login_throttle_does_not_echo_password(self):
        with patch('video_platform.api.ROOT',self.root),TestClient(self.app()) as c:
            for _ in range(10):self.assertEqual(c.post('/api/auth/login',json={'username':'nobody','password':'fixture-hidden-password'}).status_code,401)
            response=c.post('/api/auth/login',json={'username':'nobody','password':'fixture-hidden-password'})
            self.assertEqual(response.status_code,429);self.assertNotIn('fixture-hidden-password',response.text)

    def test_password_change_revokes_old_session_and_audit_has_no_secrets(self):
        with patch('video_platform.api.ROOT',self.root),TestClient(self.app()) as c:
            h=self.login(c)
            response=c.post('/api/auth/password',headers=h,json={'current_password':'fixture-alice-password','new_password':'fixture-new-alice-password'})
            self.assertEqual(response.status_code,200);self.assertEqual(c.get('/api/tasks').status_code,401)
            self.assertEqual(c.post('/api/auth/login',json={'username':'alice','password':'fixture-alice-password'}).status_code,401)
            self.login(c,'alice','fixture-new-alice-password')
            with self.auth.connect() as db:rows=[tuple(r) for r in db.execute('SELECT * FROM audit')]
            self.assertNotIn('fixture-new-alice-password',str(rows));self.assertNotIn('fixture-alice-password',str(rows))
