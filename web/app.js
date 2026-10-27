"use strict";
const $ = id => document.getElementById(id);
const labels = {queued:"排队中", running:"生成中", succeeded:"已完成", failed:"生成失败", cancelled:"已取消"};
let selectedId = null, selectedTask = null, historyLimit = 12, refreshBusy = false;
let pendingSubmission = null;
let agentPlan = null, agentBusy = false;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch { throw new Error("服务返回了无法读取的结果。"); }
  if (!response.ok) {
    const message = typeof body.detail === "string" ? body.detail : "参数不符合要求，请检查描述、规格和随机种子。";
    throw new Error(message);
  }
  return body;
}

function statusLabel(element, status) {
  element.textContent = labels[status] || status;
  element.className = "status " + status;
}

function showTask(task) {
  selectedTask = task;
  $("empty-stage").hidden = true;
  $("selected-task").hidden = false;
  statusLabel($("selected-status"), task.status);
  $("selected-prompt").textContent = task.parameters.prompt;
  const p = task.parameters;
  const seconds = task.metrics?.generation_seconds;
  $("selected-meta").textContent = `${p.width}×${p.height} · ${p.frames}帧 · ${p.steps}步 · 种子 ${p.seed}` + (seconds ? ` · 生成 ${seconds.toFixed(1)}秒` : "");
  $("selected-error").hidden = !task.error;
  $("selected-error").textContent = task.error || "";
  const waiting = ["queued", "running"].includes(task.status);
  $("progress-area").hidden = !waiting;
  $("progress").value = task.progress;
  $("progress-message").textContent = task.status === "queued" ? `任务已保存，排队位置 ${task.queue_position ?? "—"}` : "正在生成视频，请稍候。";
  $("progress-text").textContent = task.status === "queued" ? "按提交顺序逐个执行，暂时离开页面也不会丢失任务。" : task.progress ? `采样进度 ${task.progress}% · 完成后还需解码验收` : "正在加载模型或整理文字描述。";
  $("video").hidden = task.status !== "succeeded";
  $("download").hidden = task.status !== "succeeded";
  if (task.status === "succeeded") {
    if ($("video").dataset.taskId !== task.id) {
      $("video").src = task.video_url;
      $("video").dataset.taskId = task.id;
    }
    $("download").href = task.video_url + "?download=true";
  } else if ($("video").hasAttribute("src")) {
    $("video").pause();
    $("video").removeAttribute("src");
    $("video").removeAttribute("data-task-id");
    $("video").load();
  }
  $("cancel").hidden = task.status !== "queued";
}

function renderHistory(history) {
  $("history-list").replaceChildren();
  $("history-count").textContent = `共 ${history.total} 个任务 · 自动刷新`;
  for (const task of history.items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-card" + (task.id === selectedId ? " selected" : "");
    const top = document.createElement("div"); top.className = "history-top";
    const badge = document.createElement("span"); statusLabel(badge, task.status);
    const date = document.createElement("small"); date.textContent = new Date(task.created_at).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
    top.append(badge, date);
    const prompt = document.createElement("p"); prompt.textContent = task.parameters.prompt;
    const meta = document.createElement("span"); meta.className = "history-meta";
    meta.textContent = `${task.parameters.width}×${task.parameters.height} · ${task.parameters.frames}帧 · ${task.id.slice(0,8)}`;
    button.append(top, prompt, meta);
    button.addEventListener("click", async () => {
      selectedId = task.id;
      await refresh();
    });
    $("history-list").append(button);
  }
  $("more").hidden = history.items.length >= history.total;
}

async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
    const [history, health] = await Promise.all([api(`/api/tasks?limit=${historyLimit}`), api("/api/health")]);
    $("status-dot").className = "online";
    $("connection").textContent = health.worker.state === "waiting_for_gpu" ? "等待空闲显卡" : ["unavailable","not_started","stopped"].includes(health.worker.state) ? "执行进程未连接" : "服务已连接";
    if (!agentBusy) $("agent-plan").disabled = !health.agent?.weights_ready;
    if (!selectedId && history.items.length) selectedId = history.items[0].id;
    if (selectedId) showTask(await api("/api/tasks/" + selectedId));
    renderHistory(history);
    $("history-error").textContent = "";
  } catch (error) {
    $("status-dot").className = "";
    $("connection").textContent = "连接中断";
    $("history-error").textContent = error.message + " 保留当前画面，稍后会自动重试。";
  } finally { refreshBusy = false; }
}

