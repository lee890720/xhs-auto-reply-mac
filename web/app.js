'use strict';

// 所有用户内容一律通过 textContent 写入，绝不拼接 HTML 字符串 —— 旧版控制台
// 直接把评论内容拼进 HTML，昵称里带标签就能注入脚本。

let state = { running: false, need_login: false, x: {} };
let providers = [];
let currentFilter = 'pending_review';
let currentPlatform = '';                 // 空串表示两个平台都看
let currentFollowerFilter = 'pending';
let xMaxWeight = 280;

const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  pending_review: '待审核', approved: '待发送', sent: '已发送',
  failed: '发送失败', rejected: '已拒绝', blocked: '敏感词拦截',
};
const ENGINE_LABEL = {
  stopped: '已停止', starting: '启动中', running: '监控中',
  quiet: '静默时段', need_login: '需要登录', error: '异常',
};
const X_ENGINE_LABEL = {
  stopped: '已停止', starting: '启动中', running: '监控中', quiet: '静默时段',
  auth_error: '认证失败', no_credit: '额度用完', rate_limited: '限流暂停', error: '异常',
};
const PLATFORM_LABEL = { xhs: '小红书', x: 'X' };
const FOLLOWER_LABEL = {
  pending: '待处理', approved: '排队回关', followed: '已回关',
  ignored: '已忽略', failed: '回关失败',
};

// X 的长度按加权字符算：拉丁字母、数字、常用标点算 1，中文和 emoji 算 2。
// 与 xhsd/x_api.py 的 weighted_length 保持一致。
const WEIGHT_1_RANGES = [[0, 4351], [8192, 8205], [8208, 8223], [8242, 8247]];
function weightedLength(text) {
  let n = 0;
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    n += WEIGHT_1_RANGES.some(([lo, hi]) => cp >= lo && cp <= hi) ? 1 : 2;
  }
  return n;
}

function externalLink(href, text, cls) {
  const a = document.createElement('a');
  a.href = href;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.textContent = text;
  if (cls) a.className = cls;
  return a;
}

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
  if (name === 'followers') loadFollowers();
  if (name === 'logs') loadLogs();
}

// ============ 引擎状态 ============

function setChip(prefix, cls, text, running) {
  $(prefix + '-dot').className = 'dot ' + cls;
  $(prefix + '-text').textContent = text;
  const btn = $(prefix + '-toggle');
  btn.textContent = running ? '停止' : '启动';
  btn.className = 'btn btn-sm ' + (running ? '' : 'btn-primary');
}

