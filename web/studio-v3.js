import {shotRecipes,composeShot} from './shot-design.mjs?v=design-20261021-v1';

import {activeStatuses, labels, localTaskActivity, makePayload, blockingReason, formFromTask, filterTasks, effectiveDuration, generationRecord, parallelTimingFacts, isVideoAPI} from './studio-state.mjs?v=byok-20261025-v1';

import './session.js?v=shared-shell-20261025-v1';



const $ = id => document.getElementById(id);

const accountSession=await window.frameseqSession;

const accountSuffix=':'+(accountSession.user?.id||'anonymous');

const DRAFT_KEY = 'frameseq-composer-v3'+accountSuffix, FAVORITES_KEY = 'frameseq-favorites-v3'+accountSuffix, PENDING_KEY='studio-pending-request'+accountSuffix;

let data = [], models = [], cloud = {}, health = {}, connected = false;

let selected = null, scope = 'works', origin = 'all', favoritesOnly = false;

let refreshing = false, submitting = false, pending = null, lastGrid = '', toastTimer;

let favorites = new Set();

let shotContext = null;



function readStorage(key, fallback) {try {return JSON.parse(localStorage.getItem(key)) ?? fallback;} catch {return fallback;}}

function writeStorage(key, value) {try {localStorage.setItem(key, JSON.stringify(value)); return true;} catch {return false;}}

if(accountSession.user?.id==='legacy-owner')for(const [legacy,scoped] of [['frameseq-composer-v3',DRAFT_KEY],['frameseq-favorites-v3',FAVORITES_KEY],['studio-pending-request',PENDING_KEY]]){

  const old=readStorage(legacy,null);if(readStorage(scoped,null)===null&&old!==null)writeStorage(scoped,old);

}

const savedFavorites = readStorage(FAVORITES_KEY, []);

if (Array.isArray(savedFavorites)) favorites = new Set(savedFavorites.filter(value => typeof value === 'string'));

pending = readStorage(PENDING_KEY, null);



function element(tag, content, className) {

  const node = document.createElement(tag);

  if (content !== undefined) node.textContent = content;

  if (className) node.className = className;

  return node;

}

function toast(message) {

  $('toast').textContent = message; $('toast').hidden = false;

  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 3200);

}

async function api(path, options = {}) {

  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 20000);

  try {

    const response = await fetch(path, {...options, signal: controller.signal});

    const body = await response.json();

    if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : '参数未通过检查，请核对输入。');

    return body;

  } catch (error) {

    if (error.name === 'AbortError') throw Error('网络等待超时；提交标识已保留，重试将查询同一提交，不会主动重复生成。');

    throw error;

  } finally {clearTimeout(timeout);}

}

function form() {return {model: $('model').value, prompt: $('prompt').value, preset: $('preset').value, seed: $('seed').value, negative: $('negative').value, duration: $('duration').value, duration_mode: $('duration-mode').value, resolution:$('api-resolution').value, ratio:$('api-ratio').value, allow_paid_call:$('api-consent').checked};}

function fillForm(value) {

  if(isVideoAPI(value.model)&&!Array.from($('model').options).some(o=>o.value===value.model)) {const option=element('option','读取已保存的API连接…');option.value=value.model;$('model').append(option);}

  $('model').value = value.model==='minimax-h3'?'local-h3':['local-h3', 'local-wan','local-cogvideo'].includes(value.model)||isVideoAPI(value.model) ? value.model : 'local-wan';

  $('prompt').value = typeof value.prompt === 'string' ? value.prompt.slice(0, 2000) : '';

  $('preset').value = ['preview','story','cinema'].includes(value.preset) ? value.preset : 'fast';

  $('seed').value = Number.isInteger(Number(value.seed)) && Number(value.seed) >= 0 && Number(value.seed) <= 2147483647 ? value.seed : 42;

  $('negative').value = typeof value.negative === 'string' ? value.negative.slice(0, 2000) : '';

  $('duration').value = value.duration ?? 4; $('duration-mode').value = value.duration_mode === 'suggested' ? 'suggested' : 'manual';

  $('api-resolution').dataset.preferred=value.resolution||'';$('api-ratio').value=value.ratio||'16:9';$('api-consent').checked=false;

}

