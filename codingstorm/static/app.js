'use strict';

const $ = (sel, root = document) => root.querySelector(sel);

const STATUS_LABEL = {
  queued: '排队中',
  running: '执行中',
  awaiting_review: '待审阅',
  merged: '已合并',
  discarded: '已丢弃',
  failed: '失败',
  interrupted: '已中断',
  cancelled: '已取消',
};

const state = {
  projects: [],
  projectId: null,
  tasks: [],
  task: null,
  schedules: [],
  window: null,
  ws: null,
  seen: new Set(),   // 已渲染的事件 seq，用于去重（历史与实时可能重叠）
  expanded: new Set(),
  progressAt: 0,
};

// ---------------- 工具 ----------------

let toastTimer = null;
function toast(msg, isErr) {
  let box = $('.toast');
  if (!box) {
    box = document.createElement('div');
    box.className = 'toast';
    document.body.appendChild(box);
  }
  box.textContent = msg;
  box.classList.toggle('err', !!isErr);
  box.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove('show'), 4000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && body.detail) msg = body.detail;
    } catch (_) { /* 非 JSON 响应，用默认消息 */ }
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

/** 建元素。文本一律用 textContent，避免把仓库内容当 HTML 解析。 */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'html') node.innerHTML = v;      // 仅用于服务端已转义的 diff
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function badge(status) {
  return el('span', { class: `badge ${status}`, text: STATUS_LABEL[status] || status });
}

function shortSha(sha) {
  return sha ? sha.slice(0, 8) : '—';
}

/** 时间一律按**配置的时区**显示，不用浏览器时区。
 *  用户配的「每天 9 点」指的是那个时区的 9 点，浏览器在哪儿不影响它的含义。 */
function fmtLocal(iso, withDate = true) {
  if (!iso) return '—';
  const opts = { hour: '2-digit', minute: '2-digit', hour12: false };
  if (withDate) Object.assign(opts, { month: '2-digit', day: '2-digit' });
  if (state.window && state.window.timezone) opts.timeZone = state.window.timezone;
  return new Intl.DateTimeFormat('zh-CN', opts).format(new Date(iso));
}

function untilText(iso) {
  if (!iso) return '';
  const minutes = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (minutes <= 0) return '马上';
  if (minutes < 60) return `${minutes} 分钟后`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时 ${minutes % 60} 分后`;
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时后`;
}

// ---------------- 项目 ----------------

async function loadProjects() {
  state.projects = await api('/api/projects');
  renderProjects();
}

function renderProjects() {
  const ul = $('#projects');
  ul.replaceChildren();

  if (!state.projects.length) {
    ul.append(el('li', { class: 'muted', text: '还没有项目，先在下面注册一个' }));
    return;
  }
  for (const p of state.projects) {
    ul.append(el('li', {
      class: p.id === state.projectId ? 'active' : '',
      onclick: () => selectProject(p.id),
    }, el('div', { text: p.name }), el('div', { class: 'muted', text: p.target_branch })));
  }
}

async function createProject(form) {
  const data = Object.fromEntries(new FormData(form));
  try {
    const p = await api('/api/projects', { method: 'POST', body: JSON.stringify(data) });
    form.reset();
    await loadProjects();
    await selectProject(p.id);
    toast(`项目 ${p.name} 已注册`);
  } catch (err) {
    toast(`注册失败：${err.message}`, true);
  }
}

// ---------------- 任务 ----------------

async function selectProject(id) {
  state.projectId = id;
  filePath = '';
  renderProjects();
  const project = state.projects.find((p) => p.id === id);
  $('#queue-title').textContent = project ? `${project.name} 的任务` : '任务队列';
  await loadTasks();
  await loadSchedules();
  await loadContext();
  renderWindow();  // 横幅里那行「本项目」跟着选中项目走
  updateRefTabs();
  refreshFilesIfVisible();
}

async function loadTasks() {
  if (!state.projectId) {
    state.tasks = [];
  } else {
    state.tasks = await api(`/api/tasks?project_id=${encodeURIComponent(state.projectId)}`);
  }
  renderTasks();
}

function renderTasks() {
  const ul = $('#tasks');
  ul.replaceChildren();
  $('#queue-count').textContent = state.tasks.length ? `${state.tasks.length} 条` : '';

  if (!state.tasks.length) {
    ul.append(el('li', { class: 'muted', text: '队列为空' }));
    return;
  }
  // 窗口关着时排队中的任务一条都跑不了。不标出来的话，看起来就像卡死了
  const waiting = !!(state.window && state.window.enabled && !state.window.open);

  for (const t of state.tasks) {
    if (state.task && state.task.id === t.id) {
      const fresh = state.task;
      Object.assign(t, { status: fresh.status });
    }
    const title = el('span', { class: 'title', text: t.title });
    if (waiting && t.status === 'queued') {
      title.append(el('span', { class: 'muted small', text: ' · 等窗口' }));
    }
    ul.append(el('li', {
      class: state.task && state.task.id === t.id ? 'active' : '',
      onclick: () => openTask(t.id),
    },
      badge(t.status),
      title,
    ));
  }
}

async function submitTask(form) {
  if (!state.projectId) { toast('先选一个项目', true); return; }
  const data = Object.fromEntries(new FormData(form));
  if (!data.title || !data.title.trim()) { toast('标题不能为空', true); return; }
  try {
    const t = await api(`/api/projects/${state.projectId}/tasks`, {
      method: 'POST',
      body: JSON.stringify({
        title: data.title,
        kind: data.kind || 'task',
        priority: Number(data.priority || 0),
      }),
    });
    form.reset();
    await loadTasks();
    toast('已入队 —— 可以关掉这一页，跑完再回来看');
    await openTask(t.id);
  } catch (err) {
    toast(`提交失败：${err.message}`, true);
  }
}

