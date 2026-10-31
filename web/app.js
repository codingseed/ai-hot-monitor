const $ = (selector) => document.querySelector(selector);
const toast = $('#toast');
let toastTimer;
let dashboard = null;
let realtimeSocket = null;
let reconnectTimer = null;

function setRealtimeStatus(connected) {
  const status = $('#realtime-status');
  status.className = `status-pill ${connected ? 'status-on' : 'status-off'}`;
  status.textContent = connected ? '实时已连接' : '实时重连中';
}

function connectRealtime() {
  clearTimeout(reconnectTimer);
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  realtimeSocket = new WebSocket(`${scheme}://${window.location.hostname}:8766`);
  realtimeSocket.addEventListener('open', () => setRealtimeStatus(true));
  realtimeSocket.addEventListener('close', () => {
    setRealtimeStatus(false);
    reconnectTimer = setTimeout(connectRealtime, 4000);
  });
  realtimeSocket.addEventListener('error', () => realtimeSocket.close());
  realtimeSocket.addEventListener('message', async (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    if (payload.type !== 'alerted_story' && payload.type !== 'verified_story') return;
    await refresh();
    const story = payload.story || {};
    const urgencyLabel = { urgent: '紧急', high: '高', medium: '中', low: '低' }[story.urgency] || '高';
    if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      new Notification(`AI 热点提醒（${urgencyLabel}）`, { body: story.title || '发现一条新热点' });
    } else {
      showToast(`新热点提醒（${urgencyLabel}）：${story.title || ''}`);
    }
  });
}

function showToast(message, isError = false) {
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('visible'), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function formatTime(value) {
  if (!value) return '尚未扫描';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const delta = Math.max(0, Date.now() - date.getTime());
  if (delta < 60_000) return '刚刚';
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)} 分钟前`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
}

function setText(selector, value) {
  $(selector).textContent = value;
}

function renderKeywords(keywords) {
  const list = $('#keyword-list');
  list.replaceChildren();
  setText('#keyword-count', String(keywords.length));
  setText('#nav-count', String(keywords.length));
  setText('#metric-keywords', String(keywords.length));
  if (!keywords.length) {
    const empty = document.createElement('span');
    empty.className = 'muted-copy';
    empty.textContent = '暂时还没有关注词';
    list.append(empty);
    return;
  }
  for (const keyword of keywords) {
    const chip = document.createElement('span');
    chip.className = 'keyword-chip';
    const label = document.createElement('span');
    label.textContent = keyword.phrase;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.title = `移除 ${keyword.phrase}`;
    remove.setAttribute('aria-label', `移除关键词 ${keyword.phrase}`);
    remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';
    remove.addEventListener('click', async () => {
      try {
        dashboard = await api(`/api/keywords/${keyword.id}`, { method: 'DELETE' });
        render(dashboard);
        showToast(`已移除「${keyword.phrase}」`);
      } catch (error) {
        showToast(error.message, true);
      }
    });
    chip.append(label, remove);
    list.append(chip);
  }
}

const truthLabels = { credible: '可信', suspicious: '存疑', uncertain: '待核验', unverified: '未核验', pending: '未核验' };
const verdictLabels = truthLabels;
const urgencyLabels = { urgent: '紧急', high: '高', medium: '中', low: '低' };

function renderStories(stories) {
  const list = $('#story-list');
  const filter = $('#story-filter').value;
  const filtered = filter === 'all' ? stories : stories.filter((story) => (story.urgency || 'medium') === filter);
  list.replaceChildren();
  $('#empty-state').hidden = stories.length > 0;
  if (!stories.length) {
    setText('#feed-count', '暂无已收录信号');
    return;
  }
  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'loading-state';
    empty.textContent = '这个紧急度暂时没有热点';
    list.append(empty);
    setText('#feed-count', `共 ${stories.length} 条信号`);
    return;
  }
  for (const story of filtered) {
    const row = document.createElement('article');
    row.className = 'story-row';
    const main = document.createElement('div');
    main.className = 'story-main';
    const meta = document.createElement('div');
    meta.className = 'story-meta';
    const source = document.createElement('span');
    source.className = 'story-source';
    source.textContent = story.source || '未知来源';
    const separator = document.createElement('span');
    separator.className = 'story-separator';
    separator.textContent = '·';
    const time = document.createElement('time');
    time.textContent = formatTime(story.published_at || story.discovered_at);
    meta.append(source, separator, time);
    const link = document.createElement('a');
    link.className = 'story-title';
    link.href = story.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = story.title;
    const summary = document.createElement('p');
    summary.className = 'story-summary';
    summary.textContent = story.summary_zh || story.summary || '来源未提供摘要，打开原文查看详情。';
    const tags = document.createElement('div');
    tags.className = 'story-tags';
    if (story.matched_keyword) {
      const keywordTag = document.createElement('span');
      keywordTag.className = 'story-tag';
      keywordTag.textContent = `# ${story.matched_keyword}`;
      tags.append(keywordTag);
    }
    if (story.corroborations > 1) {
      const sourceTag = document.createElement('span');
      sourceTag.className = 'story-tag';
      sourceTag.textContent = `${story.corroborations} 条交叉线索`;
      tags.append(sourceTag);
    }
    const sourceList = document.createElement('div');
    sourceList.className = 'story-sources';
    for (const evidence of story.sources || []) {
      try {
        const evidenceUrl = new URL(evidence.url);
        if (!['http:', 'https:'].includes(evidenceUrl.protocol)) continue;
        const evidenceLink = document.createElement('a');
        evidenceLink.href = evidenceUrl.href;
        evidenceLink.target = '_blank';
        evidenceLink.rel = 'noopener noreferrer';
        const regionLabel = evidence.region === 'china' ? '国内' : evidence.region === 'global' ? '国际' : '';
        const languageLabel = evidence.language ? evidence.language.toUpperCase() : '';
        evidenceLink.textContent = [evidence.publisher || evidence.platform || '来源', regionLabel, languageLabel].filter(Boolean).join(' · ');
        sourceList.append(evidenceLink);
      } catch { /* Ignore malformed source URLs. */ }
    }
    main.append(meta, link, summary, tags);
    if (sourceList.childElementCount) main.append(sourceList);
    const side = document.createElement('div');
    const urgency = story.urgency || 'medium';
    const priority = document.createElement('span');
    priority.className = `urgency urgency-${urgency}`;
    priority.textContent = urgencyLabels[urgency] || '中';
    side.append(priority);
    const truth = document.createElement('span');
    truth.className = `truth-label truth-${story.verdict || 'unverified'}`;
    truth.textContent = `真实性：${truthLabels[story.verdict] || '未核验'}`;
    side.append(truth);
    const relevance = document.createElement('div');
    relevance.className = 'story-confidence';
    relevance.textContent = `相关性 ${Math.round(story.relevance_score || 0)}%`;
    side.append(relevance);
    row.append(main, side);
    list.append(row);
  }
  setText('#feed-count', `显示 ${filtered.length} / ${stories.length} 条信号`);
}

