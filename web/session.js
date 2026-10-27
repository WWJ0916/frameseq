/* Same-origin session bridge for the existing workspaces. Credentials stay in HttpOnly cookies. */
(() => {
  if(window.frameseqSession)return;
  // Keep path, project/shot parameters and fragment; escape previously cached HTML.
  const workspacePaths=new Set(['/studio','/studio-classic','/projects','/storyboard','/edits','/video-api','/assistant','/admin','/explore']);
  document.addEventListener?.('click',event=>{
    const link=event.target.closest?.('a[href]');
    if(!link||link.hasAttribute('download')||(link.getAttribute('href')||'').startsWith('#'))return;
    const url=new URL(link.href,location.href);
    if(url.origin!==location.origin||!workspacePaths.has(url.pathname))return;
    url.searchParams.set('ui','shared-shell-20261025-v1');
    link.href=url.pathname+url.search+url.hash;
  },true);
  const original=window.fetch.bind(window);
  // A transient network failure must not permanently reject the shared promise.
  // Never assume an anonymous identity while authentication is unavailable.
  async function loadSession(){
    for(;;){
      try{
        const response=await original('/api/auth/me',{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(12000)});
        if(!response.ok)throw Error('Session service unavailable');
        const session=await response.json();
        if(typeof session.authenticated!=='boolean')throw Error('Invalid session response');
        const note=document.getElementById('form-message');
        if(note?.dataset.sessionRetry==='true'){note.textContent='';delete note.dataset.sessionRetry;}
        return session;
      }catch{
        const status=document.getElementById('connection');if(status)status.textContent='连接暂时中断，正在重试';
        const note=document.getElementById('form-message');
        if(note){note.textContent='暂时无法连接服务，正在自动重试；请保留当前页面。';note.dataset.sessionRetry='true';}
        await new Promise(resolve=>setTimeout(resolve,3000));
      }
    }
  }
  const ready=loadSession();
  window.frameseqSession=ready;
  window.fetch=async(input,options={})=>{
    const url=new URL(typeof input==='string'?input:input.url,location.href);
    if(url.origin!==location.origin)return original(input,options);
    const session=await ready;
    const method=(options.method||'GET').toUpperCase();
    const headers=new Headers(options.headers||{});
    if(!['GET','HEAD','OPTIONS'].includes(method)&&session.csrf)headers.set('X-CSRF-Token',session.csrf);
    return original(input,{...options,headers,credentials:'same-origin'});
  };
  ready.then(session=>{
    const name=document.querySelector('.sidebar-bottom b');if(name&&session.user)name.textContent=session.user.name;
    if(session.user?.role!=='admin')for(const link of document.querySelectorAll('a[href="/assistant"],a[href^="/assistant?"],a[href="/assets/h3-live.html"]'))link.hidden=true;
    const actions=document.querySelector('.top-actions');if(!actions)return;
    if(session.user?.role==='admin'){
      const admin=document.createElement('a');admin.className='secondary-button';admin.textContent='管理后台';admin.href='/admin';actions.append(admin);
    }
    const link=document.createElement('a');link.className='secondary-button';link.textContent=session.authenticated?'账号':'登录';link.href='/login';actions.append(link);
    if(session.authenticated&&session.required){
      const logout=document.createElement('button');logout.className='secondary-button';logout.textContent='退出';logout.onclick=async()=>{await window.fetch('/api/auth/logout',{method:'POST'});location.href='/';};actions.append(logout);
    }
  }).catch(()=>{});
})();
