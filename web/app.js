'use strict';

// 所有用户内容一律通过 textContent 写入，绝不拼接 HTML 字符串 —— 旧版控制台
// 直接把评论内容拼进 HTML，昵称里带标签就能注入脚本。

let state = { running: false, need_login: false };
let providers = [];
let currentFilter = 'pending_review';

const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  pending_review: '待审核', approved: '待发送', sent: '已发送',
  failed: '发送失败', rejected: '已拒绝', blocked: '敏感词拦截',
};
const ENGINE_LABEL = {
  stopped: '已停止', starting: '启动中', running: '监控中',
  quiet: '静默时段', need_login: '需要登录', error: '异常',
};

function toast(msg, kind) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast show' + (kind ? ' ' + kind : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 3200);
}

async function api(path, options) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = 'HTTP ' + resp.status;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* 保留默认 */ }
    throw new Error(detail);
  }
  return resp.json();
}

const post = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

const put = (path, body) => api(path, {
  method: 'PUT',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

// ============ 导航 ============

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p =>
    p.classList.toggle('active', p.id === 'panel-' + name));

  if (name === 'overview') loadStats();
  if (name === 'review') loadQueue();
  if (name === 'logs') loadLogs();
}

// ============ 引擎状态 ============

function renderStatus(s) {
  state = s;

  const dot = $('status-dot');
  const text = $('status-text');
  let cls = '', label = ENGINE_LABEL[s.state] || s.state;

  if (s.need_login) { cls = 'warn'; label = '需要扫码登录'; }
  else if (s.state === 'error') cls = 'err';
  else if (s.state === 'running') cls = 'on';
  else if (s.state === 'quiet' || s.state === 'starting') cls = 'warn';

  dot.className = 'dot ' + cls;
  text.textContent = label + (s.running ? ` · 今日 ${s.today_sent}/${s.daily_limit}` : '');

  const btn = $('btn-toggle');
  btn.textContent = s.running ? '停止' : '启动';
  btn.className = 'btn ' + (s.running ? '' : 'btn-primary');

  $('login-banner').hidden = !s.need_login;
  $('setup-banner').hidden = s.llm_configured;

  const badge = $('badge-pending');
  badge.textContent = s.pending_review;
  badge.hidden = s.pending_review === 0;

  $('stat-today').textContent = s.today_sent;
  $('stat-pending').textContent = s.pending_review;
  $('stat-quota').textContent =
    `上限 ${s.daily_limit} · 剩余 ${Math.max(0, s.daily_limit - s.today_sent)}`;
}

async function toggleEngine() {
  const action = state.running ? 'stop' : 'start';
  $('btn-toggle').disabled = true;
  try {
    const r = await post('/api/engine/' + action);
    toast(r.message, r.success ? 'ok' : 'err');
  } catch (e) {
    toast('操作失败: ' + e.message, 'err');
  } finally {
    $('btn-toggle').disabled = false;
    refreshStatus();
  }
}

async function openLogin() {
  try { toast((await post('/api/login/open')).message); }
  catch (e) { toast(e.message, 'err'); }
}

async function recheckLogin() {
  try {
    const r = await post('/api/login/recheck');
    toast(r.message, r.success ? 'ok' : 'err');
    refreshStatus();
  } catch (e) { toast(e.message, 'err'); }
}

async function refreshStatus() {
  try { renderStatus(await api('/api/status')); }
  catch (e) { $('status-text').textContent = '守护进程未响应'; }
}

// ============ 概览 ============

async function loadStats() {
  let s;
  try { s = await api('/api/stats'); } catch (e) { return; }

  $('stat-rate').textContent = s.success_rate + '%';
  $('stat-failed').textContent = s.total_failed ? `失败 ${s.total_failed} 条` : '暂无失败记录';
  $('stat-total').textContent = s.total_sent;
  $('stat-messages').textContent = `共采集 ${s.total_messages} 条评论`;

  const chart = $('chart');
  chart.textContent = '';
  const max = Math.max(1, ...s.trend.map(d => d.count));
  for (const d of s.trend) {
    const col = document.createElement('div');
    col.className = 'bar-col';

    const num = document.createElement('div');
    num.className = 'bar-num';
    num.textContent = d.count || '';

    const bar = document.createElement('div');
    bar.className = 'bar' + (d.is_today ? ' today' : '');
    bar.style.height = Math.max(3, (d.count / max) * 100) + '%';
    bar.title = `${d.date}  ${d.count} 条`;

    const day = document.createElement('div');
    day.className = 'bar-day';
    day.textContent = d.label;

    col.append(num, bar, day);
    chart.appendChild(col);
  }

  const list = $('author-list');
  list.textContent = '';
  if (!s.top_authors.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = '还没有回复记录';
    list.appendChild(p);
    return;
  }
  const top = s.top_authors[0].n;
  for (const a of s.top_authors) {
    const row = document.createElement('div');
    row.className = 'author-row';

    const name = document.createElement('span');
    name.className = 'author-name';
    name.textContent = a.author || '未知';
    name.title = a.author || '';

    const bar = document.createElement('div');
    bar.className = 'author-bar';
    bar.style.width = Math.max(3, (a.n / top) * 62) + '%';

    const num = document.createElement('span');
    num.className = 'author-num';
    num.textContent = a.n;

    row.append(name, bar, num);
    list.appendChild(row);
  }
}