function updateSchedule(state) {
  const interval = Number(state.settings.interval_minutes || 30);
  setText('#interval-display', `每 ${interval} 分钟`);
  const scanButton = $('#scan-button');
  const running = Boolean(state.scan_state.running);
  scanButton.disabled = running;
  scanButton.querySelector('span').textContent = running ? '扫描中…' : '立即扫描';
  setText('#live-label', running ? '正在扫描' : state.scan_state.error ? '部分连接异常' : '监控运行中');
  $('.live-pulse').style.background = state.scan_state.error ? '#d38e4a' : running ? '#e08b58' : '#62a77d';
  setText('#last-scan', formatTime(state.last_scan));
  if (running) setText('#next-scan', '扫描进行中');
  else if (!state.last_scan) setText('#next-scan', '即将开始首次扫描');
  else {
    const due = new Date(state.last_scan).getTime() + interval * 60_000;
    const remaining = Math.max(0, due - Date.now());
    setText('#next-scan', remaining < 60_000 ? '即将开始' : `${Math.ceil(remaining / 60_000)} 分钟后`);
  }
  const aiStatus = $('#ai-status');
  aiStatus.className = `status-pill ${state.settings.ai_configured ? 'status-on' : 'status-off'}`;
  aiStatus.textContent = state.settings.ai_configured ? '已配置' : '未配置';
  const twitterStatus = $('#twitter-status');
  twitterStatus.className = `status-pill ${state.settings.twitter_configured ? 'status-on' : 'status-off'}`;
  twitterStatus.textContent = state.settings.twitter_configured ? '已配置' : '未配置';
  const smtpStatus = $('#smtp-status');
  smtpStatus.className = `status-pill ${state.settings.smtp_configured ? 'status-on' : 'status-off'}`;
  smtpStatus.textContent = state.settings.smtp_configured ? 'SMTP 已配置' : 'SMTP 未配置';
}

