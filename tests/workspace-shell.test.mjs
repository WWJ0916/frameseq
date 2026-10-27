import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

test('every workspace renders the exact same sidebar before JavaScript runs', async () => {
  const template=(await readFile(new URL('../web/workspace-sidebar.html',import.meta.url),'utf8')).replaceAll('\r\n','\n').trim();
  for(const page of ['studio-v3','projects','storyboard','edits','video-api','index','admin']){
    const html=(await readFile(new URL(`../web/${page}.html`,import.meta.url),'utf8')).replaceAll('\r\n','\n');
    assert.equal(html.match(/<aside\b[^>]*data-workspace-shell[^>]*>[\s\S]*?<\/aside>/)?.[0],template,page);
    assert.equal((html.match(/id="connection"/g)||[]).length,1,page);
    assert.equal((html.match(/id="theme"/g)||[]).length,1,page);
    assert.doesNotMatch(html,/assistant-api|语言助手 API|data-workspace-layout="framed"/);
    assert.match(html,/workspace-shell\.css/);assert.match(html,/workspace-shell\.js/);
  }
  assert.deepEqual([...template.matchAll(/data-view="(.*?)"/g)].map(m=>m[1]),['create','works','tasks']);
  const projects=await readFile(new URL('../web/projects.html',import.meta.url),'utf8');
  assert.match(projects,/<main[^>]*>[\s\S]*id="project-list"/);
});

test('deep links choose the correct navigation item and use one shared theme handler', async () => {
  const source=await readFile(new URL('../web/workspace-shell.js',import.meta.url),'utf8');
  for(const view of ['works','tasks','create']){
    const links=['create','works','tasks'].map(value=>({href:`http://test/studio?view=${value}`,dataset:{view:value},classList:{toggle(_,selected){this.selected=selected;}},setAttribute(){},removeAttribute(){}}));
    let handler;const theme={setAttribute(){},addEventListener(_,fn){assert.equal(handler,undefined);handler=fn;}};
    const doc={querySelector:()=>({querySelectorAll:()=>links}),getElementById:()=>theme,documentElement:{dataset:{}}};
    const memory=new Map();
    vm.runInNewContext(source,{document:doc,location:{pathname:'/studio',href:`http://test/studio?view=${view}`,search:`?view=${view}`},URL,URLSearchParams,localStorage:{getItem:k=>memory.get(k),setItem:(k,v)=>memory.set(k,v)},window:{frameseqSession:Promise.resolve({authenticated:false})}});
    assert.deepEqual(links.map(n=>n.classList.selected),['create','works','tasks'].map(v=>v===view));
    handler();assert.equal(doc.documentElement.dataset.theme,'light');
    handler();assert.equal(doc.documentElement.dataset.theme,'dark');
  }
});