// ============ 审核队列 ============

function renderFilters(counts) {
  const bar = $('filters');
  bar.textContent = '';
  const order = ['pending_review', 'approved', 'sent', 'failed', 'blocked', 'rejected'];
  for (const key of order) {
    const b = document.createElement('button');
    b.className = 'filter' + (key === currentFilter ? ' active' : '');
    b.textContent = STATUS_LABEL[key] + (counts[key] ? ` ${counts[key]}` : '');
    b.onclick = () => { currentFilter = key; loadQueue(); };
    bar.appendChild(b);
  }
}

function buildItem(r) {
  const item = document.createElement('div');
  item.className = 'item';

  // 头部
  const head = document.createElement('div');
  head.className = 'item-head';

  const author = document.createElement('span');
  author.className = 'item-author';
  author.textContent = r.author || '未知用户';

  const tag = document.createElement('span');
  tag.className = 'tag tag-' + r.status.replace('pending_review', 'pending');
  tag.textContent = STATUS_LABEL[r.status] || r.status;

  const meta = document.createElement('span');
  meta.className = 'item-meta';
  const when = r.sent_at || r.reviewed_at || r.created_at || '';
  meta.textContent = [r.raw_time, when.replace('T', ' ')].filter(Boolean).join(' · ');

  head.append(author, tag, meta);

  // 正文
  const body = document.createElement('div');
  body.className = 'item-body';

  if (r.note_image_url) {
    const img = document.createElement('img');
    img.className = 'item-thumb';
    // 小红书 CDN 会校验 Referer，带上本地地址会被拒，图片显示成破图
    img.referrerPolicy = 'no-referrer';
    img.src = r.note_image_url;          // 作为 src 属性赋值，不参与 HTML 解析
    img.loading = 'lazy';
    img.alt = '';                        // 加载失败时不显示 alt 文字占位
    img.title = '点击查看大图';
    img.onerror = () => img.remove();    // 仍然失败就移除，别留个破图框
    img.onclick = () => window.open(r.note_image_url, '_blank', 'noopener');
    body.appendChild(img);
  }

  const main = document.createElement('div');
  main.className = 'item-main';

  const quote = document.createElement('div');
  quote.className = 'quote';
  quote.textContent = r.content || '（无内容）';
  main.appendChild(quote);

  const editable = r.status === 'pending_review' || r.status === 'blocked';
  const draft = document.createElement('textarea');
  draft.className = 'draft';
  draft.value = r.final_text || r.draft || '';
  draft.readOnly = !editable;
  draft.placeholder = r.status === 'blocked'
    ? '这条命中了敏感词，没有自动生成回复。你可以手写一条，或直接拒绝。'
    : '回复内容';
  main.appendChild(draft);

  if (r.error) {
    const err = document.createElement('div');
    err.className = 'item-error';
    err.textContent = r.error;
    main.appendChild(err);
  }

  // 操作
  const actions = document.createElement('div');
  actions.className = 'item-actions';

  const addBtn = (label, cls, handler) => {
    const b = document.createElement('button');
    b.className = 'btn btn-sm ' + cls;
    b.textContent = label;
    b.onclick = async () => {
      b.disabled = true;
      try { await handler(); }
      catch (e) { toast(e.message, 'err'); }
      finally { b.disabled = false; }
    };
    actions.appendChild(b);
  };

  if (editable) {
    addBtn('批准并发送', 'btn-ok', async () => {
      const r2 = await post(`/api/replies/${r.id}/approve`, { text: draft.value });
      toast(r2.message, 'ok');
      loadQueue();
    });
    addBtn('重新生成', '', async () => {
      const r2 = await post(`/api/replies/${r.id}/regenerate`);
      if (r2.success) { draft.value = r2.message; toast('已重新生成', 'ok'); }
      else toast(r2.message, 'err');
    });
    addBtn('拒绝', '', async () => {
      await post(`/api/replies/${r.id}/reject`);
      toast('已拒绝');
      loadQueue();
    });
  } else if (r.status === 'failed') {
    addBtn('重新排队发送', 'btn-primary', async () => {
      await post(`/api/replies/${r.id}/approve`, { text: draft.value });
      toast('已重新排队', 'ok');
      loadQueue();
    });
  }

  main.appendChild(actions);
  body.appendChild(main);
  item.append(head, body);
  return item;
}