// ---------------- 执行窗口 ----------------

async function loadWindow() {
  state.window = await api('/api/window');
  renderWindow();
}

function renderWindow() {
  const w = state.window;
  const box = $('#window-banner');
  if (!w || !w.enabled) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.replaceChildren();

  const spans = w.windows.map(([a, b]) => `${a}–${b}`).join('、');
  let text;
  if (w.disabled) {
    text = `执行窗口 ${spans} 已临时关闭 —— 任务不再等窗口`;
  } else if (w.open) {
    text = `谷时窗口 ${spans}（${w.timezone}）· 开启中，${untilText(w.next_close_at)}关闭`;
  } else {
    text = `谷时窗口 ${spans}（${w.timezone}）· 已关闭，${untilText(w.next_open_at)}开启`;
  }

  box.classList.toggle('warn', w.disabled);
  box.append(el('span', { class: 'banner-text', text }));
  box.append(el('button', {
    class: w.disabled ? '' : 'ghost',
    text: w.disabled ? '恢复窗口限制' : '临时关闭窗口',
    title: w.disabled
      ? '重新按窗口执行任务'
      : '急事用：立即放行所有排队任务。重启平台后会自动恢复限制',
    onclick: () => toggleWindow(!w.disabled),
  }));
  if (w.disabled) {
    // 不说清楚的话，第二天看到账单才知道忘了恢复
    box.append(el('span', { class: 'muted', text: '重启平台会自动恢复' }));
  }
  renderProjectWindow();
}

/** 项目级的窗口例外。和全局开关挤在同一条横幅里 —— 它们本来就在讲同一件事。 */
function renderProjectWindow() {
  const box = $('#window-banner');
  const project = state.projects.find((p) => p.id === state.projectId);
  if (!project) return;

  const current = parseOverride(project.window_override);
  const select = el('select', {
    class: 'small',
    title: '这个项目要不要跟着谷时窗口走',
    onchange: (e) => setProjectWindow(e.target.value),
  },
    el('option', { value: 'inherit' }, '跟随全局'),
    el('option', { value: 'always' }, '本项目不受限'),
    el('option', { value: 'custom' }, '本项目自定义时段…'),
  );
  select.value = current.mode;
  box.append(el('span', { class: 'proj-window' }, `本项目：`, select));
}

function parseOverride(raw) {
  if (!raw) return { mode: 'inherit' };
  try {
    const data = JSON.parse(raw);
    return { mode: data.mode === 'always' || data.mode === 'custom' ? data.mode : 'inherit' };
  } catch (_) {
    return { mode: 'inherit' };
  }
}