$("task-form").addEventListener("submit", async event => {
  event.preventDefault();
  const body = {prompt:$("prompt").value.trim(), preset:$("preset").value, seed:Number($("seed").value), negative_prompt:$("negative").value};
  if (!body.prompt) { $("form-message").textContent = "请先输入画面描述。"; return; }
  const fingerprint = JSON.stringify(body);
  if (!pendingSubmission || pendingSubmission.fingerprint !== fingerprint) pendingSubmission = {fingerprint, key:crypto.randomUUID()};
  $("submit").disabled = true;
  $("form-message").textContent = "正在提交…";
  try {
    const task = await api("/api/tasks", {method:"POST", headers:{"Content-Type":"application/json", "Idempotency-Key":pendingSubmission.key}, body:fingerprint});
    selectedId = task.id;
    pendingSubmission = null;
    showTask(task);
    $("form-message").textContent = "任务已保存，可以在历史记录中随时查看。";
    await refresh();
  } catch(error) { $("form-message").textContent = error.message + " 可重试提交；相同请求会防止重复入队。"; }
  finally { $("submit").disabled = false; }
});
$("example").addEventListener("click", () => { $("prompt").value = "A blue paper boat floats gently across a shallow bowl of water. Small ripples move around it, soft daylight, steady camera, close-up."; $("prompt").focus(); });
$("preset").addEventListener("change", () => { $("preset-hint").textContent = $("preset").value === "fast" ? "约1秒视频。已测生成约26秒，排队与加载另计，实际随负载变化。" : "约2秒视频。实验生成约62秒，排队与加载另计，实际随负载变化。"; });
$("cancel").addEventListener("click", async () => {
  $("cancel").disabled = true;
  try { showTask(await api(`/api/tasks/${selectedId}/cancel`, {method:"POST"})); await refresh(); }
  catch(error) { $("form-message").textContent = error.message; }
  finally { $("cancel").disabled = false; }
});
$("reuse").addEventListener("click", () => {
  if (!selectedTask) return;
  const p = selectedTask.parameters;
  $("prompt").value = p.prompt; $("negative").value = p.negative_prompt; $("seed").value = p.seed;
  $("preset").value = p.width === 480 ? "preview" : "fast";
  $("preset").dispatchEvent(new Event("change")); $("prompt").focus();
});
$("refresh").addEventListener("click", refresh);
$("more").addEventListener("click", () => { historyLimit = Math.min(historyLimit + 12, 100); refresh(); });
$("video").addEventListener("error", () => { $("selected-error").hidden = false; $("selected-error").textContent = "视频加载失败，可以刷新或尝试下载文件。"; });

function clearPlan() {
  agentPlan = null;
  $("agent-result").hidden = true;
  $("agent-message").textContent = "";
}
for (const id of ["prompt", "preset", "seed", "negative"]) $(id).addEventListener("input", clearPlan);
$("example").addEventListener("click", clearPlan);
$("reuse").addEventListener("click", clearPlan);
$("agent-plan").addEventListener("click", async () => {
  const request = $("prompt").value.trim();
  if (request.length < 3 || request.length > 1000) { $("agent-message").textContent = "请用3到1000字描述需求；更长描述可手动提交。"; return; }
  if (agentBusy) return;
  clearPlan(); agentBusy = true; $("agent-plan").disabled = true;
  $("agent-message").textContent = "本地助手正在整理，可能需要几十秒；这一步不会提交生成任务。";
  try {
    const plan = await api("/api/agent/plan", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({request})});
    if ($("prompt").value.trim() !== request) { $("agent-message").textContent = "描述已改变，请重新整理，当前方案未应用。"; return; }
    agentPlan = plan;
    $("agent-result").hidden = false;
    $("agent-summary").textContent = plan.call.summary;
    $("agent-meta").textContent = plan.decision_source === "platform_precheck" ? "平台规格与工具范围检查 · 此回复未调用语言模型" : `Qwen2.5-1.5B · CPU · 整理耗时 ${plan.planning_seconds.toFixed(1)}秒`;
    const isTask = plan.call.tool === "create_video_task";
    $("agent-submit").hidden = !isTask; $("agent-submit").disabled = false;
    $("agent-description").textContent = isTask ? plan.call.arguments.prompt : plan.call.arguments.question;
    $("agent-message").textContent = isTask ? `建议规格：${plan.call.arguments.preset === "preview" ? "清晰预览" : "快速验证"}，随机种子 ${plan.call.arguments.seed}。确认下方方案后再生成。` : "请按助手的问题补充或调整上方需求。";
  } catch (error) { $("agent-message").textContent = error.message + " 也可以继续手动填写和提交。"; }
  finally { agentBusy = false; $("agent-plan").disabled = false; }
});
$("agent-submit").addEventListener("click", async () => {
  if (!agentPlan) return;
  $("agent-submit").disabled = true;
  try {
    const task = await api(`/api/agent/plans/${agentPlan.id}/submit`, {method:"POST"});
    selectedId = task.id; showTask(task);
    $("agent-message").textContent = "方案已调用任务工具并保存，可在右侧与历史中查看。";
    await refresh();
  } catch (error) { $("agent-message").textContent = error.message; $("agent-submit").disabled = false; }
});
refresh();
setInterval(refresh, 3000);