async function loadQueue() {
  const box = $('queue');
  let data;
  try { data = await api('/api/replies?status=' + currentFilter + '&limit=100'); }
  catch (e) { toast('加载失败: ' + e.message, 'err'); return; }

  renderFilters(data.counts);
  $('btn-approve-all').hidden = currentFilter !== 'pending_review' || !data.items.length;

  box.textContent = '';
  if (!data.items.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = currentFilter === 'pending_review'
      ? '没有待审核的回复' : '这个分类下暂无记录';
    box.appendChild(p);
    return;
  }
  for (const r of data.items) box.appendChild(buildItem(r));
}

async function approveAll() {
  if (!confirm('确定批准所有待审回复？它们会按发送间隔依次发出。')) return;
  try {
    const r = await post('/api/replies/approve_all');
    toast(r.message, 'ok');
    loadQueue();
  } catch (e) { toast(e.message, 'err'); }
}

// ============ 设置 ============

function fillForm(cfg) {
  const set = (id, v) => { const el = $(id); if (el) el.value = v; };

  const sel = $('llm-provider');
  if (!sel.options.length) {
    for (const p of providers) {
      const o = document.createElement('option');
      o.value = p.key;
      o.textContent = p.label;
      sel.appendChild(o);
    }
  }
  sel.value = cfg.llm.provider;
  set('llm-api_key', cfg.llm.api_key);
  set('llm-base_url', cfg.llm.base_url);
  set('llm-model', cfg.llm.model);
  set('llm-temperature', cfg.llm.temperature);
  set('llm-max_tokens', cfg.llm.max_tokens);
  set('llm-timeout', cfg.llm.timeout);
  onProviderChange();

  set('persona-system_prompt', cfg.persona.system_prompt);

  set('reply-mode', cfg.reply.mode);
  set('reply-poll_interval', cfg.reply.poll_interval);
  set('reply-daily_limit', cfg.reply.daily_limit);
  set('reply-max_per_poll', cfg.reply.max_per_poll);
  set('reply-min_delay', cfg.reply.min_delay);
  set('reply-max_delay', cfg.reply.max_delay);
  $('reply-quiet_enabled').checked = cfg.reply.quiet_enabled;
  set('reply-quiet_start', cfg.reply.quiet_start);
  set('reply-quiet_end', cfg.reply.quiet_end);

  set('safety-sensitive_keywords', cfg.safety.sensitive_keywords.join('\n'));
  set('safety-on_sensitive', cfg.safety.on_sensitive);
  set('safety-fallback_text', cfg.safety.fallback_text);
}

function onProviderChange() {
  const p = providers.find(x => x.key === $('llm-provider').value);
  if (!p) return;

  $('provider-note').textContent = p.note || '';
  $('llm-base_url').placeholder = p.base_url || '必填 —— 请填写服务商提供的 OpenAI 兼容端点';
  $('llm-model').placeholder = p.default_model || '必填 —— 请填写模型名';

  const dl = $('model-options');
  dl.textContent = '';
  for (const m of p.models) {
    const o = document.createElement('option');
    o.value = m;
    dl.appendChild(o);
  }

  const hint = $('key-hint');
  hint.textContent = 'Key 只保存在本机数据库，不会上传任何地方。';
  if (p.console_url) {
    hint.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = p.console_url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = '前往申请 →';
    hint.appendChild(a);
  }
}