function render(state) {
  dashboard = state;
  renderKeywords(state.keywords);
  renderStories(state.stories);
  const total = state.stories.length;
  const urgent = state.stories.filter((story) => story.urgency === 'urgent').length;
  const high = state.stories.filter((story) => story.urgency === 'high').length;
  setText('#metric-total', String(total).padStart(2, '0'));
  setText('#metric-urgent', String(urgent).padStart(2, '0'));
  setText('#metric-high', String(high).padStart(2, '0'));
  updateSchedule(state);
  if (!$('#scope-input').dataset.loaded) {
    $('#scope-input').value = state.settings.scope || 'AI 编程';
    $('#interval-input').value = state.settings.interval_minutes || 30;
    $('#ai-url').value = state.settings.ai_base_url || '';
    $('#ai-model').value = state.settings.ai_model || '';
    $('#smtp-host').value = state.settings.smtp_host || '';
    $('#smtp-port').value = state.settings.smtp_port || 587;
    $('#smtp-username').value = state.settings.smtp_username || '';
    $('#smtp-sender').value = state.settings.smtp_sender || '';
    $('#smtp-recipient').value = state.settings.smtp_recipient || '';
    $('#smtp-security').value = state.settings.smtp_use_ssl ? 'ssl' : state.settings.smtp_starttls ? 'starttls' : 'none';
    $('#scope-input').dataset.loaded = 'true';
  }
}

async function refresh() {
  try {
    render(await api('/api/state'));
  } catch (error) {
    setText('#live-label', '服务连接失败');
    showToast(`无法读取监控数据：${error.message}`, true);
  }
}

$('#keyword-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = $('#keyword-input');
  const phrase = input.value.trim();
  if (!phrase) return;
  try {
    const state = await api('/api/keywords', { method: 'POST', body: JSON.stringify({ phrase }) });
    input.value = '';
    render(state);
    showToast(`开始追踪「${phrase}」`);
  } catch (error) {
    showToast(error.message, true);
  }
});

$('#scan-button').addEventListener('click', async () => {
  try {
    await api('/api/scan', { method: 'POST', body: '{}' });
    showToast('扫描已开始，完成后会自动更新');
    await refresh();
  } catch (error) {
    showToast(error.message, true);
    await refresh();
  }
});

$('#refresh-button').addEventListener('click', refresh);
$('#story-filter').addEventListener('change', () => {
  if (dashboard) renderStories(dashboard.stories);
});

$('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const feedback = $('#settings-feedback');
  const smtpSecurity = $('#smtp-security').value;
  const payload = {
    scope: $('#scope-input').value.trim(),
    interval_minutes: $('#interval-input').value,
    ai_base_url: $('#ai-url').value.trim(),
    ai_model: $('#ai-model').value.trim(),
    smtp_host: $('#smtp-host').value.trim(),
    smtp_port: $('#smtp-port').value,
    smtp_username: $('#smtp-username').value.trim(),
    smtp_sender: $('#smtp-sender').value.trim(),
    smtp_recipient: $('#smtp-recipient').value.trim(),
    smtp_starttls: smtpSecurity === 'starttls',
    smtp_use_ssl: smtpSecurity === 'ssl',
    telegram_chat_id: $('#telegram-chat').value.trim(),
  };
  if ($('#ai-key').value.trim()) payload.ai_api_key = $('#ai-key').value.trim();
  if ($('#smtp-password').value.trim()) payload.smtp_password = $('#smtp-password').value.trim();
  if ($('#twitter-key').value.trim()) payload.twitter_api_key = $('#twitter-key').value.trim();
  if ($('#telegram-token').value.trim()) payload.telegram_bot_token = $('#telegram-token').value.trim();
  feedback.textContent = '保存中…';
  try {
    render(await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) }));
    $('#ai-key').value = '';
    $('#twitter-key').value = '';
    $('#smtp-password').value = '';
    $('#telegram-token').value = '';
    feedback.textContent = '设置已保存';
    showToast('设置已保存');
    setTimeout(() => { feedback.textContent = ''; }, 3000);
  } catch (error) {
    feedback.textContent = error.message;
    showToast(error.message, true);
  }
});

$('#test-email').addEventListener('click', async () => {
  const button = $('#test-email');
  button.disabled = true;
  try {
    await api('/api/notifications/test-email', { method: 'POST', body: '{}' });
    showToast('测试邮件已发送');
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$('#enable-browser-notifications').addEventListener('click', async () => {
  if (!('Notification' in window)) {
    showToast('此浏览器不支持桌面通知', true);
    return;
  }
  try {
    const permission = await Notification.requestPermission();
    showToast(permission === 'granted' ? '浏览器通知已启用' : '浏览器通知权限未授予', permission !== 'granted');
  } catch (error) {
    showToast(`无法启用浏览器通知：${error.message}`, true);
  }
});

$('#settings-form').addEventListener('input', () => {
  const feedback = $('#settings-feedback');
  if (feedback.textContent === '设置已保存') feedback.textContent = '';
});

connectRealtime();
refresh();
setInterval(refresh, 15_000);
