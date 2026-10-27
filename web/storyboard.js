'use strict';
try {document.documentElement.dataset.theme = JSON.parse(localStorage.getItem('frameseq-theme')) === 'light' ? 'light' : 'dark';} catch {document.documentElement.dataset.theme = 'dark';}
const el = id => document.getElementById(id);
let board = null, dirty = false, remaining = null;
const emptyShot = () => ({description: '', narration: '', seconds: 10});

async function request(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : '请检查标题、镜头描述和时长。');
  return body;
}
function markDirty() {
  dirty = true;
  el('board-message').textContent = '有尚未保存的修改。保存只写入本人项目数据库，不生成视频。';
}
function summary() {
  const seconds = board.shots.reduce((total, shot) => total + Number(shot.seconds || 0), 0);
  el('total-seconds').textContent = seconds + ' 秒';
  el('output-cost').textContent = '本地GPU';
  el('budget-note').textContent = '这里只保存剪辑计划，不自动生成。H3本地Turbo 8步原生约5.17秒，较长计划需拆分镜头；也可在视频模型API页配置自己的云端模型。';
  el('add-shot').disabled = board.shots.length >= 15;
}
function button(label, action) {
  const node = document.createElement('button');
  node.type = 'button'; node.className = 'secondary'; node.textContent = label;
  node.addEventListener('click', action); return node;
}
function field(parent, label, type, value, onInput, properties = {}) {
  const node = document.createElement(type);
  Object.assign(node, properties); node.value = value;
  const wrapper = document.createElement('label');
  wrapper.append(document.createTextNode(label), node); parent.append(wrapper);
  node.addEventListener('input', () => {onInput(node.value); markDirty(); summary();});
  return node;
}
function render() {
  el('title').value = board.title; el('synopsis').value = board.synopsis;
  el('shots').replaceChildren();
  board.shots.forEach((shot, index) => {
    const card = document.createElement('article'); card.className = 'panel shot';
    const head = document.createElement('div'); head.className = 'shot-head';
    const title = document.createElement('h3'); title.textContent = `镜头 ${String(index + 1).padStart(2, '0')} · 草稿`;
    const tools = document.createElement('div'); tools.className = 'shot-tools';
    const up = button('↑ 上移', () => {
      [board.shots[index - 1], board.shots[index]] = [board.shots[index], board.shots[index - 1]]; markDirty(); render();
    }); up.disabled = index === 0;
    const down = button('↓ 下移', () => {
      [board.shots[index + 1], board.shots[index]] = [board.shots[index], board.shots[index + 1]]; markDirty(); render();
    }); down.disabled = index === board.shots.length - 1;
    const remove = button('删除', () => {board.shots.splice(index, 1); markDirty(); render();});
    remove.disabled = board.shots.length === 1;
    tools.append(up, down, remove); head.append(title, tools);
    const body = document.createElement('div'); body.className = 'shot-body';
    const image = document.createElement('div'), voice = document.createElement('div');
    field(image, '画面、动作与镜头', 'textarea', shot.description, value => shot.description = value, {rows: 4, maxLength: 2000});
    field(voice, '旁白或对白（尚未配音）', 'textarea', shot.narration, value => shot.narration = value, {rows: 2, maxLength: 500});
    field(voice, '目标秒数（4–15）', 'input', shot.seconds, value => shot.seconds = Number(value), {type: 'number', min: 4, max: 15, step: 1});
    body.append(image, voice); card.append(head, body); el('shots').append(card);
  });
  summary();
}
function reset() {board = {title: '', synopsis: '', shots: [emptyShot()], revision: 0}; dirty = false; render();}
function canReplace() {return !dirty || window.confirm('当前修改尚未保存，仍要打开另一份草稿吗？');}
async function loadList() {
  const data = await request('/api/storyboards');
  el('board-list').replaceChildren(new Option('打开已保存草稿…', ''));
  for (const saved of data.items) el('board-list').append(new Option(saved.title + ' · v' + saved.revision, saved.id));
  if (board?.id) el('board-list').value = board.id;
}
el('title').addEventListener('input', () => {board.title = el('title').value; markDirty();});
el('synopsis').addEventListener('input', () => {board.synopsis = el('synopsis').value; markDirty();});
el('add-shot').addEventListener('click', () => {if (board.shots.length < 15) {board.shots.push(emptyShot()); markDirty(); render();}});
el('new-board').addEventListener('click', () => {if (canReplace()) {reset(); el('board-list').value = ''; el('board-message').textContent = '新建草稿尚未保存。';}});
el('board-list').addEventListener('change', async () => {
  const id = el('board-list').value; if (!id) return;
  if (!canReplace()) {el('board-list').value = board.id || ''; return;}
  try {board = await request('/api/storyboards/' + id); dirty = false; render(); el('board-message').textContent = `已打开v${board.revision}；仍为未生成草稿。`;}
  catch (error) {el('board-message').textContent = error.message;}
});
el('template').addEventListener('click', () => {
  if (!canReplace()) return;
  board = {title: '纸船去往哪里', synopsis: '六个镜头组成有开端、遇阻和抵达的纸船故事。统一蓝色纸船、石子浅溪、柔和日光。人工模板；跨镜头外观一致性尚未制作验证。', revision: 0, shots: [
    {description: '清晨石子浅溪全景，一只蓝色纸船停在岸边。固定镜头，写实风格，柔和日光，无文字。', narration: '它第一次离开岸边。', seconds: 10},
    {description: '同一只蓝色纸船顺着清澈浅溪缓缓漂动，水面倒映树影，低机位跟拍。', narration: '不知道前面有什么，也想往前看看。', seconds: 10},
    {description: '蓝色纸船靠近一块露出水面的石头，水流让船身轻轻转动，近景。', narration: '一块石头，挡住了去路。', seconds: 10},
    {description: '蓝色纸船在石头旁的水流中缓慢转向，绕过石头，保持日光与写实风格。', narration: '换个方向，也可以继续。', seconds: 10},
    {description: '蓝色纸船离开石头，驶入平缓开阔的浅水区，水波向外扩散，镜头缓慢拉远。', narration: '水流变缓，风也轻了。', seconds: 10},
    {description: '蓝色纸船在浅水区停稳，落叶轻轻漂过，柔和日光照亮船身，固定近景。', narration: '有时候，抵达就是找到自己的节奏。', seconds: 10}
  ]}; markDirty(); render(); el('board-list').value = '';
});
el('save-board').addEventListener('click', async () => {
  el('save-board').disabled = true;
  try {
    const body = {title: board.title, synopsis: board.synopsis, shots: board.shots, revision: board.revision};
    const saved = await request('/api/storyboards' + (board.id ? '/' + board.id : ''), {
      method: board.id ? 'PUT' : 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    board = saved; dirty = false; render(); await loadList();
    el('board-message').textContent = `已保存v${board.revision}，规划${board.total_seconds}秒；没有提交生成或产生API用量。`;
  } catch (error) {el('board-message').textContent = error.message;}
  finally {el('save-board').disabled = false;}
});
el('export-board').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify({...board, status: 'draft_only', execution_supported: false}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = 'storyboard-draft.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
window.addEventListener('beforeunload', event => {if (dirty) {event.preventDefault(); event.returnValue = '';}});
reset(); loadList().catch(error => el('board-message').textContent = error.message);
