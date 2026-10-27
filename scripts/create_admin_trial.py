"""Explicit operator CLI: create an additional local administrator, never promote a customer."""
import argparse,json,os,re,secrets,time,uuid
from pathlib import Path
from video_platform.auth import AuthStore,hash_password
from video_platform.config import ROOT

def create_admin_trial(root,username='admin_trial'):
    root=Path(root);store=AuthStore(root)
    if not re.fullmatch(r'[a-z0-9_]{3,32}',username):raise ValueError('Invalid username')
    folder=root/'.secrets';folder.mkdir(mode=0o700,exist_ok=True)
    credential=folder/('admin-trial-'+username+'.json')
    if credential.exists():
        saved=json.loads(credential.read_text())
        with store.connect() as db:row=db.execute('SELECT id,role,status FROM users WHERE username=?',(username,)).fetchone()
        if not row or row['role']!='admin' or row['status']!='active' or row['id']!=saved['id']:raise ValueError('Existing credential/account mismatch; do not reset')
        return {'created':False,'username':username,'credential_file':str(credential)}
    password=secrets.token_urlsafe(24);uid=uuid.uuid4().hex
    encoded=hash_password(password)
    # Stage the private credential before the transaction; remove only this new file if insertion fails.
    fd=os.open(credential,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w') as stream:json.dump({'id':uid,'username':username,'password':password,'role':'admin','access':'operator loopback; public gateway requires separate explicit admin invitation','created_at':time.time()},stream)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():raise ValueError('Bootstrap the initial operator first')
            if db.execute('SELECT 1 FROM users WHERE username=?',(username,)).fetchone():raise ValueError('Username already exists; never promote/reset')
            db.execute('INSERT INTO users(id,username,name,password_hash,role,created_at,status) VALUES(?,?,?,?,?,?,?)',(uid,username,'后台体验管理员',encoded,'admin',time.time(),'active'))
            db.execute('INSERT INTO audit VALUES(?,?,?,?,?)',(uuid.uuid4().hex,uid,'operator_cli_additional_admin',uid,time.time()))
    except BaseException:
        credential.unlink(missing_ok=True)
        raise
    return {'created':True,'username':username,'credential_file':str(credential)}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--username',default='admin_trial');args=parser.parse_args()
    print(json.dumps(create_admin_trial(ROOT,args.username)))
if __name__=='__main__':main()
