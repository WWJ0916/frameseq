// Pure UI rules. No network, DOM, credentials or generation side effects.
export const activeStatuses = new Set(['queued', 'running', 'submitting', 'waiting', 'downloading', 'unknown']);
export const labels = {queued: '排队中', submitting:'提交中', downloading:'保存视频中', running: '生成中', paused:'已暂停', waiting: '等待查询', unknown: '待核对', succeeded: '已完成', failed: '失败', cancelled: '已取消'};
export const isVideoAPI = id => /^video-api:[0-9a-f]{32}$/.test(id || '');

export function parallelTimingFacts(metrics = {}) {
  const ranks = metrics.rank_metrics;
  if (!Array.isArray(ranks) || ranks.length < 2 ||
      new Set(ranks.map(row => row?.rank)).size !== ranks.length ||
      !ranks.every(row => Number.isInteger(row?.rank) && row.rank >= 0 &&
        ['forward_seconds', 'cooldown_total_seconds'].every(key =>
          Number.isFinite(row[key]) && row[key] >= 0))) return [];
  // Ranks run at the same time: summing their durations would double-count.
  const forward = Math.max(...ranks.map(row => row.forward_seconds));
  const cooling = Math.max(...ranks.map(row => row.cooldown_total_seconds));
  if (Number.isFinite(metrics.generation_seconds) &&
      forward + cooling > metrics.generation_seconds + 1) return [];
  const duration = seconds => seconds >= 60 ? `${(seconds / 60).toFixed(1)}分钟` : `${seconds.toFixed(1)}秒`;
  return [`采样计算与通信约${duration(forward)}`, `降温等待约${duration(cooling)}`, '两卡耗时取较慢卡，不相加'];
}

export function modelDisplayName(task) {
  if(task.origin==='api')return (task.parameters?.model_id||task.model||'视频模型')+' · 自带密钥API';
  if(task.origin==='cloud')return 'H3 · 旧API作品';
  const id=task.parameters?.model_id||task.model||'';
  if(id.includes('MiniMax-H3')||id==='local-h3')return 'H3 · 本地部署';
  if(id.includes('CogVideo')||id==='local-cogvideo')return 'CogVideoX · 本地部署';
  if(!id||id.includes('Wan')||id==='local-wan')return 'Wan · 本地部署';
  return '未知本地模型';
}

export function localTaskActivity(task) {
  const runtime=task.runtime||{},event=runtime.last_event;
  const steps=Number.isInteger(event?.completed_steps)&&event.total_steps>0
    ? `已完成${event.completed_steps}／${event.total_steps}步采样。` : '';
  if(task.status==='paused')return {title:'任务已暂停',description:'输入和日志已保留；尚无成片，恢复可能需要重新采样。'};
  if(task.status==='queued'&&task.progress>=99&&modelDisplayName(task)==='H3 · 本地部署')return {title:'采样完成，等待解码',description:'已保留采样结果，等待空闲显卡继续解码；不会重新采样。'};
  if(task.status==='queued')return {title:'任务已排队',description:'等待空闲的本地生成资源；离开页面不会停止服务器任务。'};
  if(runtime.stage==='decode')return {title:'正在解码音视频',description:'采样已完成，正在生成并检查MP4，完成后可播放下载。'};
  if(runtime.stage==='encode')return {title:'正在理解画面描述',description:'本地模型正在编码描述，之后开始视频采样。'};
  if(runtime.cooling)return {title:'显卡降温等待',description:steps+'正在暂停计算以降温，随后继续同一任务；采样后还需音视频解码。'};
  return {title:task.stage||'本地模型正在生成',description:steps+'正在加载、采样或检查结果；离开页面不会停止服务器任务。'};
}

export function generationRecord(task) {
  const select=(object,keys)=>Object.fromEntries(keys.filter(k=>object?.[k]!==undefined).map(k=>[k,object[k]]));
  const m=task.metrics||{},v=m.validation||m;
  return {schema_version:1,task_id:task.id,model:task.model,
    execution:task.origin==='api'?'customer_video_api':task.origin==='cloud'?'official_api':'self_hosted',status:task.status,
    provenance:select(m,['imported_existing_result','import_source_label','source_sampling_run_id','source_decode_run_id','original_generation_started_at','resumed_from_sample','stage_gpus','physical_gpus','parallelism','same_video_sequence_parallel']),
    parameters:select(task.parameters,['prompt','negative_prompt','model_id','inference_profile','scheduler','sampler','precision','vae_precision','audio_vae_precision','preset','seed','width','height','frames','fps','steps','guidance','max_sequence_length','duration','resolution','ratio']),
    times:select(task,['created_at','started_at','finished_at']),
    measurements:select(m,['precision','vae_precision','generation_seconds','model_load_seconds','prompt_encoding_seconds','sampling_and_decode_seconds','peak_gpu_allocated_bytes','peak_gpu_reserved_bytes','elapsed_seconds']),
    output:select(v,['sha256','width','height','decoded_frames','frames','fps','duration_seconds','codec']),
    visual_review:m.visual_review?select(m.visual_review,['approved','reason']):{approved:null,reason:'未记录逐镜人工审片；可解码不代表内容合格。'},
    limitation:'参数和指标是本次任务记录；不同硬件、依赖和种子实现可能影响复现，未包含模型权重、认证信息或供应商下载链接。'};
}