function saveDraft() {

  $('draft-status').textContent = writeStorage(DRAFT_KEY, form()) ? '草稿已保存在本机' : '本机存储不可用，请先复制描述';

  $('char-count').textContent = $('prompt').value.length + ' / 2000';

}

function updateControls() {

  const isCloud = isVideoAPI($('model').value), h3=$('model').value==='local-h3', cog=$('model').value==='local-cogvideo', preview = $('preset').value === 'preview', story=$('preset').value==='story',cinema=$('preset').value==='cinema';

  const model = models.find(item => item.id === $('model').value);

  if(isCloud&&model){

    const resolutions=model.resolutions||[];

    if($('api-resolution').dataset.connection!==model.id){

      const previous=$('api-resolution').dataset.preferred||$('api-resolution').value;

      $('api-resolution').replaceChildren(...resolutions.map(value=>{const option=element('option',value);option.value=value;return option;}));

      if(resolutions.includes(previous))$('api-resolution').value=previous;

      $('api-resolution').dataset.connection=model.id;delete $('api-resolution').dataset.preferred;

    }

    $('duration').min=model.min_seconds;$('duration').max=model.max_seconds;

    $('duration-explanation').textContent=`当前连接支持${model.min_seconds}–${model.max_seconds}秒。建议时长仅为文字规则估算，保存密钥不会验证权限或余额。`;

  }

  let seconds;

  try {seconds = effectiveDuration(form());} catch {seconds = NaN;}

  const reason = blockingReason(model, cloud, health, submitting, connected, seconds,$('preset').value) || (isCloud&&!$('api-consent').checked?'请在参数中确认API费用':'');

  const cinemaOption=$('preset').querySelector('option[value="cinema"]');

  if(cinemaOption)cinemaOption.disabled=!models.find(m=>m.id==='local-wan')?.native480_verified;

  $('duration').disabled = !isCloud || $('duration-mode').value === 'suggested';

  $('duration-mode').disabled=!isCloud;$('api-resolution').disabled=!isCloud;$('api-ratio').disabled=!isCloud;

  $('seed').disabled=isCloud;

  if (isCloud && $('duration-mode').value==='suggested' && Number.isFinite(seconds)) $('duration').value = seconds;

  $('submit').disabled = Boolean(reason);

  $('submit-label').textContent = reason || (isCloud ? '使用我的API生成' : '生成本地视频');

  $('local-fields').hidden = isCloud; $('cloud-fields').hidden = !isCloud;

  $('preset').disabled=isCloud||cog||h3; $('negative').disabled=isCloud||h3;

  $('preset').querySelector('option[value="h3-native"]').hidden=!h3;

  if(h3){$('preset').value='h3-native';$('negative').value='';}else if($('preset').value==='h3-native')$('preset').value='fast';

  $('negative').closest('label').hidden=h3;

  $('model-hint').textContent = isCloud ? '密钥已加密保存，尚未验证供应商权限、余额或实测画质；费用以你的供应商账单为准。' : cog ? '实验预览：画质不稳定，建议主体居中、小幅移动；失败会保留原因。' : '本地低分辨率预览适合流程验证，复杂人物画质尚不稳定。任务后台排队，关闭页面不会中断。';

  $('duration-chip').textContent = h3?'◷ 约5.17秒 · 固定':isCloud ? `◷ ${Number.isFinite(seconds) ? seconds : '—'}秒 ▾` : cog?'◷ 约6秒':(story||cinema)?'◷ 约5秒':preview ? '◷ 约2秒' : '◷ 约1秒';

  $('resolution-chip').textContent = h3?'▣ 864×480':isCloud ? '▣ '+$('api-resolution').value : cog?'▣ 720×480':cinema?'▣ 832×480':(preview||story) ? '▣ 480×272' : '▣ 320×192';

  $('ratio-chip').textContent = h3?'▭ 9:5':isCloud ? '▭ '+$('api-ratio').value : cog?'▭ 3:2':cinema?'▭ 26:15':(preview||story) ? '▭ 30:17' : '▭ 5:3';

  if(!isCloud && !cog && cinema)$('model-hint').textContent='原生480P实验规格，计算量约为低清规格的3倍，可能需要较长时间；不会自动重试。';

  $('cost-hint').textContent = isCloud ? '自带密钥 · 使用你自己的供应商余额 · 仅点击生成才提交收费请求；不自动从本地切换到API' : '本地开放权重推理 · Wan约1/2/5秒；Cog约6秒固定规格 · 请使用英文描述';

  if(isCloud){$('advanced').hidden=false;$('settings').setAttribute('aria-expanded','true');}

  if(h3){$('model-hint').textContent='本地H3 Turbo 8步：两张卡共同生成同一条视频，约5.17秒、480P。暂不支持自定义时长、负面描述或参考图。';$('cost-hint').textContent='不调用云API · 本地双卡生成，耗时随模型和资源变化，排队另计 · 支持中文/英文描述';}

  $('char-count').textContent = $('prompt').value.length + ' / 2000';

}

