import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

test('bootstrap retries network/HTTP/schema failure without choosing an anonymous identity',async()=>{
  const source=await readFile(new URL('../web/session.js',import.meta.url),'utf8');
  let attempts=0;
  const status={textContent:''},note={textContent:'',dataset:{}};
  const actual={authenticated:true,csrf:'fixture-csrf',user:{id:'fixture-owner',role:'admin'}};
  const window={fetch:async()=>{
    attempts++;
    if(attempts===1)throw Error('offline');
    if(attempts===2)return {ok:false};
    if(attempts===3)return {ok:true,json:async()=>({})};
    return {ok:true,json:async()=>actual};
  }};
  const document={getElementById:id=>id==='connection'?status:note,querySelector:()=>null};
  vm.runInNewContext(source,{window,document,AbortSignal,URL,Headers,Promise,setTimeout:fn=>queueMicrotask(fn)});
  const result=await window.frameseqSession;
  assert.equal(result,actual);
  assert.equal(attempts,4);
  assert.equal(note.textContent,'');
  assert.equal(note.dataset.sessionRetry,undefined);
  assert.match(status.textContent,/重试/);
});

test('workspace navigation escapes old HTML while preserving project context and boundaries',async()=>{
  const source=await readFile(new URL('../web/session.js',import.meta.url),'utf8');
  let click;
  const document={addEventListener:(type,handler)=>{if(type==='click')click=handler;},getElementById:()=>null,querySelector:()=>null};
  const window={fetch:async()=>({ok:true,json:async()=>({authenticated:true,user:{id:'fixture'}})})};
  vm.runInNewContext(source,{window,document,location:{origin:'http://127.0.0.1:18080',href:'http://127.0.0.1:18080/projects'},AbortSignal,URL,Headers,Promise,setTimeout});
  await window.frameseqSession;
  const anchor=(href,download=false)=>({href,hasAttribute:name=>name==='download'&&download,getAttribute:()=>href});
  const internal=anchor('http://127.0.0.1:18080/studio?project=fixture&shot=two#details');click({target:{closest:()=>internal}});
  const url=new URL(internal.href,'http://127.0.0.1:18080');assert.equal(url.pathname,'/studio');assert.equal(url.searchParams.get('project'),'fixture');assert.equal(url.searchParams.get('shot'),'two');assert.equal(url.hash,'#details');assert.equal(url.searchParams.get('ui'),'shared-shell-20261025-v1');
  for(const link of [anchor('https://example.org/studio'),anchor('#shots'),anchor('/api/tasks/fixture/video',true)]){const original=link.href;click({target:{closest:()=>link}});assert.equal(link.href,original);}
});
