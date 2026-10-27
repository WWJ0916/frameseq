import {suggestDuration, labels,modelDisplayName} from './studio-state.mjs?v=byok-20261025-v1';
import './session.js?v=shared-shell-20261025-v1';
const session=await window.frameseqSession;
const $ = id => document.getElementById(id);
const uid = () => crypto.randomUUID().replaceAll('-', '');
let project = null, episodeId = null, shotId = null, dirty = false, busy = false, cloud = {}, native480Ready=false,apiModels=[];
function el(tag, text, cls) {const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function button(text, action, cls='secondary-button') {const n=el('button',text,cls);n.type='button';n.onclick=()=>guard(action);return n;}
function message(text, error=false) {$('message').textContent=text;$('message').style.color=error?'var(--error)':'var(--accent)';}
async function api(path, options={}) {const r=await fetch(path,options);const b=await r.json();if(!r.ok)throw Error(typeof b.detail==='string'?b.detail:'请检查名称、时长和输入长度。');return b;}
function json(method, body) {return {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};}
async function guard(fn) {if(busy)return;busy=true;try {await fn();}catch(e){message(e.message,true);}finally{busy=false;}}
function mark() {dirty=true;$('saved-status').textContent='有未保存更改';}
function episode() {return project?.episodes.find(e=>e.id===episodeId);}
function shot() {return episode()?.shots.find(s=>s.id===shotId);}
function documentBody() {return Object.fromEntries(['title','synopsis','style','characters','episodes','revision'].map(k=>[k,project[k]]));}
async function save() {
  if(!project) return;
  const saved=await api(project.id?'/api/projects/'+project.id:'/api/projects',json(project.id?'PUT':'POST',documentBody()));
  project=saved;dirty=false;$('saved-status').textContent=`已保存 · v${saved.revision}`;history.replaceState(null,'','/projects?project='+saved.id+(episodeId?'&episode='+episodeId:''));await list();return saved;
}
async function list() {
  const items=(await api('/api/projects')).items;$('project-list').replaceChildren();
  for(const p of items)$('project-list').append(button(p.title,async()=>{if(dirty)await save();await open(p.id);},'nav-item'+(project?.id===p.id?' active':'')));
}
async function open(id) {project=await api('/api/projects/'+id);dirty=false;episodeId=project.episodes.some(e=>e.id===episodeId)?episodeId:project.episodes[0]?.id;shotId=null;render();}
function newProject() {project={title:'未命名短剧',synopsis:'',style:'',characters:'',episodes:[],versions:[],revision:0};episodeId=null;shotId=null;mark();render();$('project-title').focus();}
function render() {
  $('empty').hidden=!!project;$('workspace').hidden=!project;if(!project)return;
  for(const [id,key] of [['project-title','title'],['synopsis','synopsis'],['style','style'],['characters','characters']])$(id).value=project[key];
  $('saved-status').textContent=dirty?'有未保存更改':`已保存 · v${project.revision}`;
  renderEpisodes();renderEpisode();
}
function renderEpisodes() {
  $('episodes').replaceChildren();
  project.episodes.forEach((e,i)=>{const b=button(`${String(i+1).padStart(2,'0')}  ${e.title}`,()=>{episodeId=e.id;shotId=null;renderEpisodes();renderEpisode();},'episode-tab'+(e.id===episodeId?' active':''));b.append(el('small',`${e.shots.length} 个镜头 · 目标 ${e.target_seconds} 秒`));$('episodes').append(b);});
}
function stats() {
  const e=episode();if(!e)return;
  const planned=e.shots.reduce((n,s)=>n+s.seconds,0);
  const selected=project.versions.filter(v=>v.episode_id===e.id&&v.selected&&v.status==='succeeded');
  const actual=selected.reduce((n,v)=>n+(v.actual_seconds||0),0);
  $('episode-stats').replaceChildren(...[`计划 ${planned} / 目标 ${e.target_seconds} 秒`,`差额 ${planned-e.target_seconds>=0?'+':''}${planned-e.target_seconds} 秒`,`已选片 ${selected.length} / ${e.shots.length}`,`已选素材实测 ${actual.toFixed(2)} 秒`,'本地模型 · 时长为剪辑计划'].map(t=>el('b',t)));
}
function renderEpisode() {
  const e=episode();$('episode-area').hidden=!e;$('no-episode').hidden=!!e;if(!e)return;
  if(project.id)history.replaceState(null,'',`/projects?project=${project.id}&episode=${e.id}`);
  $('episode-number').textContent=`EPISODE ${String(project.episodes.indexOf(e)+1).padStart(2,'0')}`;
  $('episode-title').value=e.title;$('target-seconds').value=e.target_seconds;$('episode-synopsis').value=e.synopsis;
  if(!e.shots.some(s=>s.id===shotId))shotId=e.shots[0]?.id;
  $('shots').replaceChildren();e.shots.forEach((s,i)=>renderShot(s,i));stats();renderVersions();renderExports();
}
function field(label,node) {const n=el('label',label);n.append(node);return n;}
function input(value,update,tag='input') {const n=el(tag);n.value=value;n.oninput=()=>{update(n.value);mark();};return n;}
function renderShot(s,i) {
  const card=el('article',undefined,'shot-card'+(s.id===shotId?' selected':''));card.dataset.shotId=s.id;
  const head=el('div',undefined,'shot-head'),title=input(s.title,v=>s.title=v);title.maxLength=120;title.setAttribute('aria-label',`镜头${i+1}标题`);
  head.append(el('span',String(i+1).padStart(2,'0'),'shot-number'),title);
  const controls=el('div',undefined,'shot-controls');
  for(const [label,delta] of [['↑',-1],['↓',1]]){const b=button(label,()=>{const a=episode().shots,j=i+delta;if(j<0||j>=a.length)return;[a[i],a[j]]=[a[j],a[i]];mark();renderEpisode();});b.disabled=i+delta<0||i+delta>=episode().shots.length;b.setAttribute('aria-label',`镜头${i+1}${delta<0?'上移':'下移'}`);controls.append(b);}
  controls.append(button('移除',()=>{if(project.versions.some(v=>v.shot_id===s.id))throw Error('镜头已有生成版本，请保留历史。');episode().shots.splice(i,1);mark();renderEpisode();}));head.append(controls);
  const body=el('div',undefined,'shot-body'),text=input(s.description,v=>s.description=v,'textarea');text.maxLength=2000;text.rows=4;text.placeholder='主体、动作、场景与镜头；仅描述本镜头。';text.setAttribute('aria-label',`镜头${i+1}画面描述`);
  const narration=input(s.narration,v=>s.narration=v);narration.maxLength=500;narration.placeholder='对白 / 旁白备忘（未合成声音）';const copy=el('div');copy.append(field('画面描述',text),field('对白 / 旁白',narration));
  const options=el('div',undefined,'shot-options'),model=el('select');for(const [value,label] of [['local-h3','H3 · Turbo 8步（约5.17秒）'],['local-wan','Wan · 本地部署'],['local-cogvideo','CogVideoX · 2B 本地'],...apiModels.map(m=>[m.id,m.name])]){const o=el('option',label);o.value=value;model.append(o);}model.value=s.model==='minimax-h3'?'local-h3':s.model;model.onchange=()=>{s.model=model.value;mark();renderEpisode();};
  const seconds=input(s.seconds,v=>{s.seconds=Number(v);stats();});seconds.type='number';seconds.min=4;seconds.max=15;seconds.step=1;
  options.append(field('生成模型',model),field('剪辑计划时长（非原生生成时长）',seconds),button('按描述建议时长',()=>{s.seconds=suggestDuration(s.description);mark();renderEpisode();message('已按句数和字数估算时长；这是节奏参考，可自行修改。');}));
  const preset=el('select');for(const [v,t] of [['fast','Wan 约1秒 · 快速'],['preview','Wan 约2秒 · 预览'],['story','Wan 约5秒 · 低清'],['cinema','Wan 原生480P · 约5秒']]){const o=el('option',t);o.value=v;if(v==='cinema')o.disabled=!native480Ready;preset.append(o);}preset.value=s.preset;preset.onchange=()=>{s.preset=preset.value;mark();};const presetField=field('Wan预设（H3固定约5.17秒，Cog约6秒）',preset);presetField.hidden=s.model!=='local-wan';options.append(presetField);body.append(copy,options);
  const foot=el('div',undefined,'shot-foot'),actions=el('div');const versions=project.versions.filter(v=>v.shot_id===s.id);foot.append(el('p',`${versions.length} 个生成版本 · ${versions.some(v=>v.selected)?'已选片':'尚未选片'} · ${s.model==='local-h3'?'本地H3 Turbo 8步固定约5.17秒':s.model.startsWith('video-api:')?'自带密钥API · 创作页确认费用':'本地实验预设'}`));
  actions.append(button('查看版本',()=>{shotId=s.id;renderEpisode();$('versions-title').scrollIntoView({block:'center',behavior:'smooth'});}),button('带入生成 ↗',async()=>{if(!s.description.trim())throw Error('请先填写这个镜头的画面描述。');shotId=s.id;await save();location.href=`/studio?ui=shared-shell-20261025-v1&project=${project.id}&episode=${episodeId}&shot=${s.id}`;},'primary-button'));foot.append(actions);card.append(head,body,foot);$('shots').append(card);
}
function renderVersions() {
  const s=shot();$('versions-title').textContent=s?`${s.title} · 生成版本`:'镜头版本';$('link-task').disabled=!s;$('versions').replaceChildren();if(!s)return;
  const versions=project.versions.filter(v=>v.shot_id===s.id);
  if(!versions.length)$('versions').append(el('p','还没有关联任务。可带入工作台生成，或关联现有实验素材。','helper'));
  versions.forEach((v,i)=>{const row=el('div',undefined,'version-row'),info=el('div');info.append(el('b',`V${i+1} · ${modelDisplayName({origin:v.origin,parameters:v.snapshot.parameters})} · ${labels[v.status]||v.status}${v.selected?' · ✓ 已采用':''}`,v.selected?'adopted':''),el('p',`${v.task_id.slice(0,8)} · 实测${v.actual_seconds??'待验收'}秒 · ${v.note||'关联任务'}`));
    const details=el('details'),summary=el('summary','查看当时描述与参数');details.append(summary,el('p',v.snapshot.parameters.prompt),el('p',`当时规格 ${v.snapshot.parameters.duration? v.snapshot.parameters.duration+'秒':v.snapshot.parameters.frames+'帧 / '+v.snapshot.parameters.fps+'fps'}；当前分镜计划${s.seconds}秒。修改分镜不会覆盖历史。`));info.append(details);row.append(info);
    if(v.video_url){row.append(button('播放',()=>{const video=$('version-video');video.src=v.video_url;$('video-caption').textContent=`V${i+1} · 实测${v.actual_seconds}秒 · ${v.note}`;$('version-download').href=v.video_url+'?download=true';$('playback').showModal();}));const b=button(v.selected?'已采用':'采用此版',async()=>{if(dirty)await save();project=await api(`/api/projects/${project.id}/versions/${v.id}/choose`,json('POST',{revision:project.revision}));render();message('已记录选片；没有剪辑或生成新视频。');});b.disabled=v.selected||v.quality_rejected;if(v.quality_rejected)b.textContent='画面异常，不能采用';row.append(b);} $('versions').append(row);});
}
async function picker(title, records, describe, select) {$('picker-title').textContent=title;$('picker-items').replaceChildren();for(const r of records){const b=button(describe(r)[0],async()=>{await select(r);$('picker').close();},'picker-row');b.append(el('small',describe(r)[1]));$('picker-items').append(b);}if(!records.length)$('picker-items').append(el('p','没有可选择的记录。'));$('picker').showModal();}
async function linkTask() {
  if(dirty)await save();const records=(await api('/api/library?limit=100')).items;
  await picker('关联已有任务 · 不重新生成',records,t=>[t.parameters.prompt,`${modelDisplayName(t)} · ${labels[t.status]} · ${t.id.slice(0,8)}`],async t=>{project=await api(`/api/projects/${project.id}/versions`,json('POST',{episode_id:episodeId,shot_id:shotId,origin:t.origin,task_id:t.id,revision:project.revision,note:'从历史关联的实验素材，未因本镜头重新生成'}));render();message('已关联真实历史任务；镜头计划与原任务参数可能不同，详见版本记录。');});
}
async function renderExports(){
  const area=$('exports');area.replaceChildren();if(!project?.id)return;
  try{const records=(await api(`/api/projects/${project.id}/exports`)).items.filter(r=>r.episode_id===episodeId);
    if(!records.length)area.append(el('p','选好每个镜头的版本后，可导出本集。素材不足时会提示，不自动补静帧。','helper'));
    for(const r of records){const row=el('div',undefined,'version-row'),info=el('div');info.append(el('b',`本集导出 · ${labels[r.status]||r.status}`),el('p',r.error||`${r.plan.clips.length}个镜头 · ${r.plan.total_seconds}秒 · 版本v${r.plan.project_revision}`));row.append(info);if(r.status==='succeeded'){row.append(button('播放成片',()=>{$('version-video').src=`/api/exports/${r.id}/video`;$('video-caption').textContent=r.result.limitation;$('version-download').href=`/api/exports/${r.id}/video?download=true`;$('playback').showModal();}));const a=el('a','下载成片 ↓','secondary-button');a.href=`/api/exports/${r.id}/video?download=true`;row.append(a);}area.append(row);}
  }catch(e){area.append(el('p',e.message,'helper'));}
}
$('export-episode').onclick=()=>guard(async()=>{if(dirty)await save();const record=await api(`/api/projects/${project.id}/episodes/${episodeId}/export`,json('POST',{revision:project.revision}));message(record.status==='succeeded'?'此前相同选片已导出，直接播放即可。':'剪辑任务已保存；只处理现有素材，不生成新视频或预留API费用。');await renderExports();});
$('save').onclick=()=>guard(async()=>{await save();render();message('项目、剧集与分镜已保存；没有调用模型。');});
$('reload').onclick=()=>guard(async()=>{if(dirty)throw Error('还有未保存的输入，请先保存；发生版本冲突时可先复制文本，再刷新页面载入最新记录。');if(project?.id)await open(project.id);await list();});
$('new-project').onclick=$('start').onclick=()=>guard(async()=>{if(dirty)await save();newProject();});
$('add-episode').onclick=()=>guard(()=>{const e={id:uid(),title:`第${project.episodes.length+1}集`,synopsis:'',target_seconds:60,shots:[]};project.episodes.push(e);episodeId=e.id;shotId=null;mark();renderEpisodes();renderEpisode();});
$('add-shot').onclick=()=>guard(()=>{const e=episode(),s={id:uid(),title:`镜头${e.shots.length+1}`,description:'',narration:'',seconds:5,model:'local-wan',preset:'story'};e.shots.push(s);shotId=s.id;mark();renderEpisodes();renderEpisode();});
$('link-task').onclick=()=>guard(linkTask);
$('import-board').onclick=()=>guard(async()=>{const records=(await api('/api/storyboards')).items;await picker('复制已有草稿到本集',records,b=>[b.title,`${b.shots.length}镜头 · ${b.total_seconds}秒 · 原草稿保留`],async b=>{const e=episode();if(e.shots.length+b.shots.length>60)throw Error('每集最多60个镜头。');b.shots.forEach((s,i)=>e.shots.push({id:uid(),title:`导入镜头${i+1}`,description:s.description,narration:s.narration,seconds:s.seconds,model:'local-h3',preset:'fast'}));mark();await save();render();message('分镜草稿已复制并保存，原草稿保留；没有生成视频。');});});
for(const [id,key] of [['project-title','title'],['synopsis','synopsis'],['style','style'],['characters','characters']])$(id).oninput=()=>{project[key]=$(id).value;mark();};
for(const [id,key] of [['episode-title','title'],['episode-synopsis','synopsis'],['target-seconds','target_seconds']])$(id).oninput=()=>{episode()[key]=key==='target_seconds'?Number($(id).value):$(id).value;mark();stats();};
$('close-picker').onclick=()=>$('picker').close();$('close-playback').onclick=()=>$('playback').close();$('playback').onclose=()=>$('version-video').pause();
window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
await guard(async()=>{cloud=await api('/api/cloud/state');const catalog=(await api('/api/models')).models;native480Ready=catalog.some(m=>m.id==='local-wan'&&m.native480_verified);apiModels=catalog.filter(m=>m.execution==='customer_video_api');$('budget').textContent='本地模型 + 我的API';await list();const q=new URLSearchParams(location.search);episodeId=q.get('episode');if(q.get('project'))await open(q.get('project'));else{const p=(await api('/api/projects')).items[0];if(p)await open(p.id);}});
setInterval(async()=>{if(!project?.id||dirty||busy||['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName))return;try{const fresh=await api('/api/projects/'+project.id);if(fresh.revision!==project.revision){project=fresh;render();}else await renderExports();}catch{}},10000);
