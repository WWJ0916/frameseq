import json
from pathlib import Path
import tempfile
import time
import unittest
import httpx
from fastapi.testclient import TestClient
from video_platform.friend_gateway import create_gateway


class InvitedAdminGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'.secrets').mkdir()
        self.config={'enabled':True,'expires_at':time.time()+3600,'public_origin':'https://friends.example.test',
            'allowed_user_ids':['friend'],'allowed_usernames':['friend_test'],
            'allowed_admin_ids':['trial'],'allowed_admin_usernames':['admin_trial'],'daily_limit':10}
        self.save();self.writes=[]
        self.client=TestClient(create_gateway(self.root,httpx.MockTransport(self.upstream)),base_url='https://friends.example.test')
    def tearDown(self):self.client.close();self.tmp.cleanup()
    def save(self):(self.root/'.secrets/friend-gateway.json').write_text(json.dumps(self.config))
    def upstream(self,request):
        cookie=request.headers.get('cookie','')
        identity='trial' if '=trial' in cookie else 'friend' if '=friend' in cookie else 'operator' if '=operator' in cookie else None
        def user(uid):return {'id':uid,'username':'admin_trial' if uid=='trial' else 'friend_test','role':'creator' if uid=='friend' else 'admin'}
        if request.url.path=='/api/auth/me':return httpx.Response(200,json={'user':user(identity) if identity else None,'csrf':'csrf'})
        if request.url.path=='/api/auth/login':
            uid='trial' if json.loads(request.content)['username']=='admin_trial' else 'friend'
            return httpx.Response(200,json={'user':user(uid),'csrf':'csrf'},headers={'set-cookie':f'frameseq_session={uid}; HttpOnly; SameSite=lax; Path=/'})
        if request.method not in {'GET','HEAD'}:
            if request.headers.get('x-csrf-token')!='csrf':return httpx.Response(403,json={'detail':'CSRF'})
            self.writes.append((request.url.path,dict(request.headers)))
        if request.url.path=='/admin':return httpx.Response(200,text="<html><head></head><body><a href='/admin'>管理</a></body></html>",headers={'content-type':'text/html'})
        return httpx.Response(200,json={'ok':True})
    def sign_in(self,identity='trial'):self.client.cookies.set('frameseq_session',identity)
    def test_only_separately_invited_admin_can_log_in(self):
        result=self.client.post('/api/auth/login',headers={'origin':'https://friends.example.test'},json={'username':'admin_trial','password':'fixture-only'})
        self.assertEqual(result.status_code,200);self.assertIn('Secure',result.headers['set-cookie'])
        self.assertEqual(self.client.get('/api/admin/overview').status_code,200)
        self.assertNotIn("a[href='/admin']",self.client.get('/admin').text)
        self.assertIn('administration',self.client.get('/api/experience').json()['features'])
    def test_creator_cannot_access_admin_or_inflate_own_role(self):
        self.sign_in('friend')
        for method,path in [('GET','/admin'),('GET','/assets/admin.js'),('GET','/api/admin/users'),('PATCH','/api/admin/users/'+'a'*32+'/status')]:
            self.assertEqual(self.client.request(method,path,headers={'origin':'https://friends.example.test','x-csrf-token':'csrf'}).status_code,404)
        self.assertEqual(self.writes,[])
    def test_operator_and_revoked_trial_denied(self):
        self.sign_in('operator');self.assertEqual(self.client.get('/admin').status_code,403)
        self.sign_in();self.config['allowed_admin_ids']=[];self.save()
        self.assertEqual(self.client.get('/admin').status_code,403)
    def test_admin_writes_require_origin_and_upstream_csrf(self):
        self.sign_in();path='/api/admin/users/'+'a'*32+'/status'
        for headers in ({},{'origin':'https://evil.test'},{'origin':'https://friends.example.test'}):
            self.assertEqual(self.client.patch(path,json={},headers=headers).status_code,403)
        self.assertEqual(self.writes,[])
        self.assertEqual(self.client.patch(path,json={},headers={'origin':'https://friends.example.test','x-csrf-token':'csrf'}).status_code,200)
        self.assertEqual(self.writes[-1][1]['origin'],'http://127.0.0.1:18080')
    def test_no_generic_proxy_or_paid_routes_even_for_admin(self):
        self.sign_in()
        for path in ('/api/admin/secrets','/api/admin/users/invalid/status','/api/auth/invites','/api/cloud/tasks','/openapi.json','/assets/h3-live.html'):
            for method in ('GET','POST'):
                self.assertEqual(self.client.request(method,path,headers={'origin':'https://friends.example.test','x-csrf-token':'csrf'}).status_code,404)
    def test_anonymous_admin_redirect_and_expiry_still_enforced(self):
        self.assertEqual(self.client.get('/admin',follow_redirects=False).headers['location'],'/login?next=/admin')
        self.sign_in();self.config['expires_at']=time.time()-1;self.save()
        self.assertEqual(self.client.get('/api/admin/overview').status_code,503)