function renderStatus(s) {
  state = s;
  const x = s.x || {};

  // ---- 小红书 ----
  let cls = '', label = ENGINE_LABEL[s.state] || s.state;
  if (s.need_login) { cls = 'warn'; label = '需要扫码登录'; }
  else if (s.state === 'error') cls = 'err';
  else if (s.state === 'running') cls = 'on';
  else if (s.state === 'quiet' || s.state === 'starting') cls = 'warn';
  const parts = [label];
  if (s.running) {
    parts.push(`今日 ${s.today_sent}/${s.daily_limit}`);
    // 队列非空时明确显示，否则用户看不出后台还在按间隔逐条发
    if (s.approved_waiting) parts.push(`待发 ${s.approved_waiting}`);
  }
  setChip('xhs', cls, parts.join(' · '), s.running);

  // ---- X ----
  let xcls = '', xlabel = X_ENGINE_LABEL[x.state] || x.state || '–';
  if (x.state === 'running') xcls = 'on';
  else if (['auth_error', 'no_credit', 'error'].includes(x.state)) xcls = 'err';
  else if (['quiet', 'starting', 'rate_limited'].includes(x.state)) xcls = 'warn';
  if (!x.configured && !x.running) xlabel = '未配置';
  const xparts = [xlabel];
  if (x.running) {
    if (x.account) xparts.push('@' + x.account);
    xparts.push(`今日 ${x.today_sent}/${x.daily_limit}`);
    if (x.approved_waiting) xparts.push(`待发 ${x.approved_waiting}`);
    if (x.follow_queue) xparts.push(`待回关 ${x.follow_queue}`);
  }
  setChip('x', xcls, xparts.join(' · '), x.running);

  // ---- 横幅 ----
  $('login-banner').hidden = !s.need_login;
  $('setup-banner').hidden = s.llm_configured;
  const xNeedsHuman = x.state === 'auth_error' || x.state === 'no_credit';
  $('x-banner').hidden = !xNeedsHuman && !x.follow_notice;
  if (xNeedsHuman) {
    $('x-banner-title').textContent =
      x.state === 'auth_error' ? 'X 认证失败，引擎已停止' : 'X API 额度用完，已暂停';
    $('x-banner-text').textContent = x.last_error || '';
  } else if (x.follow_notice) {
    $('x-banner-title').textContent = 'X 回关受限';
    $('x-banner-text').textContent = x.follow_notice;
  }
  renderFollowersNotice(x);

  const badge = $('badge-pending');
  badge.textContent = s.pending_review_total;
  badge.hidden = !s.pending_review_total;
  const fbadge = $('badge-followers');
  fbadge.textContent = x.followers_pending || 0;
  fbadge.hidden = !x.followers_pending;

  // ---- 概览卡片：两个平台合计，提示里分开列 ----
  $('stat-today').textContent = (s.today_sent || 0) + (x.today_sent || 0);
  $('stat-pending').textContent = s.pending_review_total;
  $('stat-quota').textContent =
    `小红书 ${s.today_sent}/${s.daily_limit} · X ${x.today_sent || 0}/${x.daily_limit || 0}`;
}

