import test from 'node:test';import assert from 'node:assert/strict';
import {makePayload,blockingReason,formFromTask,modelDisplayName,generationRecord,filterTasks} from '../web/studio-state.mjs';
const cid='a'.repeat(32),form={model:'video-api:'+cid,prompt:'Blue boat',duration:5,duration_mode:'manual',resolution:'768P',ratio:'16:9',allow_paid_call:true};
test('video API requires separate consent and excludes all local fields',()=>{
 assert.throws(()=>makePayload({...form,allow_paid_call:false}));
 assert.deepEqual(makePayload({...form,seed:NaN,preset:'invalid'}),{connection_id:cid,prompt:'Blue boat',duration:5,resolution:'768P',ratio:'16:9',allow_paid_call:true});
});
test('video API does not depend on GPU availability or legacy operator cloud balance',()=>{
 const model={id:form.model,configured:true,min_seconds:4,max_seconds:15,verified:false};
 assert.equal(blockingReason(model,{}, {worker:{state:'maintenance'}},false,true,5),'');
 assert.ok(blockingReason(model,{}, {},false,true,3));assert.ok(blockingReason({...model,configured:false},{},{},false,true,5));
});
test('API reuse requires fresh consent and keeps its identity apart from local H3',()=>{
 const task={id:'fixture',origin:'api',connection_id:cid,model:'MiniMax-H3',parameters:{prompt:'Blue boat',duration:5,resolution:'768P',ratio:'16:9'},metrics:{},created_at:'2026-10-25'};
 const draft=formFromTask(task);assert.equal(draft.model,form.model);assert.equal(draft.allow_paid_call,false);
 assert.ok(modelDisplayName(task).includes('API'));assert.equal(generationRecord(task).execution,'customer_video_api');
 assert.equal(filterTasks([task],{scope:'tasks',origin:'h3',status:'all',search:'',favorites:new Set(),sort:'newest'}).length,0);
});