async function setProjectWindow(mode) {
  let windows = null;
  if (mode === 'custom') {
    const answer = prompt('这个项目允许执行的时段，如 09:00-18:00（多段用逗号分隔）');
    if (answer === null) { renderWindow(); return; }
    windows = answer.split(',').map((s) => s.trim()).filter(Boolean)
      .map((s) => s.split(/[-–—]/).map((p) => p.trim()));
    if (!windows.length || windows.some((w) => w.length !== 2)) {
      toast('时段要写成 09:00-18:00 这样', true);
      renderWindow();
      return;
    }
  }
  try {
    const body = windows ? { mode, windows } : { mode };
    const updated = await api(`/api/projects/${state.projectId}/window`, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
    Object.assign(state.projects.find((p) => p.id === updated.id), updated);
    renderWindow();
    toast('已更新本项目的窗口设置');
  } catch (err) {
    toast(`设置失败：${err.message}`, true);
    renderWindow();  // 把下拉框拨回真实值
  }
}

async function toggleWindow(disabled) {
  try {
    state.window = await api('/api/window', {
      method: 'POST',
      body: JSON.stringify({ disabled }),
    });
    renderWindow();
    await loadTasks();
    toast(disabled ? '已临时关闭窗口，排队中的任务会立刻开始跑' : '已恢复窗口限制');
  } catch (err) {
    toast(`操作失败：${err.message}`, true);
  }
}

// ---------------- 定时任务 ----------------

async function loadSchedules() {
  if (!state.projectId) {
    state.schedules = [];
  } else {
    state.schedules = await api(`/api/schedules?project_id=${encodeURIComponent(state.projectId)}`);
  }
  renderSchedules();
}

function scheduleState(s) {
  if (s.missed_at) return { label: '已错过', cls: 'failed' };
  if (!s.enabled) return { label: '已停用', cls: 'cancelled' };
  if (!s.next_run_at) return { label: '已完成', cls: 'merged' };
  return { label: `下次 ${fmtLocal(s.next_run_at)}`, cls: 'queued' };
}

function renderSchedules() {
  const ul = $('#schedules');
  ul.replaceChildren();
  $('#schedule-count').textContent = state.schedules.length ? `${state.schedules.length} 条` : '';

  if (!state.schedules.length) {
    ul.append(el('li', { class: 'muted', text: '还没有定时任务 —— 比如「每天 9 点跑一遍文档里的命令」' }));
    return;
  }

  for (const s of state.schedules) {
    const st = scheduleState(s);
    const row = el('li', { class: 'schedule' },
      el('div', { class: 'schedule-head' },
        el('span', { class: `badge ${st.cls}`, text: st.label }),
        el('span', { class: 'rule', text: s.rule_text }),
        el('span', { class: 'title', text: s.title }),
      ),
    );

    const meta = [];
    if (s.run_count) meta.push(`自动触发 ${s.run_count} 次`);
    if (s.last_run_at) meta.push(`上次 ${fmtLocal(s.last_run_at)}`);
    if (meta.length) row.append(el('div', { class: 'muted small', text: meta.join(' · ') }));

    if (s.last_task_id && s.last_task_status === 'awaiting_review') {
      // 未合并的改动对后续任务不可见，这是周期任务最容易踩的坑
      row.append(el('div', { class: 'warn small' },
        `上一轮 ${s.last_task_id.slice(0, 8)} 还没审，本轮会切在旧主干上`));
    }
    if (s.missed_at) {
      row.append(el('div', { class: 'warn small', text: '到点时平台没在跑，这一轮没有执行' }));
    }

    const actions = el('div', { class: 'row' },
      el('button', { class: 'ghost', text: '立即跑一次', onclick: () => scheduleAction(s.id, 'run') }),
      el('button', {
        class: 'ghost',
        text: s.enabled ? '停用' : '启用',
        onclick: () => scheduleAction(s.id, s.enabled ? 'pause' : 'resume'),
      }),
      el('button', { class: 'danger', text: '删除', onclick: () => scheduleAction(s.id, 'rm') }),
    );
    if (s.last_task_id) {
      actions.append(el('button', {
        class: 'ghost', text: '看上一轮',
        onclick: () => openTask(s.last_task_id),
      }));
    }
    row.append(actions);
    ul.append(row);
  }
}

async function createSchedule(form) {
  if (!state.projectId) { toast('先选一个项目', true); return; }
  const data = Object.fromEntries(new FormData(form));
  if (!data.title || !data.title.trim()) { toast('要写清楚到点做什么', true); return; }

  const mode = data.mode;
  let rule;
  if (mode === 'once') {
    if (!data.date) { toast('选一个日期', true); return; }
    rule = { type: 'once', at: `${data.date}T${data.time}` };
  } else if (mode === 'weekly') {
    const days = [...form.querySelectorAll('#schedule-weekdays input:checked')].map((i) => i.value);
    if (!days.length) { toast('至少选一个星期几', true); return; }
    rule = { type: 'weekly', days, time: data.time };
  } else {
    rule = { type: 'daily', time: data.time };
  }

  try {
    const s = await api(`/api/projects/${state.projectId}/schedules`, {
      method: 'POST',
      body: JSON.stringify({ title: data.title, kind: data.kind || 'task', rule }),
    });
    form.reset();
    form.hidden = true;
    syncScheduleForm();
    await loadSchedules();
    toast(`已建立：${s.rule_text}，下一次 ${fmtLocal(s.next_run_at)}`);
  } catch (err) {
    toast(`建立失败：${err.message}`, true);
  }
}

async function scheduleAction(id, action) {
  if (action === 'rm' && !confirm('删掉这条定时任务？（它已经生成的任务会留着）')) return;
  try {
    if (action === 'run') {
      const t = await api(`/api/schedules/${id}/run`, { method: 'POST' });
      await loadTasks();
      toast(`已入队 ${t.id}`);
      await openTask(t.id);
      return;
    }
    if (action === 'rm') {
      await api(`/api/schedules/${id}`, { method: 'DELETE' });
    } else {
      await api(`/api/schedules/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: action === 'resume' }),
      });
    }
    await loadSchedules();
    toast({ rm: '已删除', pause: '已停用', resume: '已重新启用' }[action]);
  } catch (err) {
    toast(`操作失败：${err.message}`, true);
  }
}

/** 切换规则类型时显示对应的输入项。 */
function syncScheduleForm() {
  const mode = $('#schedule-mode').value;
  $('#schedule-date').hidden = mode !== 'once';
  $('#schedule-date').required = mode === 'once';
  $('#schedule-weekdays').hidden = mode !== 'weekly';
  $('#schedule-hint').textContent = state.window && state.window.enabled
    ? '定时任务决定什么时候入队；真正开跑还要看执行窗口 —— 落在窗口外的会排队等到窗口开。'
    : '';
}

/** 路由形如 #/task/<id> 或 #/task/<id>/diff，便于分享链接和刷新后回到原处。 */
function parseHash() {  const m = /^#\/task\/([A-Za-z0-9]+)(?:\/(chat|diff|output))?$/.exec(location.hash || '');
  return m ? { taskId: m[1], tab: m[2] || 'chat' } : null;
}

function writeHash(taskId, tab) {
  const next = `#/task/${taskId}${tab && tab !== 'chat' ? '/' + tab : ''}`;
  if (location.hash !== next) history.replaceState(null, '', next);
}

function switchTab(which) {
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b.dataset.tab === which));
  $('#pane-chat').hidden = which !== 'chat';
  $('#pane-output').hidden = which !== 'output';
  $('#pane-diff').hidden = which !== 'diff';
  if (which === 'diff' && state.task) loadDiff();
  if (which === 'chat' && state.task) loadChat();
}

// ---------------- 任务详情 ----------------

async function openTask(id, tab = 'chat') {
  if (state.ws) { state.ws.close(); state.ws = null; }
  state.seen.clear();
  state.expanded.clear();
  state.progressAt = 0;

  $('#stream').replaceChildren();
  $('#diff-body').replaceChildren();
  $('#diff-stat').textContent = '';
  $('#progress').hidden = true;
  $('#detail-panel').hidden = false;

  state.task = await api(`/api/tasks/${id}`);

  // 深链进来时侧栏还没选中项目，任务列表会是空的 —— 跟着任务切过去
  if (state.task.project_id !== state.projectId) {
    state.projectId = state.task.project_id;
    renderProjects();
    const project = state.projects.find((p) => p.id === state.projectId);
    if (project) $('#queue-title').textContent = `${project.name} 的任务`;
  }

  renderDetail();
  await loadTasks();
  await loadContext();
  await loadUsage(id);
  updateRefTabs();
  // 文件树还开着的话跟着任务刷新 —— 切到「本次任务」时看的就是这条任务的产出
  refreshFilesIfVisible();
  switchTab(tab);
  writeHash(id, tab);
  connectWs(id);
}

function renderDetail() {
  const t = state.task;
  if (!t) return;
  $('#detail-title').textContent = t.title;
  $('#detail-status').replaceChildren(badge(t.status));

  const meta = $('#detail-meta');
  meta.replaceChildren();
  const rows = [
    ['分支', t.branch, true],
    ['基点', t.base_commit ? shortSha(t.base_commit) : null, true],
    ['提交', t.commit_sha ? shortSha(t.commit_sha) : null, true],
    ['合并', t.merge_commit_sha ? shortSha(t.merge_commit_sha) : null, true],
    ['类型', t.kind, false],
  ];
  for (const [label, value, mono] of rows) {
    if (!value) continue;
    meta.append(el('span', {}, `${label}：`,
      mono ? el('code', { text: value }) : document.createTextNode(value)));
  }
  if (t.error_text) {
    meta.append(el('span', { class: 'ev-error', text: t.error_text }));
  }
  renderActions();
}

function renderActions() {
  const box = $('#actions');
  box.replaceChildren();
  const t = state.task;
  if (!t) return;

  if (t.status === 'awaiting_review') {
    box.append(el('button', { onclick: () => doApprove() }, '批准并合入'));
    box.append(el('button', { class: 'danger', onclick: () => doDiscard() }, '丢弃'));
    box.append(el('button', { class: 'ghost', onclick: () => loadDiff() }, '刷新改动'));
  } else if (['failed', 'interrupted', 'cancelled'].includes(t.status)) {
    box.append(el('button', { onclick: () => doRequeue() }, '重新入队'));
    box.append(el('button', { class: 'danger', onclick: () => doDiscard() }, '丢弃'));
  } else if (t.status === 'queued') {
    box.append(el('button', { class: 'ghost', onclick: () => doCancel() }, '取消'));
  } else if (t.status === 'merged' || t.status === 'discarded') {
    box.append(el('span', { class: 'muted', text: '此任务已结束' }));
  }
}

async function doApprove() {
  try {
    state.task = await api(`/api/tasks/${state.task.id}/approve`, { method: 'POST' });
    renderDetail();
    await loadTasks();
    await loadSchedules();    // 「上一轮还没审」的提示该消失了
    refreshFilesIfVisible();  // 主干刚往前走了，树上的东西变了
    toast('已合并到主干');
  } catch (err) {
    toast(`批准失败：${err.message}`, true);
  }
}

async function doDiscard() {
  if (!confirm('丢弃会删掉这条任务的分支和工作区，确定吗？')) return;
  try {
    state.task = await api(`/api/tasks/${state.task.id}/discard`, { method: 'POST' });
    renderDetail();
    await loadTasks();
    await loadSchedules();
    refreshFilesIfVisible();
    toast('已丢弃');
  } catch (err) {
    toast(`丢弃失败：${err.message}`, true);
  }
}

async function doRequeue() {
  try {
    state.task = await api(`/api/tasks/${state.task.id}/requeue`, { method: 'POST' });
    openTask(state.task.id);
    await loadTasks();
    toast('已重新入队');
  } catch (err) {
    toast(`重新入队失败：${err.message}`, true);
  }
}

async function doCancel() {
  try {
    state.task = await api(`/api/tasks/${state.task.id}/cancel`, { method: 'POST' });
    renderDetail();
    await loadTasks();
    toast('已取消');
  } catch (err) {
    toast(`取消失败：${err.message}`, true);
  }
}

async function loadUsage(taskId) {
  const box = $('#usage');
  try {
    renderUsage(await api(`/api/tasks/${taskId}/attempts`));
  } catch (_) {
    box.textContent = '';
  }
}

/** 用量按 attempt 分行。沉淀那步单列 —— 它是每个任务的固定开销，混在一起就看不见了。 */
function renderUsage(attempts) {
  const box = $('#usage');
  box.replaceChildren();
  if (!attempts.length) return;

  const lines = [];
  let total = null;
  for (const a of attempts) {
    const label = a.origin === 'sediment' ? '记录沉淀' : '执行任务';
    const cost = a.cost_usd != null ? `  $${a.cost_usd.toFixed(4)}` : '';
    if (a.cost_usd != null) total = (total ?? 0) + a.cost_usd;
    lines.push(
      `${label}  in=${a.input_tokens ?? 0}  out=${a.output_tokens ?? 0}` +
      `  cache读=${a.cache_read_tokens ?? 0}${cost}`
    );
  }
  if (total != null) {
    lines.push(`合计 $${total.toFixed(4)}${attempts[0].price_version ? `（价目 ${attempts[0].price_version}）` : ''}`);
  } else {
    lines.push('（未配置价目表，只统计 token）');
  }
  box.textContent = lines.join('\n');
}

async function loadDiff() {
  const box = $('#diff-body');
  box.replaceChildren(el('div', { class: 'muted', text: '加载中…' }));
  try {
    const d = await api(`/api/tasks/${state.task.id}/diff`);
    $('#diff-stat').textContent = d.stat || '';
    // d.html 由服务端渲染并已转义，是唯一可以当 HTML 用的内容
    box.replaceChildren(el('div', { html: d.html }));
  } catch (err) {
    box.replaceChildren(el('div', { class: 'ev ev-error', text: `加载 diff 失败：${err.message}` }));
  }
}

// ---------------- 项目上下文 ----------------

let ctxData = null;
let editingDoc = null;

async function loadContext() {
  if (!state.projectId) {
    $('#context-panel').hidden = true;
    ctxData = null;
    return;
  }
  try {
    ctxData = await api(`/api/projects/${state.projectId}/context`);
  } catch (err) {
    toast(`加载上下文失败：${err.message}`, true);
    return;
  }
  renderContext();
}

function renderContext() {
  if (!ctxData) return;
  $('#context-panel').hidden = false;

  const bytes = ctxData.pointer_bytes;
  const limit = ctxData.pointer_soft_limit;
  const tag = $('#ctx-bytes');
  tag.textContent = `${bytes} / ${limit} 字节`;
  tag.style.color = bytes > limit ? 'var(--del-fg)' : '';
  tag.title = bytes > limit ? '超过软上限了 —— 多半是把「介绍性内容」也写了进去' : '';

  $('#pointer-text').value = ctxData.pointer;
  $('#journal-text').textContent = ctxData.journal;
  showDocBrowse();
  renderDocs();
}

function renderDocs() {
  const ul = $('#doc-list');
  ul.replaceChildren();
  const docs = (ctxData && ctxData.docs) || [];
  if (!docs.length) {
    ul.append(el('li', { class: 'muted', text: '还没有文档' }));
    return;
  }
  for (const d of docs) {
    ul.append(el('li', { onclick: () => openDoc(d.path) },
      el('span', { class: 'title', text: d.path }),
      el('span', { class: 'muted', text: `${d.size} B` })));
  }
}

function showDocBrowse() {
  $('#doc-browse').hidden = false;
  $('#doc-editor').hidden = true;
}

async function savePointer() {
  try {
    ctxData = await api(`/api/projects/${state.projectId}/context/pointer`, {
      method: 'PUT',
      body: JSON.stringify({ text: $('#pointer-text').value }),
    });
    renderContext();
    toast('指针图已保存 —— 下一个任务就会带上它');
  } catch (err) {
    toast(`保存失败：${err.message}`, true);
  }
}

async function openDoc(path) {
  try {
    const d = await api(
      `/api/projects/${state.projectId}/context/docs/${encodeURIComponent(path)}`);
    editingDoc = path;
    $('#doc-editor-title').textContent = path;
    $('#doc-text').value = d.text;
    $('#doc-browse').hidden = true;
    $('#doc-editor').hidden = false;
  } catch (err) {
    toast(`打开失败：${err.message}`, true);
  }
}

async function saveDoc() {
  if (!editingDoc) return;
  try {
    await api(`/api/projects/${state.projectId}/context/docs/${encodeURIComponent(editingDoc)}`,
      { method: 'PUT', body: JSON.stringify({ text: $('#doc-text').value }) });
    await loadContext();
    toast(`${editingDoc} 已保存`);
  } catch (err) {
    toast(`保存失败：${err.message}`, true);
  }
}

async function deleteDoc() {
  if (!editingDoc || !confirm(`删除 ${editingDoc}？`)) return;
  try {
    await api(`/api/projects/${state.projectId}/context/docs/${encodeURIComponent(editingDoc)}`,
      { method: 'DELETE' });
    await loadContext();
    toast('已删除');
  } catch (err) {
    toast(`删除失败：${err.message}`, true);
  }
}

async function createDoc(form) {
  const path = (Object.fromEntries(new FormData(form)).path || '').trim();
  if (!path) return;
  try {
    await api(`/api/projects/${state.projectId}/context/docs/${encodeURIComponent(path)}`,
      { method: 'PUT', body: JSON.stringify({ text: `# ${path}\n` }) });
    form.reset();
    await loadContext();
    await openDoc(path);
  } catch (err) {
    toast(`创建失败：${err.message}`, true);
  }
}

// ---------------- 文件浏览 ----------------

let filePath = '';      // 当前所在目录，空串表示仓库根
let fileRefMode = '';   // '' = 主干，'task' = 本次任务

function currentFileRef() {
  if (fileRefMode === 'task') {
    const t = state.task;
    if (!t) return '';
    // 用提交 sha 而不是分支名：批准之后分支就删掉了，sha 一直都在
    return t.commit_sha || t.branch || '';
  }
  const p = state.projects.find((x) => x.id === state.projectId);
  return p ? p.target_branch : '';
}

function updateRefTabs() {
  if (fileRefMode === 'task' && !state.task) fileRefMode = '';
  document.querySelectorAll('.rtab').forEach((b) => {
    b.disabled = b.dataset.ref === 'task' && !state.task;
    b.classList.toggle('active', (b.dataset.ref || '') === fileRefMode);
  });
}

async function loadFileList() {
  const box = $('#file-tree');
  const crumbs = $('#file-crumbs');
  box.replaceChildren();
  crumbs.replaceChildren();
  updateRefTabs();

  if (!state.projectId) {
    box.append(el('div', { class: 'tree-empty', text: '先选一个项目' }));
    return;
  }
  const ref = currentFileRef();
  if (!ref) {
    box.append(el('div', { class: 'tree-empty', text: '这条任务还没产出提交，只有主干可看' }));
    return;
  }

  let data;
  try {
    data = await api(`/api/projects/${state.projectId}/tree`
      + `?path=${encodeURIComponent(filePath)}&ref=${encodeURIComponent(ref)}`);
  } catch (err) {
    box.append(el('div', { class: 'tree-empty', text: `读不到：${err.message}` }));
    return;
  }

  crumbs.append(...crumbNodes(data.path));
  if (!data.entries.length) {
    box.append(el('div', { class: 'tree-empty', text: '（空目录）' }));
    return;
  }
  for (const e of data.entries) {
    const isDir = e.type === 'dir';
    box.append(el('div', {
      class: `tree-row ${isDir ? 'dir' : 'file'}`,
      onclick: () => (isDir ? enterDir(e.path) : openFile(e.path)),
    },
      // 目录名后面加个斜杠 —— 比图标省事，窄侧栏里也看得清
      el('span', { text: isDir ? `${e.name}/` : e.name }),
      !isDir && e.size !== null ? el('span', { class: 'sz', text: `${e.size} B` }) : null,
    ));
  }
}

function crumbNodes(path) {
  const nodes = [el('button', { onclick: () => enterDir('') }, '项目根')];
  if (!path) return nodes;
  const parts = path.split('/');
  let acc = '';
  parts.forEach((part, i) => {
    acc = acc ? `${acc}/${part}` : part;
    const target = acc;
    nodes.push(el('span', { text: ' / ' }));
    nodes.push(i === parts.length - 1
      ? el('span', { text: part })
      : el('button', { onclick: () => enterDir(target) }, part));
  });
  return nodes;
}

function enterDir(path) {
  filePath = path;
  loadFileList();
}

async function openFile(path) {
  const ref = currentFileRef();
  if (!ref) return;
  try {
    const f = await api(`/api/projects/${state.projectId}/file`
      + `?path=${encodeURIComponent(path)}&ref=${encodeURIComponent(ref)}`);
    $('#file-title').textContent = path;
    $('#file-meta').textContent = f.binary
      ? `${f.size} B · 二进制`
      : `${f.size} B${f.truncated ? ' · 已截断' : ''}`;
    // 一律 textContent —— 仓库内容不能当 HTML 解析
    $('#file-body').textContent = f.binary ? '（二进制文件，不显示内容）' : f.text;
    $('#queue-panel').hidden = true;
    $('#detail-panel').hidden = true;
    $('#file-panel').hidden = false;
  } catch (err) {
    toast(`打开失败：${err.message}`, true);
  }
}

function closeFile() {
  $('#file-panel').hidden = true;
  $('#queue-panel').hidden = false;
  if (state.task) $('#detail-panel').hidden = false;
}

/** 文件树开着就刷一下。

 批准/丢弃会推进主干，切换任务会换掉「本次任务」的版本 —— 这些都会让树上的内容
 变样，而树只在自己被点开时才拉数据，不刷就一直是旧的（批准完看不到新文件就是这么来的）。
 */
function refreshFilesIfVisible() {
  if (!$('#ctx-files').hidden) loadFileList();
}

// ---------------- 对话 ----------------

const FOLLOWABLE = ['awaiting_review', 'failed'];

async function loadChat() {
  const t = state.task;
  if (!t) return;
  let messages = [];
  let events = [];
  try {
    [messages, events] = await Promise.all([
      api(`/api/tasks/${t.id}/messages`),
      api(`/api/tasks/${t.id}/events`),
    ]);
  } catch (err) {
    toast(`加载对话失败：${err.message}`, true);
    return;
  }
  renderChat(t, messages, events);
}

function renderChat(t, messages, events) {
  const box = $('#chat');
  box.replaceChildren();

  // 每一轮跑完都会落一条 result 事件，按序对应第 1..N 轮。
  // 失败的那轮也有（subtype 不是 success），所以序号对得上。
  const replies = events.filter((e) => e.type === 'result').map((e) => e.payload || {});

  const turns = [{ text: t.body ? `${t.title}\n\n${t.body}` : t.title }];
  for (const m of messages) turns.push({ text: m.text });

  turns.forEach((turn, i) => {
    box.append(el('div', { class: 'chat-turn user' },
      el('div', { class: 'chat-role', text: `第 ${i + 1} 轮 · 你` }),
      el('div', { class: 'chat-text', text: turn.text })));

    const r = replies[i];
    if (!r) return;
    const failed = r.is_error || (r.subtype && r.subtype !== 'success');
    const text = r.result
      || (failed ? `（这一轮没跑成：${r.subtype || '执行失败'}）` : '（没有文字输出）');
    box.append(el('div', { class: `chat-turn ai${failed ? ' failed' : ''}` },
      el('div', { class: 'chat-role', text: 'AI' }),
      el('div', { class: 'chat-text', text })));
  });

  renderChatForm(t);
}

function renderChatForm(t) {
  const form = $('#chat-form');
  const hint = $('#chat-hint');
  const canFollow = FOLLOWABLE.includes(t.status);
  form.hidden = !canFollow;
  if (!hint || !canFollow) return;
  hint.textContent = t.status === 'awaiting_review'
    ? 'AI 会带着前面几轮的上下文接着做，改动累积在同一条分支上'
    : '带上失败原因让它再试一次';
}

async function sendFollowup(form) {
  const input = $('#chat-input');
  const text = input.value.trim();
  if (!text) { toast('先说点什么', true); return; }
  try {
    await api(`/api/tasks/${state.task.id}/messages`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
    input.value = '';
    toast('已入队 —— 接着上面那轮继续跑');
    await openTask(state.task.id, 'chat');
    await loadTasks();
  } catch (err) {
    toast(`继续失败：${err.message}`, true);
  }
}

// ---------------- WebSocket ----------------

function connectWs(taskId) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/api/ws/tasks/${taskId}`);
  state.ws = ws;

  ws.onopen = () => setConn(true);
  ws.onclose = () => {
    setConn(false);
    // 任务还在跑就自动重连，确保「随时能看到进展」
    if (state.task && state.task.id === taskId &&
        ['queued', 'running'].includes(state.task.status)) {
      setTimeout(() => { if (state.task && state.task.id === taskId) connectWs(taskId); }, 2000);
    }
  };
  ws.onerror = () => setConn(false);

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }

    if (msg.kind === 'snapshot' || msg.kind === 'status') {
      const prev = state.task ? state.task.status : null;
      state.task = msg.task;
      renderDetail();
      renderTasks();
      if (prev !== msg.task.status) {
        // 状态变了，对话里要么多出 AI 这一轮的回复，要么输入框重新可用
        loadChat();
        updateRefTabs();
        refreshFilesIfVisible();
        if (msg.task.status === 'awaiting_review') { loadDiff(); loadUsage(msg.task.id); }
        if (msg.task.status === 'merged' || msg.task.status === 'discarded') {
          $('#stream').append(el('div', { class: 'ev ev-init', text: `— 任务${STATUS_LABEL[msg.task.status]} —` }));
        }
      }
    } else if (msg.kind === 'event') {
      if (state.seen.has(msg.seq)) return;
      state.seen.add(msg.seq);
      renderEvent(msg);
    } else if (msg.kind === 'events') {
      // 合批推送，按 seq 排序后依次渲染
      const items = msg.events.filter((e) => e.seq === undefined || !state.seen.has(e.seq));
      items.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
      for (const e of items) {
        if (e.seq !== undefined) {
          if (state.seen.has(e.seq)) continue;
          state.seen.add(e.seq);
        }
        renderEvent(e);
      }
    } else if (msg.kind === 'error') {
      toast(msg.message, true);
    }
  };
}

function setConn(on) {
  const c = $('#conn');
  c.textContent = on ? '已连接' : '未连接';
  c.className = `conn ${on ? 'on' : 'off'}`;
}

// ---------------- 事件渲染 ----------------

function toolSummary(name, input) {
  if (!input || typeof input !== 'object') return '';
  if (input.command) return String(input.command).split('\n')[0];
  if (input.file_path) return input.file_path;
  if (input.pattern) return String(input.pattern);
  if (input.url) return String(input.url);
  const s = JSON.stringify(input);
  return s.length > 120 ? s.slice(0, 120) + '…' : s;
}

function toolCard(name, input) {
  const summary = toolSummary(name, input);
  const body = el('div', { class: 'tool-body', hidden: true, text: JSON.stringify(input, null, 2) });
  const head = el('div', { class: 'tool-head' },
    el('span', { class: 'tool-name', text: name }),
    el('span', { class: 'tool-summary', text: summary }),
  );
  head.addEventListener('click', () => { body.hidden = !body.hidden; });
  return el('div', { class: 'tool' }, head, body);
}

function resultBlock(payload) {
  const text = typeof payload.content === 'string'
    ? payload.content
    : JSON.stringify(payload.content, null, 2);
  const body = el('div', { class: 'tool-body', hidden: true, text: text ?? '' });
  const preview = (text || '').split('\n')[0].slice(0, 140);
  const head = el('div', { class: 'tool-head' },
    el('span', { class: `tool-name ${payload.is_error ? 'tool-result-err' : ''}`,
                 text: payload.is_error ? '结果（出错）' : '结果' }),
    el('span', { class: 'tool-summary', text: preview }),
  );
  head.addEventListener('click', () => { body.hidden = !body.hidden; });
  return el('div', { class: 'tool' }, head, body);
}

function renderEvent(msg) {
  const stream = $('#stream');
  const type = msg.type;
  const p = msg.payload || {};

  if (type === 'progress') {
    const box = $('#progress');
    box.hidden = false;
    box.textContent = `思考中… 已生成约 ${msg.thinking_tokens || 0} tokens`;
    return;
  }

  if (type === 'init') {
    $('#progress').hidden = true;
    stream.append(el('div', { class: 'ev ev-init' },
      `会话 ${(p.session_id || '').slice(0, 8)} · 模型 ${p.model || '?'} · ${shortSha(p.cwd)}`));
    return;
  }

  if (type === 'assistant') {
    const wrap = el('div', { class: 'ev' });
    if (p.text) wrap.append(el('div', { class: 'ev-text', text: p.text }));
    for (const tu of p.tool_uses || []) wrap.append(toolCard(tu.name, tu.input));
    if (wrap.childElementCount) stream.append(wrap);
    return;
  }

  if (type === 'tool_result') {
    stream.append(el('div', { class: 'ev' }, resultBlock(p)));
    return;
  }

  if (type === 'result') {
    $('#progress').hidden = true;
    const usage = p.usage || {};
    const cls = p.is_error ? 'ev ev-text ev-error' : 'ev ev-result';
    const wrap = el('div', { class: cls });
    if (p.result) wrap.append(el('div', { text: p.result }));
    wrap.append(el('div', { class: 'muted', text:
      `turns=${p.num_turns ?? '?'} · in=${usage.input_tokens ?? 0} ` +
      `out=${usage.output_tokens ?? 0} · cache_read=${usage.cache_read_input_tokens ?? 0}` }));
    stream.append(wrap);
    return;
  }
}

// ---------------- 初始化 ----------------

function init() {
  $('#project-form').addEventListener('submit', (e) => {
    e.preventDefault();
    createProject(e.target);
  });

  document.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      switchTab(btn.dataset.tab);
      if (state.task) writeHash(state.task.id, btn.dataset.tab);
    });
  });

  window.addEventListener('hashchange', () => {
    const route = parseHash();
    if (route && (!state.task || state.task.id !== route.taskId)) {
      openTask(route.taskId, route.tab);
    } else if (route && state.task) {
      switchTab(route.tab);
    }
  });

  // ---- 上下文面板 ----
  document.querySelectorAll('.ctab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.ctab').forEach((b) => b.classList.toggle('active', b === btn));
      const which = btn.dataset.ctab;
      $('#ctx-pointer').hidden = which !== 'pointer';
      $('#ctx-journal').hidden = which !== 'journal';
      $('#ctx-docs').hidden = which !== 'docs';
      $('#ctx-files').hidden = which !== 'files';
      if (which === 'files') loadFileList();
    });
  });

  // ---- 文件浏览 ----
  document.querySelectorAll('.rtab').forEach((btn) => {
    btn.addEventListener('click', () => {
      fileRefMode = btn.dataset.ref || '';
      updateRefTabs();
      enterDir('');
    });
  });
  $('#file-back').addEventListener('click', closeFile);

  // ---- 对话 ----
  $('#chat-form').addEventListener('submit', (e) => { e.preventDefault(); sendFollowup(e.target); });
  $('#pointer-save').addEventListener('click', savePointer);
  $('#doc-form').addEventListener('submit', (e) => { e.preventDefault(); createDoc(e.target); });
  $('#doc-save').addEventListener('click', saveDoc);
  $('#doc-cancel').addEventListener('click', showDocBrowse);
  $('#doc-delete').addEventListener('click', deleteDoc);

  // 指针图随输入更新字节数提示
  $('#pointer-text').addEventListener('input', () => {
    if (!ctxData) return;
    const bytes = new TextEncoder().encode($('#pointer-text').value).length;
    const tag = $('#ctx-bytes');
    tag.textContent = `${bytes} / ${ctxData.pointer_soft_limit} 字节`;
    tag.style.color = bytes > ctxData.pointer_soft_limit ? 'var(--del-fg)' : '';
  });

  // ---- 定时任务 ----
  $('#schedule-new').addEventListener('click', () => {
    const form = $('#schedule-form');
    form.hidden = !form.hidden;
    if (!form.hidden) {
      syncScheduleForm();
      form.querySelector('textarea').focus();
    }
  });
  $('#schedule-cancel').addEventListener('click', () => { $('#schedule-form').hidden = true; });
  $('#schedule-mode').addEventListener('change', syncScheduleForm);
  $('#schedule-form').addEventListener('submit', (e) => { e.preventDefault(); createSchedule(e.target); });

  // ---- 窗口状态 ----
  // 横幅上写着「还有 3 小时开启」，不刷新就成了假信息。
  // 顺带把定时任务也刷一遍 —— 批准之后「上一轮还没审」的提示要跟着消失。
  setInterval(async () => {
    try {
      state.window = await api('/api/window');
      renderWindow();
      renderTasks();
      await loadSchedules();
    } catch (_) { /* 断线了就别管，连接状态那边会提示 */ }
  }, 30000);

  // 任务提交表单挂在详情面板上方（动态建，避免 HTML 里重复一份）
  const form = el('form', { class: 'inline-form', id: 'task-form' },
    el('textarea', { name: 'title', rows: '2', placeholder: '描述要做的事，比如：给 stats.py 加一个 mode 函数', required: true }),
    el('div', { style: 'display:flex;gap:6px' },
      el('select', { name: 'kind' },
        el('option', { value: 'requirement' }, '需求'),
        el('option', { value: 'instruction' }, '指令'),
        el('option', { value: 'bug' }, '缺陷'),
        el('option', { value: 'task' }, '其他'),
      ),
      el('input', { name: 'priority', type: 'number', value: '0', title: '优先级，越大越先跑', style: 'width:80px' }),
      el('button', { type: 'submit' }, '提交任务'),
    ),
  );
  form.addEventListener('submit', (e) => { e.preventDefault(); submitTask(e.target); });
  $('#tasks').after(form);

  // 窗口状态要先拿到 —— 任务列表要靠它标出「等窗口」的那些
  loadWindow().catch(() => {}).then(() => loadProjects()).then(() => {
    const route = parseHash();
    if (route) {
      openTask(route.taskId, route.tab);
    } else if (state.projects.length) {
      // 首次进来直接选中第一个项目，免得看到空列表
      selectProject(state.projects[0].id);
    }
  });
}

document.addEventListener('DOMContentLoaded', init);