async function toggleEngine(platform) {
  const running = platform === 'x' ? (state.x || {}).running : state.running;
  const action = running ? 'stop' : 'start';
  const base = platform === 'x' ? '/api/x/engine/' : '/api/engine/';
  const btn = $(platform + '-toggle');
  btn.disabled = true;
  try {
    const r = await post(base + action);
    toast(r.message, r.success ? 'ok' : 'err');
  } catch (e) {
    toast('操作失败: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
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
  catch (e) {
    $('xhs-text').textContent = '守护进程未响应';
    $('x-text').textContent = '守护进程未响应';
  }
}

// ============ 概览 ============

async function loadStats() {
  let s;
  try { s = await api('/api/stats'); } catch (e) { return; }

  $('stat-rate').textContent = s.success_rate + '%';
  $('stat-failed').textContent = s.total_failed ? `失败 ${s.total_failed} 条` : '暂无失败记录';
  $('stat-total').textContent = s.total_sent;
  $('stat-messages').textContent = `共采集 ${s.total_messages} 条评论和 @`;

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

function renderPlatformFilters(pending) {
  const bar = $('platform-filters');
  bar.textContent = '';
  for (const key of ['', 'xhs', 'x']) {
    const b = document.createElement('button');
    b.className = 'filter' + (key === currentPlatform ? ' active' : '');
    const n = key ? pending[key] : 0;
    b.textContent = (key ? PLATFORM_LABEL[key] : '全部平台') + (n ? ` · 待审 ${n}` : '');
    b.onclick = () => { currentPlatform = key; loadQueue(); };
    bar.appendChild(b);
  }
}

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

  const isX = r.platform === 'x';
  const plat = document.createElement('span');
  plat.className = 'plat plat-' + (isX ? 'x' : 'xhs');
  plat.textContent = PLATFORM_LABEL[r.platform] || '小红书';

  const author = document.createElement('span');
  author.className = 'item-author';
  if (isX && (r.author || '').startsWith('@')) {
    author.appendChild(externalLink('https://x.com/' + encodeURIComponent(r.author.slice(1)), r.author));
  } else {
    author.textContent = r.author || '未知用户';
  }

  const tag = document.createElement('span');
  tag.className = 'tag tag-' + r.status.replace('pending_review', 'pending');
  tag.textContent = STATUS_LABEL[r.status] || r.status;

  // 两个时间必须分别标注：raw_time 是评论在小红书上的发布时间，
  // 后面那个才是本系统的处理时间。并排放又不说明来源，会被读成
  //「这条卡了 31 分钟没动」。
  const meta = document.createElement('span');
  meta.className = 'item-meta';
  const clock = (ts) => (ts || '').slice(11, 19);
  const bits = [];
  if (r.raw_time) bits.push(`${isX ? '发布于' : '评论于'} ${r.raw_time}`);
  if (r.status === 'sent' && r.sent_at) bits.push(`已发送 ${clock(r.sent_at)}`);
  else if (r.status === 'approved') bits.push('排队中，按发送间隔依次发出');
  else if (r.created_at) bits.push(`采集于 ${clock(r.created_at)}`);
  meta.textContent = bits.join(' · ');

  head.append(plat, author, tag, meta);
  if (isX && r.external_id) {
    head.appendChild(externalLink(
      'https://x.com/i/web/status/' + encodeURIComponent(r.external_id), '查看原推 ↗', 'item-link'));
  }

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

  // 先显示笔记内容再显示评论 —— 审核时得先知道这条评论是在什么话题下说的，
  // 否则「约起来，我可以」这种评论根本判断不了回复对不对。
  // X 的上下文是对方回复（或引用）的那条推文：note_title 存作者，note_body 存正文
  let noteText, noteLabel = '笔记';
  if (isX) {
    noteText = r.note_body || '';
    const [relation, owner] = (r.note_type || '').split(':');
    const who = owner === 'own' ? '你的推文' : `${r.note_title || '他人'} 的推文`;
    noteLabel = relation === 'quoted' ? '引用了' + who : who;
  } else {
    noteText = [r.note_title, r.note_body].filter(Boolean).join(' — ');
  }
  if (noteText) {
    const note = document.createElement('div');
    note.className = 'note-context';
    const label = document.createElement('span');
    label.className = 'note-label';
    label.textContent = noteLabel;
    const text = document.createElement('span');
    text.textContent = noteText.length > 120 ? noteText.slice(0, 120) + '…' : noteText;
    note.append(label, text);
    main.appendChild(note);
  } else if (r.note_id) {
    const note = document.createElement('div');
    note.className = 'note-context note-context-missing';
    note.textContent = isX
      ? '它回复的那条推文没取到（可能已被删除），回复是在没有上下文的情况下生成的'
      : '这篇笔记没取到文字内容，回复是在没有上下文的情况下生成的';
    main.appendChild(note);
  }

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

  // X 有 280 加权字符的上限，审核时改长了要当场看得见，别等发送失败
  if (isX) {
    const counter = document.createElement('div');
    counter.className = 'char-count';
    const update = () => {
      const n = weightedLength(draft.value);
      counter.textContent = `${n}/${xMaxWeight}（中文一个字算 2）`;
      counter.classList.toggle('over', n > xMaxWeight);
    };
    draft.addEventListener('input', update);
    update();
    main.appendChild(counter);
  }

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
  const platformQuery = currentPlatform ? '&platform=' + currentPlatform : '';
  try { data = await api('/api/replies?status=' + currentFilter + '&limit=100' + platformQuery); }
  catch (e) { toast('加载失败: ' + e.message, 'err'); return; }

  renderPlatformFilters(data.platform_pending || {});
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
  const scope = currentPlatform ? PLATFORM_LABEL[currentPlatform] + '的' : '所有';
  if (!confirm(`确定批准${scope}待审回复？它们会按发送间隔依次发出。`)) return;
  try {
    const r = await post('/api/replies/approve_all' + (currentPlatform ? '?platform=' + currentPlatform : ''));
    toast(r.message, 'ok');
    loadQueue();
  } catch (e) { toast(e.message, 'err'); }
}

// ============ X 新关注者 ============
// 每个回关都必须是人对一个具体账号点的按钮，这里刻意没有批量操作。

function renderFollowersNotice(x) {
  const el = $('followers-notice');
  if (x.auto_follow_back) {
    el.className = 'notice notice-warn';
    el.textContent = `自动回关已开启（违反 X 开发者指南，有风险）：没有疑似机器人标记的新关注者会被自动回关，`
      + `今日已自动回关 ${x.auto_followed_today || 0}/${x.auto_follow_daily_limit}，两次之间间隔 2–5 分钟。`
      + `带标记的和超出上限的留在下面「待处理」里由你决定。`;
  } else {
    el.className = 'notice';
    el.textContent = '这里列出新关注你的人，回不回关由你逐个决定；点了「回关」的会按设置里的间隔依次执行，并受每日上限约束。';
  }
}

function renderFollowerFilters(counts) {
  const bar = $('follower-filters');
  bar.textContent = '';
  for (const key of ['pending', 'approved', 'followed', 'ignored', 'failed']) {
    const b = document.createElement('button');
    b.className = 'filter' + (key === currentFollowerFilter ? ' active' : '');
    b.textContent = FOLLOWER_LABEL[key] + (counts[key] ? ` ${counts[key]}` : '');
    b.onclick = () => { currentFollowerFilter = key; loadFollowers(); };
    bar.appendChild(b);
  }
}

function buildFollower(f) {
  const item = document.createElement('div');
  item.className = 'item';
  const row = document.createElement('div');
  row.className = 'follower';

  const img = document.createElement('img');
  img.className = 'avatar';
  img.referrerPolicy = 'no-referrer';
  img.alt = '';
  img.loading = 'lazy';
  if (f.profile_image_url) img.src = f.profile_image_url;
  img.onerror = () => { img.removeAttribute('src'); };
  row.appendChild(img);

  const main = document.createElement('div');
  main.className = 'item-main';

  const head = document.createElement('div');
  head.className = 'item-head';
  head.style.marginBottom = '0';
  const name = document.createElement('span');
  name.className = 'follower-name';
  name.appendChild(externalLink('https://x.com/' + encodeURIComponent(f.username), f.name || f.username));
  const handle = document.createElement('span');
  handle.className = 'follower-handle';
  handle.textContent = '@' + f.username;
  const tag = document.createElement('span');
  const tagClass = { pending: 'pending', approved: 'approved', followed: 'sent', ignored: 'ignored', failed: 'failed' };
  tag.className = 'tag tag-' + (tagClass[f.status] || 'rejected');
  tag.textContent = FOLLOWER_LABEL[f.status] || f.status;
  const meta = document.createElement('span');
  meta.className = 'item-meta';
  meta.textContent = '发现于 ' + (f.first_seen_at || '').replace('T', ' ').slice(0, 16)
    + (f.status === 'approved' ? ' · 排队中，按间隔依次执行' : '');
  head.append(name, handle, tag);
  if (f.approved_by === 'auto') {
    const auto = document.createElement('span');
    auto.className = 'tag tag-approved';
    auto.textContent = '自动回关';
    head.appendChild(auto);
  }
  if (f.protected) {
    const lock = document.createElement('span');
    lock.className = 'tag tag-rejected';
    lock.textContent = '受保护账号';
    head.appendChild(lock);
  }
  head.appendChild(meta);
  main.appendChild(head);

  const stats = document.createElement('div');
  stats.className = 'follower-stats';
  const since = (f.account_created_at || '').slice(0, 7);
  stats.textContent = `粉丝 ${f.followers_count} · 关注 ${f.following_count} · 发帖 ${f.tweet_count}`
    + (since ? ` · 注册于 ${since}` : '');
  main.appendChild(stats);

  if (f.description) {
    const bio = document.createElement('div');
    bio.className = 'follower-bio';
    bio.textContent = f.description;
    main.appendChild(bio);
  }

  if (f.flags) {
    const flags = document.createElement('div');
    flags.className = 'flags';
    for (const fl of f.flags.split(',').filter(Boolean)) {
      const el = document.createElement('span');
      el.className = 'flag';
      el.textContent = fl;
      flags.appendChild(el);
    }
    main.appendChild(flags);
  }

  if (f.error) {
    const err = document.createElement('div');
    err.className = f.status === 'followed' ? 'note-context' : 'item-error';
    err.textContent = f.error;
    main.appendChild(err);
  }

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
  if (['pending', 'ignored', 'failed'].includes(f.status)) {
    addBtn('回关', 'btn-ok', async () => {
      const r = await post(`/api/x/followers/${encodeURIComponent(f.user_id)}/follow`);
      toast(r.message, 'ok');
      loadFollowers();
    });
  }
  if (f.status === 'pending') {
    addBtn('忽略', '', async () => {
      await post(`/api/x/followers/${encodeURIComponent(f.user_id)}/ignore`);
      toast('已忽略');
      loadFollowers();
    });
  }
  if (actions.children.length) main.appendChild(actions);

  row.appendChild(main);
  item.appendChild(row);
  return item;
}

async function loadFollowers() {
  const box = $('followers');
  let data;
  try { data = await api('/api/x/followers?status=' + currentFollowerFilter + '&limit=100'); }
  catch (e) { toast('加载失败: ' + e.message, 'err'); return; }

  renderFollowerFilters(data.counts || {});
  box.textContent = '';
  if (!data.items.length) {
    const p = document.createElement('div');
    p.className = 'empty';
    p.textContent = currentFollowerFilter === 'pending'
      ? ((state.x || {}).running ? '没有待处理的新关注者' : '没有待处理的新关注者（X 引擎没有运行）')
      : '这个分类下暂无记录';
    box.appendChild(p);
    return;
  }
  for (const f of data.items) box.appendChild(buildFollower(f));
}

async function checkFollowers() {
  try {
    const r = await post('/api/x/followers/check');
    toast(r.message, r.success ? 'ok' : 'err');
    if (r.success) setTimeout(loadFollowers, 4000);
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
  set('safety-forbidden_in_reply', (cfg.safety.forbidden_in_reply || []).join('\n'));

  const x = cfg.x;
  for (const k of ['api_key', 'api_secret', 'access_token', 'access_token_secret',
                   'mode', 'poll_interval', 'daily_limit', 'per_user_daily_limit',
                   'max_per_poll', 'max_mentions_in_tweet', 'min_delay', 'max_delay',
                   'quiet_start', 'quiet_end', 'followers_poll_interval',
                   'followers_page_size', 'follow_daily_limit', 'follow_min_delay',
                   'follow_max_delay', 'system_prompt']) {
    set('x-' + k, x[k]);
  }
  $('x-ai_reply_approved').checked = x.ai_reply_approved;
  $('x-quiet_enabled').checked = x.quiet_enabled;
  $('x-followers_enabled').checked = x.followers_enabled;
  $('x-auto_follow_back').checked = x.auto_follow_back;
  set('x-auto_follow_daily_limit', x.auto_follow_daily_limit);
}

// 打开自动回关前再确认一次风险。只影响勾选框，真正生效要点「保存」
function confirmAutoFollow(box) {
  if (box.checked && !confirm(
    '自动回关违反 X 开发者指南，可能导致关注功能被限制、App 被停用乃至封号。\n\n确定要开启吗？（开启后还需点「保存」）')) {
    box.checked = false;
  }
}

function onProviderChange() {
  const p = providers.find(x => x.key === $('llm-provider').value);
  if (!p) return;

  $('provider-note').textContent = p.note || '';
  $('llm-base_url').placeholder = p.base_url || '必填 —— 请填写服务商提供的 OpenAI 兼容端点';

  // 模型名完全由用户填写：不预填、不给候选列表、不静默回退。
  // 各家模型更新频繁且取决于账号开通情况，任何内置清单都会过时。
  const hint = $('model-hint');
  hint.textContent = '填写你账号实际开通的模型名。';
  if (p.console_url) {
    hint.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = p.console_url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = '去控制台查看 →';
    hint.appendChild(a);
  }

  const keyHint = $('key-hint');
  keyHint.textContent = 'Key 只保存在本机数据库，不会上传任何地方。';
  if (p.console_url) {
    keyHint.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = p.console_url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = '前往申请 →';
    keyHint.appendChild(a);
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
  const num = (id) => parseFloat($(id).value);
  const int = (id) => parseInt($(id).value, 10);
  if (name === 'x-account') return {
    api_key: $('x-api_key').value.trim(),
    api_secret: $('x-api_secret').value.trim(),
    access_token: $('x-access_token').value.trim(),
    access_token_secret: $('x-access_token_secret').value.trim(),
  };
  if (name === 'x-reply') return {
    mode: $('x-mode').value,
    ai_reply_approved: $('x-ai_reply_approved').checked,
    poll_interval: int('x-poll_interval'),
    daily_limit: int('x-daily_limit'),
    per_user_daily_limit: int('x-per_user_daily_limit'),
    max_per_poll: int('x-max_per_poll'),
    max_mentions_in_tweet: int('x-max_mentions_in_tweet'),
    min_delay: num('x-min_delay'),
    max_delay: num('x-max_delay'),
    quiet_enabled: $('x-quiet_enabled').checked,
    quiet_start: $('x-quiet_start').value.trim(),
    quiet_end: $('x-quiet_end').value.trim(),
  };
  if (name === 'x-followers') return {
    followers_enabled: $('x-followers_enabled').checked,
    followers_poll_interval: int('x-followers_poll_interval'),
    followers_page_size: int('x-followers_page_size'),
    follow_daily_limit: int('x-follow_daily_limit'),
    follow_min_delay: num('x-follow_min_delay'),
    follow_max_delay: num('x-follow_max_delay'),
    auto_follow_back: $('x-auto_follow_back').checked,
    auto_follow_daily_limit: int('x-auto_follow_daily_limit'),
  };
  if (name === 'x-persona') return { system_prompt: $('x-system_prompt').value };
  if (name === 'safety') return {
    sensitive_keywords: $('safety-sensitive_keywords').value
      .split('\n').map(s => s.trim()).filter(Boolean),
    on_sensitive: $('safety-on_sensitive').value,
    fallback_text: $('safety-fallback_text').value.trim(),
    forbidden_in_reply: $('safety-forbidden_in_reply').value
      .split('\n').map(s => s.trim()).filter(Boolean),
  };
  return {};
}

async function saveSection(name) {
  const msg = $('msg-' + name);
  msg.textContent = '保存中…';
  msg.className = 'form-msg';
  try {
    // X 的几组设置在后端是同一个配置分组，分开保存只是为了页面好读
    const section = name.startsWith('x-') ? 'x' : name;
    const r = await put('/api/config/' + section, { values: collectSection(name) });
    msg.textContent = '已保存，立即生效';
    msg.className = 'form-msg ok';
    if (name === 'llm' || name === 'x-account') fillForm(r.config);
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

async function testX() {
  const msg = $('msg-x-account');
  msg.textContent = '正在测试…';
  msg.className = 'form-msg';
  try {
    const r = await post('/api/x/test');
    msg.textContent = r.message;
    msg.className = 'form-msg ' + (r.success ? 'ok' : 'err');
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'form-msg err';
  }
}

async function saveAutostart(platform) {
  const isX = platform === 'x';
  const box = $(isX ? 'x-autostart' : 'autostart');
  try {
    await put(isX ? '/api/x/autostart' : '/api/autostart', { values: { enabled: box.checked } });
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
    $('x-autostart').checked = data.x_autostart;
    xMaxWeight = data.x_max_weight || 280;
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
      const wasPending = state.pending_review_total;
      const wasFollowers = (state.x || {}).followers_pending;
      renderStatus(s);
      // 待审数变化时自动刷新队列，不用手动点
      const active = document.querySelector('.panel.active').id;
      if (s.pending_review_total !== wasPending && active === 'panel-review') loadQueue();
      if ((s.x || {}).followers_pending !== wasFollowers && active === 'panel-followers') loadFollowers();
    } catch (e) { /* 忽略单条解析失败 */ }
  };
  es.onerror = () => {
    for (const p of ['xhs', 'x']) {
      $(p + '-dot').className = 'dot err';
      $(p + '-text').textContent = '与守护进程断开';
    }
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