export function suggestDuration(prompt = '') {
  // Transparent pacing hint, not an LLM or provider inference about story length.
  const text = String(prompt).trim();
  const beats = text.split(/[。！？.!?；;\n]+/).filter(part => part.trim()).length;
  return Math.min(15, Math.max(4, beats * 2, Math.ceil(text.length / 25)));
}
export function effectiveDuration(form) {
  if(form.model==='local-h3')return 124/24;
  const duration = form.duration_mode === 'suggested' ? suggestDuration(form.prompt) : Number(form.duration ?? 4);
  if (!Number.isInteger(duration) || duration < (isVideoAPI(form.model)?2:4) || duration > 15) throw Error('时长应为支持范围内的整数，最多15秒。');
  return duration;
}

export function makePayload(form) {
  const prompt = String(form.prompt || '').trim();
  if (!prompt || prompt.length > 2000) throw Error('请填写1至2000字的画面描述。');
  if(isVideoAPI(form.model)) {
    if(form.allow_paid_call!==true)throw Error('请确认使用自己的供应商API余额。');
    return {connection_id:form.model.slice(10),prompt,duration:effectiveDuration(form),resolution:form.resolution,ratio:form.ratio,allow_paid_call:true};
  }
  if(form.model==='minimax-h3')throw Error('云API已停用，请选择本地H3。');
  if (!['local-wan','local-cogvideo','local-h3'].includes(form.model)) throw Error('请选择已接入的模型。');
  const seed = Number(form.seed);
  if (!Number.isInteger(seed) || seed < 0 || seed > 2147483647) throw Error('随机种子应为0至2147483647之间的整数。');
  if (form.model==='local-wan'&&!['fast', 'preview','story','cinema'].includes(form.preset)) throw Error('请选择已验证的本地规格。');
  if(form.model==='local-h3')return {prompt,model:'local-h3',seed};
  const body = form.model==='local-cogvideo'?{prompt,model:'local-cogvideo',seed}:{prompt,model:'local-wan',preset:form.preset,seed};
  if (form.negative?.trim()) body.negative_prompt = form.negative.trim();
  return body;
}

export function blockingReason(model, cloud, health, submitting, connected, duration = 4, preset) {
  if (submitting) return '正在保存任务…';
  if (!connected) return '等待服务连接';
  if (!model) return '模型信息尚未载入';
  if(isVideoAPI(model.id)) {
    if(!model.configured)return model.unavailable_reason||'视频API服务尚未就绪';
    if(!Number.isInteger(duration)||duration<model.min_seconds||duration>model.max_seconds)return `请选择${model.min_seconds}至${model.max_seconds}秒`;
    return '';
  }
  if (health.worker?.state==='maintenance') return '生成服务维护中，请稍后重试';
  if (model.enabled===false) return '管理员已暂停此模型的新任务';
  if (model.configured===false&&model.id!=='minimax-h3') return model.unavailable_reason || '模型或执行器尚未就绪';
  if(model.id==='local-wan' && preset==='cinema' && !model.native480_verified)return '原生480P仍在验收，请先选择其他规格';
  if(model.id==='minimax-h3')return '云API已停用，请选择本地H3';
  if(model.id==='local-h3' && !model.verified)return '本地H3尚未完成验收';
  if (!['idle', 'running','waiting_for_gpu'].includes(health.worker?.state)) return '本地执行器暂不可用';
  return '';
}

export function formFromTask(task) {
  const p = task.parameters;
  if(task.origin==='api')return {model:'video-api:'+task.connection_id,prompt:p.prompt,duration:p.duration,duration_mode:'manual',resolution:p.resolution,ratio:p.ratio,allow_paid_call:false};
  return {model: task.origin === 'cloud' || (p.model_id||'').includes('MiniMax-H3') ? 'local-h3' : p.model_id==='zai-org/CogVideoX-2b'?'local-cogvideo':'local-wan', prompt: p.prompt,
    preset: p.frames===81 && p.width===832 && p.height===480 ? 'cinema' : p.frames === 81 ? 'story' : p.frames === 33 ? 'preview' : 'fast', seed: Number.isInteger(p.seed) ? p.seed : 42,
    negative: p.negative_prompt || '', ...(task.origin === 'cloud' ? {negative:''} : {})};
}

export function filterTasks(tasks, {scope, origin, status, search, favoritesOnly, favorites, sort}) {
  const query = search.trim().toLowerCase();
  return tasks.filter(task => (scope !== 'works' || (task.status === 'succeeded'&&task.metrics?.visual_review?.approved!==false)) &&
    (origin === 'all' || (origin === 'h3' && task.origin === 'local' && (task.model || task.parameters.model_id || '').includes('MiniMax-H3')) || task.origin === origin || (origin === 'wan' && task.origin === 'local' && !(task.model || task.parameters.model_id || '').includes('CogVideo') && !(task.model || task.parameters.model_id || '').includes('MiniMax-H3')) || (origin === 'cogvideo' && task.origin === 'local' && (task.model || task.parameters.model_id || '').includes('CogVideo'))) &&
    (status === 'all' || (status === 'active' ? activeStatuses.has(task.status) : task.status === status)) &&
    (!favoritesOnly || favorites.has(task.origin + ':' + task.id)) &&
    (!query || (task.parameters.prompt + ' ' + (task.model||'') + ' ' + task.id + ' ' + (task.provider_task_id || '')).toLowerCase().includes(query)))
    .sort((a, b) => (sort === 'oldest' ? 1 : -1) * (a.created_at + a.id).localeCompare(b.created_at + b.id));
}
