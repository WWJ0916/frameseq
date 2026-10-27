import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {makePayload, blockingReason, formFromTask, filterTasks, suggestDuration, effectiveDuration, generationRecord} from '../web/studio-state.mjs';
import {modelDisplayName} from '../web/studio-state.mjs';
import {localTaskActivity} from '../web/studio-state.mjs';
import {parallelTimingFacts} from '../web/studio-state.mjs';

test('dual-card timing reports wall time rather than adding overlapping ranks',()=>{
  const facts=parallelTimingFacts({generation_seconds:2928.304,rank_metrics:[
    {rank:0,forward_seconds:238.7465,cooldown_total_seconds:2576.9134},
    {rank:1,forward_seconds:238.7577,cooldown_total_seconds:2576.9137}
  ]});
  assert.deepEqual(facts,['采样计算与通信约4.0分钟','降温等待约42.9分钟','两卡耗时取较慢卡，不相加']);
});

test('missing, duplicate or inconsistent rank metrics do not invent timing facts',()=>{
  const row={rank:0,forward_seconds:10,cooldown_total_seconds:20};
  for(const metrics of [{},{rank_metrics:[row]},{rank_metrics:[row,row]},
    {rank_metrics:[row,{...row,rank:1,forward_seconds:NaN}]},
    {rank_metrics:[row,{...row,rank:1,cooldown_total_seconds:-1}]},
    {generation_seconds:5,rank_metrics:[row,{...row,rank:1}]}])assert.deepEqual(parallelTimingFacts(metrics),[]);
});

test('step completion and cooling never imply that the final video is already ready',()=>{
  const waiting=localTaskActivity({status:'running',runtime:{stage:'sample',cooling:true,last_event:{completed_steps:8,total_steps:20}}});
  assert.equal(waiting.title,'显卡降温等待');assert.ok(waiting.description.includes('8／20'));
  const decode=localTaskActivity({status:'running',runtime:{stage:'decode',last_event:{completed_steps:20,total_steps:20}}});
  assert.equal(decode.title,'正在解码音视频');assert.ok(decode.description.includes('正在生成并检查MP4'));
  assert.equal(localTaskActivity({status:'queued'}).title,'任务已排队');
  const recovered=localTaskActivity({status:'queued',progress:99,model:'local-h3'});
  assert.equal(recovered.title,'采样完成，等待解码');assert.ok(recovered.description.includes('不会重新采样'));
});

test('model labels distinguish local H3 and historical API across project and editing views',()=>{
  assert.equal(modelDisplayName({origin:'local',parameters:{model_id:'MiniMax-AI/MiniMax-H3'}}),'H3 · 本地部署');
  assert.equal(modelDisplayName({origin:'cloud',model:'MiniMax-H3'}),'H3 · 旧API作品');
  assert.equal(modelDisplayName({origin:'local',model:'zai-org/CogVideoX-2b'}),'CogVideoX · 本地部署');
  assert.equal(modelDisplayName({origin:'local',model:'Wan2.1'}),'Wan · 本地部署');
  assert.equal(modelDisplayName({origin:'local',model:'unknown'}),'未知本地模型');
});

test('local H3 uses fixed duration, explicit model and seed; cloud submission rejected',()=>{
  assert.deepEqual(makePayload({model:'local-h3',prompt:' sea ',seed:'123',duration:15,preset:'preview',negative:'blur'}),{prompt:'sea',model:'local-h3',seed:123});
  assert.equal(effectiveDuration({model:'local-h3',duration:15}),124/24);
  assert.throws(()=>makePayload({model:'minimax-h3',prompt:'sea'}));
  assert.throws(()=>makePayload({model:'local-h3',prompt:'sea',seed:-1}));
});
test('old cloud result reuse migrates to local H3 and does not preserve API duration',()=>{
  const draft=formFromTask({origin:'cloud',parameters:{prompt:'boat',duration:9}});
  assert.equal(draft.model,'local-h3');assert.equal(draft.duration,undefined);
  assert.deepEqual(makePayload(draft),{prompt:'boat',model:'local-h3',seed:42});
});
test('local payload preserves supported preset, seed and optional negative prompt', () => {
  assert.deepEqual(makePayload({model:'local-wan',prompt:'sea',preset:'preview',seed:'42',negative:'blur'}),
    {prompt:'sea',model:'local-wan',preset:'preview',seed:42,negative_prompt:'blur'});
  for (const seed of [-1,1.5,2147483648,'NaN']) assert.throws(() => makePayload({model:'local-wan',prompt:'x',preset:'fast',seed}));
});

