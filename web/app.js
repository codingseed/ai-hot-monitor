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
  realtimeSocket.addEventListener('open', () => {
    setRealtimeStatus(true);
    refresh();
  });
  realtimeSocket.addEventListener('close', () => {
    setRealtimeStatus(false);
    reconnectTimer = setTimeout(connectRealtime, 4000);
  });
  realtimeSocket.addEventListener('error', () => realtimeSocket.close());
  realtimeSocket.addEventListener('message', async (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    if (payload.type === 'scan_completed') {
      await refresh();
      return;
    }
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
  setText('#metric-keywords', String(keywords.filter((keyword) => keyword.enabled).length));
  if (!keywords.length) {
    const empty = document.createElement('span');
    empty.className = 'muted-copy';
    empty.textContent = '暂时还没有关注词';
    list.append(empty);
    return;
  }
  for (const keyword of keywords) {
    const row = document.createElement('div');
    row.className = 'keyword-row';
    const enabled = Boolean(keyword.enabled);
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = `keyword-toggle${enabled ? ' is-on' : ''}`;
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', String(enabled));
    toggle.setAttribute('aria-label', `${enabled ? '停用' : '启用'}监控词 ${keyword.phrase}`);
    toggle.title = `${enabled ? '停用' : '启用'} ${keyword.phrase}`;
    const knob = document.createElement('span');
    toggle.append(knob);
    toggle.addEventListener('click', async () => {
      toggle.disabled = true;
      try {
        render(await api(`/api/keywords/${keyword.id}`, {
          method: 'PATCH', body: JSON.stringify({ enabled: !enabled }),
        }));
      } catch (error) {
        showToast(error.message, true);
      }
    });
    const label = document.createElement('span');
    label.className = 'keyword-label';
    label.textContent = keyword.phrase;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'keyword-remove';
    remove.title = `移除 ${keyword.phrase}`;
    remove.setAttribute('aria-label', `移除关键词 ${keyword.phrase}`);
    remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';
    remove.addEventListener('click', async () => {
      try {
        render(await api(`/api/keywords/${keyword.id}`, { method: 'DELETE' }));
        showToast(`已移除「${keyword.phrase}」`);
      } catch (error) {
        showToast(error.message, true);
      }
    });
    row.append(toggle, label, remove);
    list.append(row);
  }
}

function renderSourceOptions(stories) {
  const sourceFilter = $('#source-filter');
  const selected = sourceFilter.value;
  const labels = new Map();
  for (const story of stories) {
    for (const source of story.sources || []) {
      if (source.platform) labels.set(source.platform, source.platform === 'twitterapi.io' ? 'X' : source.platform);
    }
  }
  sourceFilter.replaceChildren(new Option('全部来源', 'all'));
  for (const [value, label] of [...labels].sort((left, right) => left[1].localeCompare(right[1], 'zh-CN'))) {
    sourceFilter.add(new Option(label, value));
  }
  sourceFilter.value = labels.has(selected) ? selected : 'all';
}

const urgencyLabels = { urgent: '紧急', high: '高', medium: '中', low: '低' };
let visibleStoryLimit = 30;
const storyPageSize = 30;

function makeIcon(path) {
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('aria-hidden', 'true');
  icon.setAttribute('focusable', 'false');
  const shape = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  shape.setAttribute('d', path);
  icon.append(shape);
  return icon;
}

function formatMetric(value) {
  if (value === null || value === undefined || value === '') return '未获取';
  const number = Number(value);
  if (!Number.isFinite(number)) return '未获取';
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(number);
}

function renderSourceEvidence(source) {
  const item = document.createElement('article');
  item.className = 'source-evidence';
  const header = document.createElement('div');
  header.className = 'source-evidence-header';
  const publisher = document.createElement('strong');
  publisher.className = 'source-platform';
  publisher.append(makeIcon('M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm0 0c2.5 2.7 3.7 6 3.7 10S14.5 19.3 12 22m0-20C9.5 4.7 8.3 8 8.3 12s1.2 7.3 3.7 10M3 9h18M3 15h18'));
  const platform = source.is_x_source || source.platform === 'twitterapi.io' ? 'X' : source.publisher || source.platform || '未知来源';
  const platformName = document.createElement('span');
  platformName.textContent = platform;
  publisher.append(platformName);
  const time = document.createElement('time');
  time.textContent = formatTime(source.published_at);
  header.append(publisher);
  if (source.is_x_source) {
    const qualityLabels = {
      eligible: ['source-quality is-eligible', '互动门槛通过'],
      ineligible: ['source-quality is-ineligible', '未达互动门槛'],
      unknown: ['source-quality is-unknown', '互动数据未获取'],
    };
    const [qualityClass, qualityText] = qualityLabels[source.quality_status] || qualityLabels.unknown;
    const quality = document.createElement('span');
    quality.className = qualityClass;
    quality.textContent = qualityText;
    header.append(quality);
  }
  header.append(time);
  let externalUrl = '';
  try {
    const url = new URL(source.url);
    if (['http:', 'https:'].includes(url.protocol)) externalUrl = url.href;
  } catch { return null; }
  if (externalUrl) {
    const external = document.createElement('a');
    external.href = externalUrl;
    external.target = '_blank';
    external.rel = 'noopener noreferrer';
    external.textContent = '查看来源';
    header.append(external);
  }
  const title = document.createElement('p');
  title.className = 'source-evidence-title';
  title.textContent = source.title || '';
  const excerpt = document.createElement('p');
  excerpt.className = 'source-evidence-excerpt';
  excerpt.textContent = source.summary || source.title || '来源未提供摘要';
  const metrics = document.createElement('div');
  metrics.className = 'source-metrics';
  const metricFields = [
    ['like_count', '点赞', 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z'],
    ['retweet_count', '转发', 'M17 1l4 4-4 4M3 11V9a4 4 0 0 1 4-4h14M7 23l-4-4 4-4m14-2v2a4 4 0 0 1-4 4H3'],
    ['reply_count', '评论', 'M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 3.9a8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z'],
    ['view_count', '浏览', 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z'],
    ['author_followers', '粉丝', 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m6-10a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm12 10v-2a4 4 0 0 0-3-3.9m-3-12.1a4 4 0 0 1 0 7.8'],
  ];
  for (const [field, label, iconPath] of metricFields) {
    const metric = document.createElement('span');
    metric.className = 'source-metric';
    metric.title = label;
    metric.append(makeIcon(iconPath));
    const value = document.createElement('span');
    value.textContent = formatMetric(source[field]);
    metric.append(value);
    metrics.append(metric);
  }
  item.append(header, title, excerpt, metrics);
  return item;
}

function renderStories(stories) {
  const list = $('#story-list');
  const urgency = $('#story-filter').value;
  const sourcePlatform = $('#source-filter').value;
  const minimumRelevance = Number($('#relevance-filter').value || 0);
  const aiConfigured = Boolean(dashboard?.settings?.ai_configured);
  const filtered = stories.filter((story) => {
    if (urgency !== 'all' && (story.urgency || 'medium') !== urgency) return false;
    if (sourcePlatform !== 'all' && !(story.sources || []).some((source) => source.platform === sourcePlatform)) return false;
    if (aiConfigured && story.verdict !== 'unverified' && Number(story.relevance_score || 0) < minimumRelevance) return false;
    return true;
  });
  if ($('#sort-filter').value === 'relevance') {
    filtered.sort((left, right) => Number(right.relevance_score || 0) - Number(left.relevance_score || 0));
  } else {
    filtered.sort((left, right) => new Date(right.published_at || right.discovered_at || 0)
      - new Date(left.published_at || left.discovered_at || 0));
  }
  const visible = filtered.slice(0, visibleStoryLimit);
  list.replaceChildren();
  $('#empty-state').hidden = stories.length > 0;
  $('#load-more').hidden = filtered.length <= visible.length;
  $('#load-more').textContent = `加载更多（剩余 ${Math.max(0, filtered.length - visible.length)} 条）`;
  if (!stories.length) {
    setText('#feed-count', '暂无已收录信号');
    return;
  }
  if (!filtered.length) {
    const empty = document.createElement('div');
    empty.className = 'loading-state';
    empty.textContent = '当前筛选条件下暂无热点';
    list.append(empty);
    setText('#feed-count', `共 ${stories.length} 条，当前条件下暂无结果`);
    return;
  }
  for (const story of visible) {
    const row = document.createElement('article');
    row.className = 'story-row spotlight';
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
    const translation = story.translation ? document.createElement('p') : null;
    if (translation) {
      translation.className = 'story-translation';
      translation.textContent = `${story.translation_language === 'zh' ? '中文译文' : 'English translation'}：${story.translation}`;
    }
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
    if (story.source_quality_warning) {
      const qualityTag = document.createElement('span');
      qualityTag.className = 'story-tag story-tag-warning';
      qualityTag.textContent = '含未达门槛的 X 来源';
      tags.append(qualityTag);
    }
    const sourceList = document.createElement('div');
    sourceList.className = 'story-sources';
    const sourceItems = story.sources || [];
    for (const evidence of sourceItems.slice(0, 3)) {
      const item = renderSourceEvidence(evidence);
      if (item) sourceList.append(item);
    }
    if (sourceItems.length > 3) {
      const moreSources = document.createElement('details');
      moreSources.className = 'more-sources';
      const moreLabel = document.createElement('summary');
      moreLabel.textContent = `展开其余 ${sourceItems.length - 3} 条来源`;
      moreSources.append(moreLabel);
      for (const evidence of sourceItems.slice(3)) {
        const item = renderSourceEvidence(evidence);
        if (item) moreSources.append(item);
      }
      sourceList.append(moreSources);
    }
    main.append(meta, link, summary);
    if (translation) main.append(translation);
    main.append(tags);
    if (sourceList.childElementCount) main.append(sourceList);
    const side = document.createElement('div');
    side.className = 'story-side';
    const urgency = story.urgency || 'medium';
    const priority = document.createElement('span');
    priority.className = `urgency urgency-${urgency}`;
    const urgencyIcon = urgency === 'urgent' || urgency === 'high'
      ? 'M12 22s-7-4.4-7-11a7 7 0 0 1 14 0c0 6.6-7 11-7 11Zm0-15a4 4 0 0 0-4 4c0 2.2 1.4 4.2 4 6 2.6-1.8 4-3.8 4-6a4 4 0 0 0-4-4Z'
      : 'M13 2 4 14h6l-1 8 11-13h-7l1-7Z';
    priority.append(makeIcon(urgencyIcon), document.createTextNode(urgencyLabels[urgency] || '中'));
    side.append(priority);
    const relevance = document.createElement('div');
    relevance.className = `story-confidence${story.relevance_unavailable ? ' is-unavailable' : ''}`;
    relevance.append(makeIcon('M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2'));
    relevance.append(document.createTextNode(story.relevance_unavailable
      ? '相关性暂不可用' : `主题相关性 ${Math.round(story.relevance_score || 0)}%`));
    if (story.rationale) relevance.title = story.rationale;
    side.append(relevance);
    row.append(main, side);
    list.append(row);
  }
  setText('#feed-count', `显示 ${visible.length} / ${filtered.length} 条 · 已载入 ${stories.length} 条`);
}

function updateSchedule(state) {
  const scanButton = $('#scan-button');
  const running = Boolean(state.scan_state.running);
  const enabledCount = state.keywords.filter((keyword) => keyword.enabled).length;
  scanButton.disabled = running || enabledCount === 0;
  scanButton.title = enabledCount ? '仅扫描已启用的监控词' : '请先启用至少一个监控词';
  scanButton.querySelector('span').textContent = running ? '扫描中…' : enabledCount ? '立即扫描' : '请启用监控词';
  setText('#live-label', running ? '正在扫描' : state.scan_state.error ? '部分连接异常' : enabledCount ? '手动监控就绪' : '请启用监控词');
  $('.live-pulse').style.background = state.scan_state.error ? '#d38e4a' : running ? '#e08b58' : '#62a77d';
  setText('#scan-mode', enabledCount ? `手动扫描 · ${enabledCount} 个监控词已启用` : '启用监控词后可扫描');
  setText('#last-scan', formatTime(state.last_scan));
  if (running) setText('#next-scan', '扫描进行中');
  else setText('#next-scan', enabledCount ? '等待手动扫描' : '请先启用监控词');
  const aiStatus = $('#ai-status');
  aiStatus.className = `status-pill ${state.settings.ai_configured ? 'status-on' : 'status-off'}`;
  aiStatus.textContent = state.settings.ai_configured ? '已配置' : '未配置';
  const twitterStatus = $('#twitter-status');
  twitterStatus.className = `status-pill ${state.settings.twitter_configured ? 'status-on' : 'status-off'}`;
  twitterStatus.textContent = state.settings.twitter_configured ? '已配置' : '未配置';
  const smtpStatus = $('#smtp-status');
  smtpStatus.className = `status-pill ${state.settings.smtp_configured ? 'status-on' : 'status-off'}`;
  smtpStatus.textContent = state.settings.smtp_configured ? 'SMTP 已配置' : 'SMTP 未配置';
  const sourceStatus = $('#source-status');
  if (sourceStatus) {
    sourceStatus.textContent = state.scan_state.error ? '部分来源异常' : 'Google News · Bing · HN · X';
    sourceStatus.title = state.scan_state.error || 'DuckDuckGo 仅在当前网络可用时补充结果';
  }
}

function render(state) {
  dashboard = state;
  renderKeywords(state.keywords);
  renderSourceOptions(state.stories);
  renderStories(state.stories);
  const total = state.total_story_count ?? state.stories.length;
  const urgent = state.stories.filter((story) => story.urgency === 'urgent').length;
  const high = state.stories.filter((story) => story.urgency === 'high').length;
  setText('#metric-total', String(total).padStart(2, '0'));
  setText('#metric-urgent', String(urgent).padStart(2, '0'));
  setText('#metric-high', String(high).padStart(2, '0'));
  updateSchedule(state);
  if (!$('#ai-url').dataset.loaded) {
    $('#ai-url').value = state.settings.ai_base_url || '';
    $('#ai-model').value = state.settings.ai_model || '';
    $('#smtp-host').value = state.settings.smtp_host || '';
    $('#smtp-port').value = state.settings.smtp_port || 587;
    $('#smtp-username').value = state.settings.smtp_username || '';
    $('#smtp-sender').value = state.settings.smtp_sender || '';
    $('#smtp-recipient').value = state.settings.smtp_recipient || '';
    $('#smtp-security').value = state.settings.smtp_use_ssl ? 'ssl' : state.settings.smtp_starttls ? 'starttls' : 'none';
    $('#ai-url').dataset.loaded = 'true';
  }
}

let lastStoriesSignature = '';
async function refresh() {
  try {
    const state = await api('/api/state?limit=500');
    const signature = JSON.stringify(state.stories.map((s) => [s.id, s.urgency, s.relevance_score, s.verdict,
      s.summary_zh, s.translation, s.title, (s.sources || []).length]));
    const changed = signature !== lastStoriesSignature;
    lastStoriesSignature = signature;
    if (changed) render(state);
    else {
      dashboard = state;
      renderKeywords(state.keywords);
      updateSchedule(state);
      setText('#metric-total', String(state.total_story_count ?? state.stories.length).padStart(2, '0'));
      setText('#metric-urgent', String(state.stories.filter((s) => s.urgency === 'urgent').length).padStart(2, '0'));
      setText('#metric-high', String(state.stories.filter((s) => s.urgency === 'high').length).padStart(2, '0'));
      setText('#metric-keywords', String(state.keywords.filter((keyword) => keyword.enabled).length));
    }
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
for (const selector of ['#story-filter', '#source-filter', '#relevance-filter', '#sort-filter']) {
  $(selector).addEventListener('change', () => {
    visibleStoryLimit = storyPageSize;
    if (dashboard) renderStories(dashboard.stories);
  });
}
$('#load-more').addEventListener('click', () => {
  visibleStoryLimit += storyPageSize;
  if (dashboard) renderStories(dashboard.stories);
});

$('#settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const feedback = $('#settings-feedback');
  const smtpSecurity = $('#smtp-security').value;
  const payload = {
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


// Spotlight tracking for tech cards (only the hovered card)
document.addEventListener('mousemove', (event) => {
  const target = event.target && event.target.closest ? event.target.closest('.spotlight') : null;
  if (target) {
    const rect = target.getBoundingClientRect();
    const x = ((event.clientX - rect.left) / rect.width) * 100;
    const y = ((event.clientY - rect.top) / rect.height) * 100;
    target.style.setProperty('--mx', `${x}%`);
    target.style.setProperty('--my', `${y}%`);
  }
});


// Typewriter effect for hero subtitle
window.addEventListener('DOMContentLoaded', () => {
  const el = document.querySelector('.page-heading p');
  if (!el) return;
  const full = el.textContent;
  el.textContent = '';
  let i = 0;
  const tick = () => {
    if (i <= full.length) {
      el.textContent = full.slice(0, i);
      i++;
      setTimeout(tick, 45);
    }
  };
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) tick();
  else el.textContent = full;
});
