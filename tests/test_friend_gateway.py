import json
from pathlib import Path
import tempfile
import time
import unittest
import httpx
from fastapi.testclient import TestClient
from video_platform.friend_gateway import create_gateway,Admission

class FriendGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'.secrets').mkdir()
        self.config={'enabled':True,'expires_at':time.time()+3600,'public_origin':'https://friends.example.test',
                     'allowed_user_ids':['friend-id'],'allowed_usernames':['friend_test']}
        self.save();self.calls=[];self.active=False;self.maintenance=False
        self.client=TestClient(create_gateway(self.root,httpx.MockTransport(self.upstream)),base_url='https://friends.example.test')
    def tearDown(self):self.client.close();self.tmp.cleanup()
    def save(self):(self.root/'.secrets/friend-gateway.json').write_text(json.dumps(self.config))
    def upstream(self,request):
        self.calls.append((request.method,request.url.path,dict(request.headers)))
        token=request.headers.get('cookie','')
        user={'id':'friend-id','role':'creator','username':'friend_test'} if 'friend-session' in token else {'id':'operator','role':'admin'} if 'admin-session' in token else None
        if request.url.path=='/api/auth/me':return httpx.Response(200,json={'authenticated':bool(user),'user':user,'csrf':'csrf' if user else None})
        if request.url.path=='/api/auth/login':return httpx.Response(200,json={'user':{'id':'friend-id','role':'creator'},'csrf':'csrf'},headers={'set-cookie':'frameseq_session=friend-session; HttpOnly; SameSite=lax; Path=/'})
        if request.url.path=='/api/tasks' and request.method=='GET':return httpx.Response(200,json={'items':[{'status':'queued'}] if self.active else []})
        if request.url.path=='/api/video-api/tasks' and request.method=='GET':return httpx.Response(200,json={'items':[]})
        if request.url.path=='/api/tasks' and request.method=='POST':
            if self.maintenance:return httpx.Response(503,json={'detail':'maintenance'},headers={'X-Task-Accepted':'no'})
            return httpx.Response(202,json={'id':'new-task','status':'queued'})
        if request.url.path.endswith('/video'):return httpx.Response(206,content=b'video',headers={'content-type':'video/mp4','content-range':'bytes 0-4/5'})
        if request.url.path in {'/studio','/projects','/storyboard','/edits'}:
            return httpx.Response(200,text="<html><head></head><body><a href='/projects'>项目</a><a href='/storyboard'>分镜</a><a href='/edits'>片段</a><a href='/admin'>管理</a></body></html>",headers={'content-type':'text/html'})
        return httpx.Response(200,content=b'ok',headers={'content-type':'text/plain'})
    def post(self,path='/api/tasks',key='one',body=None,origin='https://friends.example.test'):
        return self.client.post(path,json=body or {'model':'local-h3','prompt':'rain'},headers={'origin':origin,'idempotency-key':key,'x-csrf-token':'csrf'})
    def sign_in(self):self.client.cookies.set('frameseq_session','friend-session')
    def test_closed_by_default_and_host_allowlist(self):
        self.config['enabled']=False;self.save();self.assertEqual(self.client.get('/').status_code,503)
        self.config['enabled']=True;self.save();self.assertEqual(self.client.get('/',headers={'host':'evil.test'}).status_code,400)
    def test_private_routes_require_auth_and_admin_credentials_never_forwarded(self):
        self.assertEqual(self.client.get('/api/models').status_code,401)
        self.assertEqual(self.client.get('/studio',follow_redirects=False).status_code,303)
        self.assertEqual(self.post('/api/auth/login',body={'username':'admin','password':'private'}).status_code,403)
        self.assertFalse(any(p=='/api/auth/login' for _,p,_ in self.calls))
    def test_admin_session_and_unknown_creator_denied(self):
        self.client.cookies.set('frameseq_session','admin-session');self.assertEqual(self.client.get('/studio').status_code,403)
        self.sign_in();self.config['allowed_user_ids']=[];self.save();self.assertEqual(self.client.get('/api/models').status_code,403)
    def test_admin_registration_and_cloud_routes_cannot_be_used(self):
        self.sign_in()
        for path in ('/admin','/api/admin/users','/api/auth/register','/api/auth/invites','/api/cloud/tasks','/openapi.json','/assets/admin.js','/api/plans/id/submit'):
            self.assertEqual(self.client.get(path).status_code,404,path)
            self.assertEqual(self.post(path).status_code,404,path)
    def test_login_sets_secure_cookie_and_rejects_cross_origin(self):
        r=self.post('/api/auth/login',body={'username':'friend_test','password':'known-to-user'})
        self.assertEqual(r.status_code,200);self.assertIn('Secure',r.headers['set-cookie']);self.assertIn('HttpOnly',r.headers['set-cookie'])
        self.assertEqual(self.post(origin='https://evil.test').status_code,403)
    def test_daily_limit_idempotent_conflict_and_single_pending(self):
        self.sign_in();self.assertEqual(self.post().status_code,202)
        self.active=True;self.assertEqual(self.post().status_code,202)
        self.assertEqual(self.post(key='two').status_code,429)
        self.assertEqual(self.post(body={'model':'local-h3','prompt':'changed'}).status_code,409)
        self.active=False;self.assertEqual(self.post(key='two').status_code,202);self.assertEqual(self.post(key='three').status_code,429)
    def test_only_local_models_and_idempotency_required(self):
        self.sign_in();self.assertEqual(self.post(body={'model':'minimax-h3'}).status_code,422)
        self.assertEqual(self.post(key='').status_code,400)
    def test_legacy_wan_payload_defaults_to_local_wan(self):
        self.sign_in()
        self.assertEqual(self.post(body={'prompt':'sea','preset':'fast','seed':42}).status_code,202)
        self.assertEqual(self.calls[-1][1],'/api/tasks')
    def test_explicit_maintenance_rejection_does_not_consume_daily_allowance(self):
        self.sign_in();self.maintenance=True
        for key in ('maintenance-one','maintenance-two','maintenance-three'):
            self.assertEqual(self.post(key=key).status_code,503)
        self.maintenance=False
        self.assertEqual(self.post(key='accepted-one').status_code,202)
        self.assertEqual(self.post(key='accepted-two').status_code,202)
        self.assertEqual(self.post(key='limited-three').status_code,429)
    def test_cookie_and_range_forwarding_without_shared_client_session(self):
        self.sign_in();r=self.client.get('/api/tasks/'+'a'*32+'/video',headers={'Range':'bytes=0-4'})
        self.assertEqual(r.status_code,206);self.assertEqual(r.content,b'video')
        self.assertEqual(self.calls[-1][2]['range'],'bytes=0-4')
        self.client.cookies.clear();self.assertEqual(self.client.get('/api/models').status_code,401)
    def test_rejected_submission_cannot_bypass_later_daily_limit(self):
        a=Admission(self.root/'data/test-admission.sqlite3')
        self.assertEqual(a.reserve('u','bad','body',False),0);a.finish('u','bad','rejected')
        for key in ('one','two'):
            self.assertEqual(a.reserve('u',key,'body',False),0);a.finish('u',key,'accepted')
        self.assertEqual(a.reserve('u','bad','body',False),429)
        a.finish('u','one','rejected');self.assertEqual(a.reserve('u','third','body',False),429)
    def test_expired_gateway_and_oversized_body_fail_closed(self):
        self.config['expires_at']=time.time()-1;self.save();self.assertEqual(self.client.get('/').status_code,503)
        self.config['expires_at']=time.time()+3600;self.save();self.sign_in()
        r=self.client.post('/api/tasks',content=b'x'*(1024*1024+1),headers={'origin':'https://friends.example.test'})
        self.assertEqual(r.status_code,413)

    def test_customer_pages_and_structured_routes_open_without_admin_or_arbitrary_proxy(self):
        self.sign_in()
        for path in ('/projects','/storyboard','/edits','/api/projects','/api/projects/'+'a'*32,
                     '/api/storyboards/'+'b'*32,'/api/edits/capabilities','/api/exports/'+'c'*32+'/video'):
            self.assertIn(self.client.get(path).status_code,(200,206),path)
        for path in ('/api/projects/not-a-valid-id','/api/admin/overview','/api/assistant/draft','/api/cloud/tasks'):
            self.assertEqual(self.post(path).status_code,404)

    def test_project_and_board_writes_require_origin_and_preserve_csrf(self):
        self.sign_in()
        for path in ('/api/projects/'+'a'*32,'/api/storyboards/'+'b'*32):
            self.assertEqual(self.client.put(path,json={},headers={'origin':'https://evil.test'}).status_code,403)
            self.assertEqual(self.client.put(path,json={},headers={'origin':'https://friends.example.test','x-csrf-token':'csrf'}).status_code,200)
            self.assertEqual(self.calls[-1][2]['origin'],'http://127.0.0.1:18080')
            self.assertEqual(self.calls[-1][2]['x-csrf-token'],'csrf')

    def test_ten_daily_submissions_keep_previous_usage_and_idempotency(self):
        self.sign_in();self.assertEqual(self.post(key='already-used').status_code,202)
        self.config['daily_limit']=10;self.save()
        self.assertEqual(self.client.get('/api/experience').json()['remaining_today'],9)
        for index in range(9):self.assertEqual(self.post(key='next-'+str(index)).status_code,202)
        self.assertEqual(self.post(key='already-used').status_code,202)
        self.assertEqual(self.post(key='eleventh').status_code,429)
        self.assertEqual(self.client.get('/api/experience').json()['used_today'],10)

    def test_oversized_limit_configuration_fails_closed(self):
        for value in (0,-1,True,'10',101):
            self.config['daily_limit']=value;self.save();self.assertEqual(self.client.get('/').status_code,503)

    def test_customer_navigation_is_not_hidden_by_preview_html_filter(self):
        self.sign_in();self.config['daily_limit']=10;self.save()
        page=self.client.get('/studio').text
        self.assertIn('每天10条',page)
        self.assertIn('data-friend-preview',page)
        for path in ('/projects','/storyboard','/edits'):
            self.assertIn("href='"+path+"'",page)
            self.assertNotIn("a[href='"+path+"']",page)
        self.assertIn("a[href='/admin']",page)