test('saving a fresh draft for any local model does not require a global h3 flag',()=>{
  const source=readFileSync(new URL('../web/studio-v3.js',import.meta.url),'utf8');
  const draftFunction=source.slice(source.indexOf('function saveDraft()'),source.indexOf('function updateControls()'));
  for(const model of ['local-wan','local-cogvideo','local-h3']){
    const nodes={'draft-status':{},'char-count':{},prompt:{value:'测试画面'}};
    let saved;
    runInNewContext(draftFunction+'\nsaveDraft();',{$:id=>nodes[id],DRAFT_KEY:'draft',
      form:()=>({model,prompt:nodes.prompt.value}),writeStorage:(key,value)=>{saved=value;return true;}});
    assert.equal(saved.model,model);assert.equal(nodes['char-count'].textContent,'4 / 2000');
  }
});
test('third model has fixed local contract and cannot silently fall back to Wan',()=>{
  assert.deepEqual(makePayload({model:'local-cogvideo',prompt:'car',seed:'73',preset:'preview'}),{prompt:'car',model:'local-cogvideo',seed:73});
  assert.ok(blockingReason({id:'local-cogvideo',configured:false},{},{worker:{state:'idle'}},false,true));
  assert.equal(formFromTask({origin:'local',parameters:{model_id:'zai-org/CogVideoX-2b',prompt:'car',frames:49,seed:73}}).model,'local-cogvideo');
  assert.equal(makePayload({model:'local-wan',prompt:'boat',seed:42,preset:'story'}).preset,'story');
});
const model = {id:'minimax-h3',verified:true};
const cloud = {configured:true,pricing_current:true,remaining_unreserved_cny:8,latest:{status:'succeeded'}};
test('local H3 readiness uses the local worker, never cloud balance or keys',()=>{
  const local={id:'local-h3',verified:true,configured:true},health={worker:{state:'waiting_for_gpu'}};
  assert.equal(blockingReason(local,{configured:false,remaining_unreserved_cny:0},health,false,true),'');
  assert.ok(blockingReason({...local,configured:false},{},health,false,true));
  assert.ok(blockingReason({...local,verified:false},{},health,false,true));
  assert.ok(blockingReason({...local,enabled:false},{},health,false,true));
  assert.ok(blockingReason(model,cloud,health,false,true));
  assert.ok(blockingReason(local,{}, {worker:{state:'unavailable'}},false,true));
});
test('reuse only returns a form draft, preserving origin and parameters', () => {
  const local = {origin:'local',parameters:{prompt:'sea',frames:33,seed:7,negative_prompt:'text'}};
  assert.deepEqual(formFromTask(local),{model:'local-wan',prompt:'sea',preset:'preview',seed:7,negative:'text'});
  assert.equal(formFromTask({origin:'cloud',parameters:{prompt:'boat'}}).model,'local-h3');
});
test('gallery keeps failures in task mode and filters by source, favorite, search and time', () => {
  const tasks = [{id:'1',origin:'cloud',status:'succeeded',created_at:'2026-10-20',provider_task_id:'official',parameters:{prompt:'Boat'}},
    {id:'2',origin:'local',status:'failed',created_at:'2026-10-19',parameters:{prompt:'sea'}}];
  const rules = {scope:'works',origin:'all',status:'all',search:'',favoritesOnly:false,favorites:new Set(),sort:'newest'};
  assert.deepEqual(filterTasks(tasks,rules).map(t=>t.id),['1']);
  assert.deepEqual(filterTasks(tasks,{...rules,scope:'tasks',sort:'oldest'}).map(t=>t.id),['2','1']);
  assert.equal(filterTasks(tasks,{...rules,search:'OFFICIAL'}).length,1);
  assert.equal(filterTasks(tasks,{...rules,origin:'local'}).length,0);
  assert.equal(filterTasks(tasks,{...rules,favoritesOnly:true,favorites:new Set(['cloud:1'])}).length,1);
  assert.equal(tasks[0].id,'1');
});
test('visually rejected structural results stay in task history but are excluded from works',()=>{
  const bad={id:'bad',origin:'local',status:'succeeded',created_at:'2026-10-20',parameters:{prompt:'white output'},metrics:{visual_review:{approved:false}}};
  const rules={scope:'works',origin:'all',status:'all',search:'',favoritesOnly:false,favorites:new Set(),sort:'newest'};
  assert.equal(filterTasks([bad],rules).length,0);
  assert.equal(filterTasks([bad],{...rules,scope:'tasks'}).length,1);
});
test('local model filters separate CogVideoX from Wan',()=>{
  const tasks=[{id:'wan',origin:'local',status:'succeeded',created_at:'2026-10-20',parameters:{prompt:'boat',model_id:'Wan2.1-T2V-1.3B'}},{id:'cog',origin:'local',status:'succeeded',created_at:'2026-10-20',parameters:{prompt:'car',model_id:'zai-org/CogVideoX-2b'}}];
  const rules={scope:'works',origin:'all',status:'all',search:'',favoritesOnly:false,favorites:new Set(),sort:'newest'};
  assert.deepEqual(filterTasks(tasks,{...rules,origin:'wan'}).map(t=>t.id),['wan']);
  assert.deepEqual(filterTasks(tasks,{...rules,origin:'cogvideo'}).map(t=>t.id),['cog']);
});
test('downloadable generation record keeps reproducibility fields and excludes credentials and signed links',()=>{
  const task={id:'record-test',origin:'cloud',model:'MiniMax-H3',status:'succeeded',parameters:{prompt:'boat',duration:4,api_key:'fixture-secret'},metrics:{sha256:'abc',duration_seconds:4.45,download_url:'https://example.invalid/private-token',api_key:'fixture-secret'},owner_id:'private-owner',provider_task_id:'private-official-id'};
  const result=generationRecord(task),serialized=JSON.stringify(result);
  assert.equal(result.execution,'official_api');assert.equal(result.output.sha256,'abc');assert.equal(result.parameters.duration,4);
  assert.equal(result.visual_review.approved,null);
  for(const excluded of ['fixture-secret','private-token','private-owner','private-official-id'])assert.ok(!serialized.includes(excluded));
  const local=generationRecord({id:'x',origin:'local',parameters:{seed:73,steps:20,inference_profile:'cog2b-dpm20-fp16-v1',scheduler:'dpm',precision:'float16',vae_precision:'float16'},metrics:{validation:{decoded_frames:49}}});
  assert.equal(local.execution,'self_hosted');assert.equal(local.parameters.seed,73);assert.equal(local.output.decoded_frames,49);
  assert.equal(local.parameters.inference_profile,'cog2b-dpm20-fp16-v1');assert.equal(local.parameters.scheduler,'dpm');assert.equal(local.parameters.vae_precision,'float16');
  const h3=generationRecord({id:'h3',origin:'local',parameters:{sampler:'res_multistep',audio_vae_precision:'float32'},metrics:{peak_gpu_reserved_bytes:123}});
  assert.equal(h3.parameters.sampler,'res_multistep');assert.equal(h3.parameters.audio_vae_precision,'float32');assert.equal(h3.measurements.peak_gpu_reserved_bytes,123);
});

test('local H3 filter never mixes H3, Wan and historical cloud identities',()=>{
  const base={status:'succeeded',created_at:'2026-10-22',parameters:{prompt:'same'}};
  const tasks=[{...base,id:'wan',origin:'local',model:'Wan2.1'},
    {...base,id:'h3',origin:'local',model:'MiniMax-AI/MiniMax-H3'},
    {...base,id:'old',origin:'cloud',model:'MiniMax-H3'}];
  const rules={scope:'works',origin:'all',status:'all',search:'',favoritesOnly:false,favorites:new Set(),sort:'newest'};
  assert.deepEqual(filterTasks(tasks,{...rules,origin:'h3'}).map(t=>t.id),['h3']);
  assert.deepEqual(filterTasks(tasks,{...rules,origin:'wan'}).map(t=>t.id),['wan']);
  assert.deepEqual(filterTasks(tasks,{...rules,origin:'cloud'}).map(t=>t.id),['old']);
});