function collectSection(name) {
  if (name === 'llm') return {
    provider: $('llm-provider').value,
    api_key: $('llm-api_key').value.trim(),
    base_url: $('llm-base_url').value.trim(),
    model: $('llm-model').value.trim(),
    temperature: parseFloat($('llm-temperature').value),
    max_tokens: parseInt($('llm-max_tokens').value, 10),
    timeout: parseInt($('llm-timeout').value, 10),
  };
  if (name === 'persona') return { system_prompt: $('persona-system_prompt').value };
  if (name === 'reply') return {
    mode: $('reply-mode').value,
    poll_interval: parseInt($('reply-poll_interval').value, 10),
    daily_limit: parseInt($('reply-daily_limit').value, 10),
    max_per_poll: parseInt($('reply-max_per_poll').value, 10),
    min_delay: parseFloat($('reply-min_delay').value),
    max_delay: parseFloat($('reply-max_delay').value),
    quiet_enabled: $('reply-quiet_enabled').checked,
    quiet_start: $('reply-quiet_start').value.trim(),
    quiet_end: $('reply-quiet_end').value.trim(),
  };
  if (name === 'safety') return {
    sensitive_keywords: $('safety-sensitive_keywords').value
      .split('\n').map(s => s.trim()).filter(Boolean),
    on_sensitive: $('safety-on_sensitive').value,
    fallback_text: $('safety-fallback_text').value.trim(),
  };
  return {};
}

async function saveSection(name) {
  const msg = $('msg-' + name);
  msg.textContent = '保存中…';
  msg.className = 'form-msg';
  try {
    const r = await put('/api/config/' + name, { values: collectSection(name) });
    msg.textContent = '已保存，立即生效';
    msg.className = 'form-msg ok';
    if (name === 'llm') fillForm(r.config);
    refreshStatus();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'form-msg err';
  }
}

async function testLLM() {
  const msg = $('msg-llm');
  msg.textContent = '正在测试…';
  msg.className = 'form-msg';
  try {
    const r = await post('/api/llm/test');
    msg.textContent = r.message;
    msg.className = 'form-msg ' + (r.success ? 'ok' : 'err');
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'form-msg err';
  }
}

async function saveAutostart() {
  try {
    await put('/api/autostart', { values: { enabled: $('autostart').checked } });
    toast('已保存', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function cleanup() {
  if (!confirm('将永久删除 90 天前的评论与回复记录，确定继续？')) return;
  try {
    const r = await post('/api/maintenance/cleanup?days=90');
    $('msg-system').textContent = r.message;
    $('msg-system').className = 'form-msg ok';
  } catch (e) { toast(e.message, 'err'); }
}

async function loadConfig() {
  try {
    const data = await api('/api/config');
    providers = data.providers;
    $('autostart').checked = data.autostart;
    fillForm(data.config);
  } catch (e) { toast('配置加载失败: ' + e.message, 'err'); }
}

// ============ 日志 ============

async function loadLogs() {
  let data;
  try { data = await api('/api/events?limit=300'); } catch (e) { return; }

  const box = $('logs');
  box.textContent = '';
  if (!data.items.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = '暂无日志';
    box.appendChild(p);
    return;
  }
  for (const e of data.items) {
    const line = document.createElement('div');
    line.className = 'log-line';

    const t = document.createElement('span');
    t.className = 'log-time';
    t.textContent = (e.created_at || '').slice(11, 19);

    const lv = document.createElement('span');
    lv.className = 'log-level lv-' + e.level;
    lv.textContent = e.level;

    const src = document.createElement('span');
    src.className = 'log-src';
    src.textContent = e.source;

    const m = document.createElement('span');
    m.textContent = e.message;

    line.append(t, lv, src, m);
    box.appendChild(line);
  }
}

// ============ 启动 ============

function connectStream() {
  const es = new EventSource('/api/stream');
  es.onmessage = (ev) => {
    try {
      const s = JSON.parse(ev.data);
      const wasPending = state.pending_review;
      renderStatus(s);
      // 待审数变化时自动刷新队列，不用手动点
      if (s.pending_review !== wasPending &&
          document.querySelector('.panel.active').id === 'panel-review') {
        loadQueue();
      }
    } catch (e) { /* 忽略单条解析失败 */ }
  };
  es.onerror = () => {
    $('status-dot').className = 'dot err';
    $('status-text').textContent = '与守护进程断开';
    es.close();
    setTimeout(connectStream, 5000);
  };
}

refreshStatus();
loadConfig();
loadStats();
connectStream();
setInterval(() => {
  const active = document.querySelector('.panel.active').id;
  if (active === 'panel-overview') loadStats();
  if (active === 'panel-logs') loadLogs();
}, 15000);
