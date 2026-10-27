"""Invite-only accounts, opaque cookie sessions and CSRF checks; no external identity provider."""
from contextlib import contextmanager
import hashlib,hmac,json,os,re,secrets,sqlite3,time,uuid
from pathlib import Path
from urllib.parse import urlsplit,quote
from fastapi import APIRouter,HTTPException,Request,Response
from fastapi.responses import JSONResponse,RedirectResponse
from pydantic import BaseModel,ConfigDict,Field
from .ownership import LEGACY_OWNER,identity,is_admin

COOKIE='frameseq_session'
ITERATIONS=600_000

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def hash_password(password,salt=None):
    salt=salt or secrets.token_hex(16)
    return f'pbkdf2_sha256${ITERATIONS}${salt}$'+hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),ITERATIONS).hex()
def check_password(password,encoded):
    kind,rounds,salt,expected=encoded.split('$')
    actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(rounds)).hex()
    return hmac.compare_digest(actual,expected)

class AuthStore:
    def __init__(self,root):
        self.root=Path(root);self.path=self.root/'data/auth.sqlite3';self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE,name TEXT,password_hash TEXT,role TEXT,created_at REAL);
              CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT,csrf TEXT,expires REAL,last_seen REAL);
              CREATE TABLE IF NOT EXISTS invites(id TEXT PRIMARY KEY,created_by TEXT,expires REAL,used_by TEXT);
              CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,count INTEGER,expires REAL);
              CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,actor TEXT,event TEXT,target TEXT,created_at REAL);''')
            if 'status' not in {r[1] for r in db.execute('PRAGMA table_info(users)')}:
                db.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    def audit(self,actor,event,target=''):
        with self.connect() as db:db.execute('INSERT INTO audit VALUES(?,?,?,?,?)',(uuid.uuid4().hex,actor,event,target[:200],time.time()))
    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        except Exception:db.rollback();raise
        finally:db.close()
    def limit(self,key,maximum,window):
        stamp=time.time();key=digest(key)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE expires<?',(stamp,))
            row=db.execute('SELECT * FROM attempts WHERE id=?',(key,)).fetchone()
            if row and row['count']>=maximum:raise HTTPException(429,'操作过于频繁，请稍后再试。')
            db.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',(key,row['count']+1 if row else 1,row['expires'] if row else stamp+window))
    def create_user(self,username,password,name,invite=None,admin=False):
        username=username.strip().lower()
        if not re.fullmatch('[a-z0-9_]{3,32}',username):raise ValueError('用户名为3–32位小写字母、数字或下划线。')
        if not 12<=len(password)<=128:raise ValueError('密码长度应为12–128个字符。')
        encoded=hash_password(password);uid=LEGACY_OWNER if admin else uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if admin:
                if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():raise ValueError('管理员已经创建，不重置现有密码。')
            else:
                row=db.execute('SELECT * FROM invites WHERE id=?',(digest(invite or ''),)).fetchone()
                if not row or row['used_by'] or row['expires']<time.time():raise ValueError('邀请码无效、已使用或过期。')
            try:db.execute('INSERT INTO users(id,username,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)',(uid,username,name.strip() or username,encoded,'admin' if admin else 'creator',time.time()))
            except sqlite3.IntegrityError:raise ValueError('用户名不可用。') from None
            if not admin:db.execute('UPDATE invites SET used_by=? WHERE id=?',(uid,digest(invite)))
        return dict(id=uid,username=username,name=name.strip() or username,role='admin' if admin else 'creator')
    def login(self,username,password):
        with self.connect() as db:row=db.execute('SELECT * FROM users WHERE username=?',(username.strip().lower(),)).fetchone()
        # Similar-cost verification for unknown usernames.
        valid=check_password(password,row['password_hash'] if row else f'pbkdf2_sha256${ITERATIONS}$'+('0'*32)+'$'+('0'*64))
        if not row or not valid or row['status']!='active':raise HTTPException(401,'用户名、密码或账号状态不正确。')
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(24);stamp=time.time()
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE expires<? OR last_seen<?',(stamp,stamp-3600))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(digest(token),row['id'],csrf,stamp+8*3600,stamp))
        return token,dict(id=row['id'],username=row['username'],name=row['name'],role=row['role']),csrf
    def session(self,token):
        if not token or len(token)>200:return None
        with self.connect() as db:
            row=db.execute('''SELECT u.id,u.username,u.name,u.role,s.csrf,s.expires,s.last_seen FROM sessions s
              JOIN users u ON u.id=s.user_id WHERE s.id=? AND u.status='active' ''',(digest(token),)).fetchone()
            if not row or row['expires']<time.time() or row['last_seen']<time.time()-3600:return None
            if row['last_seen']<time.time()-60:db.execute('UPDATE sessions SET last_seen=? WHERE id=?',(time.time(),digest(token)))
        return {k:row[k] for k in ('id','username','name','role','csrf')}
    def change_password(self,uid,old,new):
        if not 12<=len(new)<=128:raise ValueError('新密码长度应为12–128个字符。')
        with self.connect() as db:row=db.execute('SELECT password_hash FROM users WHERE id=?',(uid,)).fetchone()
        if not row or not check_password(old,row[0]):raise ValueError('当前密码不正确。')
        encoded=hash_password(new)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');db.execute('UPDATE users SET password_hash=? WHERE id=?',(encoded,uid));db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        self.audit(uid,'password_changed_all_sessions_revoked')
    def invite(self,user_id):
        code=secrets.token_urlsafe(24)
        with self.connect() as db:db.execute('INSERT INTO invites VALUES(?,?,?,NULL)',(digest(code),user_id,time.time()+7*86400))
        return code

def install_auth(app,root,required=False,public_origin=None):
    store=AuthStore(root)
    app.state.auth=store;app.state.auth_required=required
    public_paths={'/','/explore','/login','/api/public/catalog','/api/public/campaigns','/api/public/demo','/api/public/demo/video','/api/public/demo/poster','/api/public/demo/subtitles','/api/auth/me','/api/auth/login','/api/auth/register'}
    @app.middleware('http')
    async def protect(request,call_next):
        path=request.url.path;user=store.session(request.cookies.get(COOKIE)) if required else identity.get()
        request.state.user=user
        if request.method not in {'GET','HEAD','OPTIONS'}:
            length=request.headers.get('content-length','0')
            if not length.isdigit() or int(length)>4*1024*1024 or request.headers.get('transfer-encoding'):
                return JSONResponse({'detail':'请求体过大或传输格式不受支持。'},status_code=413)
        public=path in public_paths or (path.startswith('/assets/') and path.endswith(('.css','.js','.mjs','.svg','.woff2')))
        if required and not user and not public:
            if path=='/api/health':return JSONResponse({'status':'ok','access':'invite_preview'})
            if path.startswith('/api/'):return JSONResponse({'detail':'请先登录后使用个人创作空间。'},status_code=401)
            return RedirectResponse('/login?next='+quote(path,safe=''),status_code=303)
        if required and request.method not in {'GET','HEAD','OPTIONS'}:
            supplied=request.headers.get('origin')
            expected=public_origin or f'{request.url.scheme}://{request.url.netloc}'
            if supplied and supplied.rstrip('/')!=expected.rstrip('/'):
                return JSONResponse({'detail':'请求来源不匹配。'},status_code=403)
            if user and path not in {'/api/auth/login','/api/auth/register'} and not hmac.compare_digest(request.headers.get('x-csrf-token',''),user['csrf']):
                return JSONResponse({'detail':'会话校验失败，请刷新后重试。'},status_code=403)
        if required and user and user['role']!='admin':
            if (path.startswith('/assets/') and path.endswith('.json')) or path=='/assets/h3-live.html':
                return JSONResponse({'detail':'记录不存在。'},status_code=404)
            if (path.startswith('/api/cloud') or path.startswith('/api/agent') or path.startswith('/api/auth/invites')) and request.method not in {'GET','HEAD'}:
                return JSONResponse({'detail':'预览阶段此操作仅向管理员开放，未提交付费任务。'},status_code=403)
            if path.startswith('/api/cloud/tasks/'):
                return JSONResponse({'detail':'任务不存在。'},status_code=404)
        token=identity.set(user or {'id':'anonymous','role':'visitor','name':'访客'}) if required else None
        try:
            response=await call_next(request)
        finally:
            if token is not None:identity.reset(token)
        response.headers.update({'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'same-origin'})
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if required and user and request.method not in {'GET','HEAD','OPTIONS'}:
            store.audit(user['id'],'http_'+request.method.lower()+'_'+str(response.status_code),path)
        # Workspace HTML contains the current app shell and must not outlive a rollout.
        if path.startswith('/api/') or 'text/html' in response.headers.get('content-type',''):
            response.headers['Cache-Control']='no-store'
        return response
    router=APIRouter(prefix='/api/auth')
    class Login(BaseModel):
        model_config=ConfigDict(extra='forbid')
        username:str=Field(min_length=3,max_length=32)
        password:str=Field(min_length=1,max_length=128)
    class Register(Login):
        name:str=Field(default='',max_length=60)
        invite:str=Field(min_length=10,max_length=100)
    class PasswordChange(BaseModel):
        model_config=ConfigDict(extra='forbid')
        current_password:str=Field(min_length=1,max_length=128)
        new_password:str=Field(min_length=12,max_length=128)
    @router.get('/me')
    def me(request:Request):
        user=request.state.user
        return {'authenticated':bool(user),'user':{k:v for k,v in user.items() if k!='csrf'} if user else None,'csrf':user.get('csrf') if user else None,'required':required,'registration':'invite_only','public_release':False}
    @router.post('/login')
    def login(body:Login,request:Request,response:Response):
        store.limit('login:'+str(request.client.host),10,900)
        token,user,csrf=store.login(body.username,body.password)
        store.audit(user['id'],'login_success')
        response.set_cookie(COOKIE,token,httponly=True,samesite='lax',secure=bool(public_origin and public_origin.startswith('https://')),max_age=8*3600,path='/')
        return {'user':user,'csrf':csrf}
    @router.post('/register',status_code=201)
    def register(body:Register,request:Request):
        store.limit('signup:'+str(request.client.host),5,3600)
        try:return store.create_user(body.username,body.password,body.name,body.invite)
        except ValueError as error:raise HTTPException(422,str(error))
    @router.post('/logout')
    def logout(request:Request,response:Response):
        with store.connect() as db:db.execute('DELETE FROM sessions WHERE id=?',(digest(request.cookies.get(COOKIE,'')),))
        response.delete_cookie(COOKIE,path='/');return {'signed_out':True}
    @router.post('/password')
    def password(body:PasswordChange,request:Request,response:Response):
        if not request.state.user or not required:raise HTTPException(401,'请先登录。')
        store.limit('password:'+request.state.user['id'],5,3600)
        try:store.change_password(request.state.user['id'],body.current_password,body.new_password)
        except ValueError as error:raise HTTPException(422,str(error))
        response.delete_cookie(COOKIE,path='/');return {'changed':True,'sessions_revoked':True}
    @router.post('/invites',status_code=201)
    def invite(request:Request):
        if not is_admin():raise HTTPException(403,'仅管理员可以创建邀请码。')
        store.limit('invite:'+identity.get()['id'],20,86400)
        return {'code':store.invite(identity.get()['id']),'expires_in_days':7,'uses':1}
    app.include_router(router)