function setView(view, scroll = true) {

  $('create-area').hidden = view !== 'create';

  scope = view === 'tasks' ? 'tasks' : 'works';

  $('filter-status').value = 'all';

  $('page-title').textContent = {create: '视频创作', works: '我的作品', tasks: '任务中心'}[view];

  for (const node of document.querySelectorAll('[data-view]')) {node.classList.toggle('active', node.dataset.view === view);if(node.dataset.view === view)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');}

  if(scroll){const url=new URL(location.href);url.searchParams.set('view',view);url.searchParams.delete('panel');history.replaceState(null,'',url.pathname+url.search+url.hash);}

  renderGrid(true);

  if (scroll) window.scrollTo({top: 0, behavior: 'smooth'});

}

function badge(status) {return element('span', labels[status] || status, 'status ' + status);}

function duration(task) {

  const m = task.metrics || {};

  const seconds = m.duration_seconds || m.validation?.duration_seconds || task.parameters.frames / (task.parameters.fps||16);

  return Number.isFinite(seconds) ? seconds.toFixed(2) + 's' : '';

}

function renderGrid(force = false) {

  const visible = filterTasks(data, {scope, origin, status: $('filter-status').value, search: $('search').value,

    favoritesOnly, favorites, sort: $('sort').value});

  const digest = JSON.stringify([visible.map(task => [task.id, task.updated_at, task.status, task.preview_url, favorites.has(task.origin + ':' + task.id)]), scope, favoritesOnly]);

  $('library-count').textContent = String(visible.length);

  $('library-title').textContent = scope === 'works' ? '最近作品' : '全部任务';

  $('scope-works').setAttribute('aria-pressed', String(scope === 'works'));

  $('scope-tasks').setAttribute('aria-pressed', String(scope === 'tasks'));

  if (!force && digest === lastGrid) return;

  lastGrid = digest; $('task-grid').replaceChildren();

  for (const task of visible) {

    const key = task.origin + ':' + task.id, card = element('article', undefined, 'task-card');

    const open = element('button', undefined, 'card-open'); open.type = 'button';

    open.setAttribute('aria-label', `查看${modelLabel(task)}任务 ${task.id.slice(0, 8)}：${task.parameters.prompt}`);

    const cover = element('div', undefined, 'thumbnail');

    if (task.preview_url) {

      const image = element('img'); image.src = task.preview_url; image.alt = '真实生成视频封面'; image.loading = 'lazy'; cover.append(image);

    } else cover.append(element('span', activeStatuses.has(task.status) ? '◌' : '◇', 'no-cover'));

    cover.append(element('span', task.origin==='api'?task.model+' · API':task.origin === 'cloud' ? 'H3 · 旧API作品' : task.model.includes('MiniMax-H3')?'H3 · 本地部署':task.model.includes('CogVideo')?'COGVIDEOX · 本地部署':'WAN · 本地部署', 'thumb-source'));

    cover.append(element('span', task.status === 'succeeded' ? duration(task) : labels[task.status], 'thumb-duration'));

    if (task.status === 'succeeded') cover.append(element('span', '▷', 'play-mark'));

    const foot = element('div', undefined, 'card-foot');

    foot.append(task.metrics?.visual_review?.approved===false?element('span','画面异常 · 已排除','status failed'):badge(task.status), element('time', new Date(task.created_at).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'})));

    open.append(cover, element('p', task.parameters.prompt, 'card-description'), foot);

    open.addEventListener('click', () => openTask(task));

    const favorite = element('button', favorites.has(key) ? '★' : '☆', 'favorite' + (favorites.has(key) ? ' saved' : ''));

    favorite.type = 'button'; favorite.setAttribute('aria-label', favorites.has(key) ? '取消本机收藏' : '收藏到本机');

    favorite.setAttribute('aria-pressed', String(favorites.has(key)));

    favorite.addEventListener('click', () => {

      if (favorites.has(key)) favorites.delete(key); else favorites.add(key);

      const saved = writeStorage(FAVORITES_KEY, [...favorites]); renderGrid(true);

      toast(saved ? '本机收藏已更新' : '本次收藏可用，但本机保存失败');

    });

    card.append(open, favorite); $('task-grid').append(card);

  }

  if (!visible.length) {

    const empty = element('div', undefined, 'empty-state');

    empty.append(element('span', '◇'), element('h3', favoritesOnly ? '还没有符合条件的收藏' : '这里暂时没有匹配的任务'),

      element('p', '试试切换模型或清除搜索；新的真实结果会自动出现在这里。'));

    $('task-grid').append(empty);

  }

}

function updateDetail(task) {

  const p = task.parameters, m = task.metrics || {}, video = $('studio-video');

  $('detail-title').textContent = modelLabel(task) + ' · ' + (task.status === 'succeeded' ? '生成作品' : '任务详情');

  $('selected-status').replaceChildren(badge(task.status)); $('selected-prompt').textContent = p.prompt;

  const running = activeStatuses.has(task.status);

  $('activity').hidden = !running; $('no-result').hidden = running || task.status === 'succeeded';

  $('activity-title').textContent = task.stage || '执行器正在处理任务';

  $('activity-description').textContent = task.origin === 'cloud'||task.origin==='api' ? '读取供应商任务状态；提交结果不明时保留记录，不自动重新生成。' : '等待资源、加载模型、采样或解码；离开页面不会终止服务器任务。';

  if(task.origin==='local'){const activity=localTaskActivity(task);$('activity-title').textContent=activity.title;$('activity-description').textContent=activity.description;}

  video.hidden = task.status !== 'succeeded'; $('download').hidden = video.hidden;

  if (task.video_url && video.dataset.task !== selected) {

    video.src = task.video_url; video.dataset.task = selected;

    if (task.preview_url) video.poster = task.preview_url; else video.removeAttribute('poster');

  } else if (!task.video_url && video.hasAttribute('src')) {video.pause(); video.removeAttribute('src'); video.removeAttribute('data-task'); video.load();}

  if (task.video_url) $('download').href = task.video_url + '?download=true';

  let editLink=document.getElementById('edit-clip-link');

  if(!editLink){editLink=element('a','选取片段编辑 ↗','secondary-button');editLink.id='edit-clip-link';document.querySelector('.detail-actions').append(editLink);}

  editLink.href='/edits?task='+task.id;editLink.hidden=task.status!=='succeeded'||m.visual_review?.approved===false;

  const qualityError=m.visual_review?.approved===false?m.visual_review.reason:'';

  $('selected-error').hidden = !(task.error||qualityError); $('selected-error').textContent = task.error || qualityError;

  $('resume').hidden = true;

  const facts = [task.provider, modelLabel(task)];

  if (task.origin === 'cloud'||task.origin==='api') {

    facts.push(`请求${p.duration}秒 · ${p.resolution}`, task.origin==='api'?'你自己的供应商API余额':`预算预留¥${(task.reserved_fen || 0) / 100}`, '实际账单未核对');

    if (m.frames) facts.push(`${m.width}×${m.height} · ${m.frames}帧`, `实测${m.duration_seconds}秒`);

    if (m.elapsed_seconds) facts.push(`提交至验收${m.elapsed_seconds.toFixed(1)}秒`);

  } else {

    facts.push(`${p.width}×${p.height} · ${p.frames}帧`, `种子${p.seed}`);

    if (m.generation_seconds) facts.push(`生成${m.generation_seconds.toFixed(1)}秒`);

    facts.push(...parallelTimingFacts(m));

  }

  $('selected-facts').replaceChildren(...facts.map(value => element('span', value)));

  $('review-note').hidden=!m.visual_review;

  $('review-note').textContent=m.imported_existing_result?'导入既有本地实测视频，未因本页提交重新生成。':m.visual_review?.reason||'';

  if(m.imported_existing_result)$('review-note').hidden=false;

  $('selected-id').textContent = '任务 ' + task.id + (task.provider_task_id ? ' · 官方ID ' + task.provider_task_id : '');

  $('selected-hash').textContent = 'SHA256 ' + (m.sha256 || m.validation?.sha256 || '本任务无已验收文件');

}

function modelLabel(task){

  if(task.origin==='api')return task.model+' · API';

  if(task.origin==='cloud')return 'MiniMax H3';

  if((task.parameters.model_id||task.model||'').includes('MiniMax-H3'))return 'MiniMax H3 · 本地实验';

  return (task.parameters.model_id||task.model||'').includes('CogVideo')?'CogVideoX · 2B':'Wan2.1 · 1.3B';

}

function openTask(task) {

  selected = task.origin + ':' + task.id; updateDetail(task);

  if (!$('detail-dialog').open) $('detail-dialog').showModal();

}

function openInfo(budget = false) {

  $('info-title').textContent = budget ? '本轮预算与用量' : '模型与能力';

  const content = $('info-content'); content.replaceChildren();

  if (budget) {

    content.append(element('h3','本地模型与自己的视频API'),

      element('p','可选择本地推理或自己配置的视频API。H3本地固定864×480、124帧/24fps，约5.17秒；两卡分担同一个视频的序列计算。其他模型使用单卡。'),

      element('p','队列一次执行一个任务。H3需要两张卡同时空闲，不会抢占他人显存；双卡不代表自动快两倍。旧API作品保留原始来源及历史费用记录。'));

  } else {

    for (const model of models) content.append(element('h3', model.name), element('span', model.preview ? '实验预览 · 通用画质有限' : model.verified ? '真实结果已验证' : '待验证', 'info-flag'),

      element('p', model.description + ' ' + model.specification));

    content.append(element('p', 'Wan、CogVideoX和H3本地入口保留；视频API页可配置自己的MiniMax、Seedance或Wan连接，保存不扣费，未验证真实调用权限。各模型当前是否可提交以就绪状态为准。短剧项目支持剧集分镜、版本选片与整集导出；可添加中文字幕及程序配乐。参考图、AI配音、批量自动生成尚未接入。'));

  }

  $('info-dialog').showModal();

}

async function refresh() {

  if (refreshing) return; refreshing = true;

  try {

    const [library, state, runtime] = await Promise.all([api('/api/library?limit=100'), api('/api/models'), api('/api/health')]);

    data = library.items; models = state.models; cloud = state.cloud; health = runtime; connected = true;

    $('connection').textContent = '服务已连接'; $('status-dot').className = 'connected';

    $('budget').textContent = '本地模型 + 我的API';

    const chosen=$('model').value;

    for(const option of [...$('model').options])if(isVideoAPI(option.value))option.remove();

    for(const model of models.filter(m=>isVideoAPI(m.id))){const option=element('option',model.name);option.value=model.id;$('model').append(option);}

    if(isVideoAPI(chosen)&&!models.some(m=>m.id===chosen)){const option=element('option','原API连接已删除，请重新选择');option.value=chosen;$('model').append(option);}

    if([...$('model').options].some(o=>o.value===chosen))$('model').value=chosen;

    $('nav-works').textContent = String(data.filter(task => task.status === 'succeeded'&&task.metrics?.visual_review?.approved!==false).length);

    $('nav-active').textContent = String(data.filter(task => activeStatuses.has(task.status)).length);

    if (selected && $('detail-dialog').open) {const task = data.find(item => item.origin + ':' + item.id === selected); if (task) updateDetail(task);}

    renderGrid(); $('library-message').textContent = '';

    $('sync').textContent = '最近同步 ' + new Date().toLocaleTimeString('zh-CN') + ` · 共${library.total}条任务`;

  } catch (error) {

    connected = false; $('connection').textContent = '连接暂不可用'; $('status-dot').className = '';

    $('library-message').textContent = error.message + '；保留已有画面，稍后自动读取。';

  } finally {refreshing = false; updateControls();}

}



$('studio-form').addEventListener('submit', async event => {

  event.preventDefault(); if (submitting || $('submit').disabled) return;

  let body;

  try {body = makePayload(form());} catch (error) {$('form-message').textContent = error.message; return;}

  const isCloud = isVideoAPI($('model').value), fingerprint = JSON.stringify({cloud: isCloud, body, context: shotContext});

  if (!pending || pending.fingerprint !== fingerprint || typeof pending.key !== 'string') {

    pending = {fingerprint, key: crypto.randomUUID()}; writeStorage(PENDING_KEY, pending);

  }

  submitting = true; updateControls(); $('form-message').textContent = '正在保存任务…';

  try {

    const task = await api(isCloud?'/api/video-api/tasks':'/api/tasks', {method: 'POST', headers: {'Content-Type': 'application/json', 'Idempotency-Key': pending.key}, body: JSON.stringify(body)});

    if (shotContext) {

      try {

        const project = await api('/api/projects/' + shotContext.project);

        await api(`/api/projects/${shotContext.project}/versions`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({episode_id: shotContext.episode, shot_id: shotContext.shot, origin: isCloud ? 'api' : 'local', task_id: task.id, revision: project.revision, note: '从分镜带入工作台后提交；以实际提交参数为准'})});

      } catch (error) {

        // Keep this submission key: a retry resolves the same job before linking again.

        $('form-message').textContent = `任务${task.id}已保存，但关联分镜失败：${error.message}。可在项目中从任务历史关联；请勿重复生成。`;

        await refresh(); return;

      }

    }

    pending = null; writeStorage(PENDING_KEY, null);

    $('form-message').textContent = '任务已保存，后台继续执行；刷新页面不会重新提交。';

    task.origin = isCloud ? 'api' : 'local'; task.model = task.parameters.model_id||'Wan2.1-T2V-1.3B';

    task.provider = isCloud ? task.provider : '本地部署'; task.metrics = task.result || task.metrics || {};

    $('api-consent').checked=false;

    openTask(task); await refresh();

  } catch (error) {$('form-message').textContent = error.message;}

  finally {submitting = false; updateControls();}

});

let activeRecipe=null;

const briefKeys=['subject','action','scene','camera','style'];

const BRIEF_KEY='frameseq-shot-brief-v1'+accountSuffix;

function previewBrief() {

  const brief=Object.fromEntries(briefKeys.map(key=>[key,$('brief-'+key).value]));

  writeStorage(BRIEF_KEY,{...brief,language:$('brief-language').value});

  try {$('brief-preview').textContent=composeShot(brief,$('brief-language').value); $('use-brief').disabled=false;}

  catch(error) {$('brief-preview').textContent=error.message; $('use-brief').disabled=true;}

}

function revealBrief() {$('brief-editor').hidden=false; $('open-brief').setAttribute('aria-expanded','true');}

$('open-brief').addEventListener('click',()=>{

  if(!$('brief-editor').hidden){$('brief-editor').hidden=true;$('open-brief').setAttribute('aria-expanded','false');return;}

  revealBrief();previewBrief();$('brief-subject').focus();

});

for(const button of document.querySelectorAll('[data-recipe]')) button.addEventListener('click',()=>{

  activeRecipe=button.dataset.recipe;const language=$('model').value==='local-h3'?'zh':'en';

  $('brief-language').value=language;

  for(const key of briefKeys)$('brief-'+key).value=shotRecipes[activeRecipe][language][key];

  revealBrief();previewBrief();$('brief-subject').focus();

});

$('brief-language').addEventListener('change',()=>{

  // Changing language does not discard user edits; only template buttons load translated text.

  previewBrief();

});

for(const key of briefKeys)$('brief-'+key).addEventListener('input',previewBrief);

const savedBrief=readStorage(BRIEF_KEY,null);

if(savedBrief && briefKeys.some(key=>savedBrief[key])) {

  for(const key of briefKeys)$('brief-'+key).value=String(savedBrief[key]||'');

  $('brief-language').value=savedBrief.language==='en'?'en':'zh';revealBrief();previewBrief();

}

$('use-brief').addEventListener('click',()=>{

  const brief=Object.fromEntries(briefKeys.map(key=>[key,$('brief-'+key).value]));

  let prompt;try {prompt=composeShot(brief,$('brief-language').value);}catch(error){toast(error.message);return;}

  if($('prompt').value.trim() && $('prompt').value!==prompt && !window.confirm('使用这份镜头描述替换当前创作描述？原描述请先自行保留。'))return;

  $('prompt').value=prompt;saveDraft();updateControls();$('prompt').focus();

  $('brief-editor').hidden=true;$('open-brief').setAttribute('aria-expanded','false');

  $('form-message').textContent='镜头描述已带入，可继续修改；尚未提交生成。';

});



const examples = {

  boat: ['一只蓝色纸船在清澈水面上缓缓漂动，水波自然扩散。固定镜头，柔和日光，写实风格，画面中没有文字。', 'A blue paper boat floats gently across clear shallow water. Small ripples move around it, soft daylight, steady camera, close-up.'],

  sea: ['细小海浪缓缓涌上沙滩，泡沫随水流散开，远处海面闪烁日光。低机位，固定镜头，自然写实，无文字。', 'Small ocean waves roll gently onto a sandy beach, sea foam spreads over the sand, sunlight glitters on the distant water. Low angle, fixed camera, realistic.'],

  product: ['一只透明玻璃香水瓶放在深色石台上，背景柔和虚化，侧光在瓶身折射，镜头缓慢推进，无文字、无商标。', 'A clear glass perfume bottle rests on a dark stone surface, soft blurred background, side lighting refracts through the glass, slow camera push-in, no text, no logo.'],

  macro: ['一颗露珠停在嫩绿叶尖，清晨微风让叶片轻轻摆动，细腻微距镜头，浅景深，柔和自然光。', 'A clear dewdrop rests on the tip of a fresh green leaf. A light morning breeze gently moves the leaf, detailed macro shot, shallow depth of field, soft natural light.']

};

for (const node of document.querySelectorAll('[data-example]')) node.addEventListener('click', () => {

  $('prompt').value = examples[node.dataset.example][$('model').value === 'local-h3' ? 0 : 1];

  saveDraft(); $('prompt').focus(); $('form-message').textContent = '已填入人工编写的灵感示例，可修改；尚未提交生成。';

});

for (const id of ['prompt', 'model', 'preset', 'seed', 'negative', 'duration', 'duration-mode','api-resolution','api-ratio','api-consent']) $(id).addEventListener(['model', 'preset', 'duration-mode','api-resolution','api-ratio','api-consent'].includes(id) ? 'change' : 'input', () => {if(id==='model')$('api-consent').checked=false;saveDraft(); updateControls();});

$('duration-chip').setAttribute('role', 'button'); $('duration-chip').tabIndex = 0;

function openDuration() {$('advanced').hidden = false; $('settings').setAttribute('aria-expanded', 'true'); if($('model').value==='local-h3')$('model-hint').textContent='本地H3当前固定124帧/24fps，约5.17秒；更长内容请拆成多个分镜。';else if(isVideoAPI($('model').value))$('model-hint').textContent='在下方设置时长、分辨率与画幅，确认自己的供应商费用后才提交。';else $('model-hint').textContent='本地模型使用固定预设；计划时长用于后续裁剪，不能自动扩展模型原生输出。';}

$('duration-chip').addEventListener('click', openDuration);

$('duration-chip').addEventListener('keydown', event => {if (['Enter', ' '].includes(event.key)) {event.preventDefault(); openDuration();}});

$('settings').addEventListener('click', () => {$('advanced').hidden = !$('advanced').hidden; $('settings').setAttribute('aria-expanded', String(!$('advanced').hidden));});

for (const node of document.querySelectorAll('[data-view]')) node.addEventListener('click', event => {event.preventDefault();setView(node.dataset.view);});

for (const node of document.querySelectorAll('[data-origin]')) node.addEventListener('click', () => {

  origin = node.dataset.origin; for (const other of document.querySelectorAll('[data-origin]')) other.setAttribute('aria-pressed', String(other === node)); renderGrid(true);

});

$('favorites-filter').addEventListener('click', () => {favoritesOnly = !favoritesOnly; $('favorites-filter').setAttribute('aria-pressed', String(favoritesOnly)); renderGrid(true);});

$('scope-works').addEventListener('click', () => {scope = 'works'; $('filter-status').value = 'all'; renderGrid(true);});

$('scope-tasks').addEventListener('click', () => {scope = 'tasks'; renderGrid(true);});

for (const id of ['search', 'filter-status', 'sort']) $(id).addEventListener(id === 'search' ? 'input' : 'change', () => renderGrid(true));

$('refresh').addEventListener('click', refresh);

$('close-detail').addEventListener('click', () => $('detail-dialog').close());

$('detail-dialog').addEventListener('close', () => $('studio-video').pause());

$('studio-video').addEventListener('error', () => {$('selected-error').hidden = false; $('selected-error').textContent = '浏览器未能读取视频，可刷新状态或尝试下载。';});

$('reuse').addEventListener('click', () => {

  const task = data.find(item => item.origin + ':' + item.id === selected); if (!task) return;

  fillForm(formFromTask(task)); saveDraft(); updateControls(); $('detail-dialog').close(); setView('create'); $('prompt').focus();

  $('form-message').textContent = '已复用描述与参数，可修改后再提交；当前没有创建新任务。';

});

$('copy').addEventListener('click', async () => {

  try {await navigator.clipboard.writeText($('selected-prompt').textContent); toast('画面描述已复制');}

  catch {toast('当前浏览器无法复制，可使用“复用描述与参数”填入表单。');}

});

$('export-record').addEventListener('click',()=>{

  const task=data.find(item=>item.origin+':'+item.id===selected);if(!task)return;

  const url=URL.createObjectURL(new Blob([JSON.stringify(generationRecord(task),null,2)],{type:'application/json'}));

  const link=document.createElement('a');link.href=url;link.download=`frameseq-${task.id}-record.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);

  toast('生成记录已导出；未重新生成视频');

});

$('model-info').addEventListener('click', event => {event.preventDefault();openInfo();}); $('capability-link').addEventListener('click', () => openInfo());

$('budget-info').addEventListener('click', () => openInfo(true)); $('close-info').addEventListener('click', () => $('info-dialog').close());

$('prompt').addEventListener('keydown', event => {if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing) {event.preventDefault(); $('studio-form').requestSubmit();}});

const savedDraft = readStorage(DRAFT_KEY, null);

if($('prompt').value.trim()) {saveDraft();}

else if (savedDraft && typeof savedDraft === 'object') {fillForm(savedDraft); $('draft-status').textContent = '已恢复本机草稿';}

async function loadShotContext() {

  const query = new URLSearchParams(location.search);

  if (!query.has('project')) return;

  $('submit').disabled = true;

  try {

    const project = await api('/api/projects/' + query.get('project'));

    const episode = project.episodes.find(e => e.id === query.get('episode'));

    const shot = episode?.shots.find(s => s.id === query.get('shot'));

    if (!shot) throw Error('分镜不存在，请重新从短剧项目打开。');

    shotContext = {project: project.id, episode: episode.id, shot: shot.id};

    fillForm({model: shot.model, prompt: shot.description, duration: shot.seconds, preset: shot.preset, seed: 42});

    const node = $('shot-context'); node.hidden = false; node.replaceChildren(document.createTextNode(`${project.title} / ${episode.title} / ${shot.title} · 本次生成将关联为新版本。 `));

    const back = element('a', '返回项目 ↗'); back.href = '/projects?project=' + project.id + '&episode=' + episode.id; node.append(back);

    saveDraft();

  } catch (error) {$('form-message').textContent = error.message;}

}

await refresh();await loadShotContext();

const requestedConnection=new URLSearchParams(location.search).get('connection');

if(/^[0-9a-f]{32}$/.test(requestedConnection||'')&&models.some(m=>m.id==='video-api:'+requestedConnection)){

  $('model').value='video-api:'+requestedConnection;$('api-consent').checked=false;saveDraft();

}

const requestedView=new URLSearchParams(location.search).get('view');

setView(['works','tasks'].includes(requestedView)?requestedView:'create',false);

if(new URLSearchParams(location.search).get('panel')==='models')openInfo();

updateControls();setInterval(refresh, 5000);



