import './session.js?v=shared-shell-20261025-v1';
await window.frameseqSession;
const $=id=>document.getElementById(id);
let providers=[],connections=[],editing=null,enabled=false,busy=false;
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
async function api(path,options={}){const r=await fetch('/api/video-api'+path,options);const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'请检查填写内容。');return d;}
function choose(provider,model,region){
  const p=providers.find(p=>p.id===provider);if(!p)return;
  $('provider').value=p.id;
  for(const [id,values,current] of [['api-model',p.models,model],['region',p.regions,region]]){
    $(id).replaceChildren(...values.map(value=>{const o=node('option',value);o.value=value;return o;}));
    if(values.includes(current))$(id).value=current;
  }
  $('workspace-label').hidden=p.id!=='wan';$('workspace').required=p.id==='wan';
  $('provider-docs').href=p.docs;
  $('provider-note').textContent=p.id==='wan'?'Wan 3.0需要百炼工作空间及匹配的区域，具体权限以官方控制台为准。':p.id==='seedance'?'请使用火山方舟视频API密钥，并开通所选Seedance模型的调用权限。':'中国站与国际站密钥应选择对应区域；请使用具有视频API权限的密钥。';
}
function reset(){editing=null;$('connection-form').reset();$('form-title').textContent='添加视频模型';$('api-key').required=true;choose(providers[0]?.id);$('message').textContent='';}
function render(){
  $('connection-count').textContent=connections.length+' / 8';$('connections').replaceChildren();
  if(!connections.length)$('connections').append(node('p','还没有添加连接。保存后，这个账号的视频创作页就会出现新的 API 模型选项。','api-empty'));
  for(const c of connections){
    const card=node('article',undefined,'api-connection');card.append(node('h3',c.label),node('p',c.model+' · '+c.region),node('small','密钥已保存 · 尚未验证调用'));
    const actions=node('div',undefined,'api-actions'),edit=node('button','编辑','secondary-button'),remove=node('button','删除连接','secondary-button'),use=node('a','去创作 ↗','secondary-button');
    edit.type=remove.type='button';use.href='/studio?connection='+c.id;
    edit.onclick=()=>{editing=c;$('form-title').textContent='编辑连接';$('label').value=c.label;choose(c.provider,c.model,c.region);$('workspace').value=c.workspace||'';$('api-key').value='';$('api-key').required=false;$('message').textContent='密钥留空可保留原密钥。';$('label').focus();};
    remove.onclick=async()=>{if(busy||!confirm('删除这个账号的连接？尚未提交的任务将取消，已提交的任务仍会查询。'))return;busy=true;remove.disabled=true;try{await api('/connections/'+c.id,{method:'DELETE'});if(editing?.id===c.id)reset();await refresh();$('message').textContent='连接已删除，已有作品保留。';}catch(e){$('message').textContent=e.message;}finally{busy=false;remove.disabled=false;}};
    actions.append(edit,use,remove);card.append(actions);$('connections').append(card);
  }
}
async function refresh(){const state=await api('/connections');connections=state.items;enabled=state.enabled;$('save').disabled=!enabled;$('service-state').textContent=enabled?(state.worker_ready?'视频API队列就绪 · 本地模型独立运行':'配置可保存 · 视频API队列暂未就绪'):'视频API存储尚未启用，请联系平台管理员';render();}
$('provider').onchange=()=>choose($('provider').value);
$('reset').onclick=reset;
$('connection-form').onsubmit=async event=>{
  event.preventDefault();if(busy||!enabled)return;busy=true;$('save').disabled=true;
  const body={provider:$('provider').value,label:$('label').value.trim(),model:$('api-model').value,region:$('region').value,workspace:$('provider').value==='wan'?$('workspace').value.trim():'',revision:editing?.revision||0};
  if($('api-key').value)body.api_key=$('api-key').value;
  try{await api('/connections'+(editing?'/'+editing.id:''),{method:editing?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});reset();await refresh();$('message').textContent='连接已加密保存。现在可在创作页选择它；本次没有提交生成或扣费。';}
  catch(e){$('message').textContent=e.message;}
  finally{delete body.api_key;$('api-key').value='';busy=false;$('save').disabled=!enabled;}
};
try{const state=await api('/providers');providers=state.providers;$('provider').replaceChildren(...providers.map(p=>{const o=node('option',p.name);o.value=p.id;return o;}));reset();await refresh();}catch(e){$('service-state').textContent=e.message;}
