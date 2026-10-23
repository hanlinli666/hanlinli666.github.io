/* ==========================================================================
   今日精进 — app logic
   Vanilla JS, zero build step. Reads ./data/{papers,news,meta}.json which are
   regenerated every morning by GitHub Actions.
   ========================================================================== */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const TZ = 'Asia/Shanghai';
  const DATA_URLS = {
    papers: './data/papers.json',
    news: './data/news.json',
    events: './data/events.json',
    meta: './data/meta.json',
  };
  const STORE = {
    theme: 'dl.theme',
    saved: 'dl.saved',
    reading: 'dl.reading',
    streak: 'dl.streak',
    lastSync: 'dl.lastSync',
  };
  const MAX_READ_DAYS = 60;

  /* ------------------------------------------------------------- utilities */

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

  const safeRead = (key, fallback) => {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (err) {
      return fallback;
    }
  };

  const safeWrite = (key, value) => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (err) {
      /* private mode / quota: ignore */
    }
  };

  const cnDay = (iso) => {
    const d = iso ? new Date(iso) : new Date();
    if (Number.isNaN(d.getTime())) return '';
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit',
    }).format(d);
  };

  const todayStr = () => cnDay(new Date().toISOString());

  const shiftDay = (dayStr, delta) => {
    const [y, m, d] = dayStr.split('-').map(Number);
    const dt = new Date(Date.UTC(y, m - 1, d));
    dt.setUTCDate(dt.getUTCDate() + delta);
    return dt.toISOString().slice(0, 10);
  };

  const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  const dayLabel = (dayStr) => {
    if (!dayStr) return '';
    const [y, m, d] = dayStr.split('-').map(Number);
    const today = todayStr();
    if (dayStr === today) return '今天';
    if (dayStr === shiftDay(today, -1)) return '昨天';
    const dow = WEEK[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
    const yearPart = today.slice(0, 4) === String(y) ? '' : `${y}年`;
    return `${yearPart}${m}月${d}日 · ${dow}`;
  };

  const clock = (iso) => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(d);
  };

  const relTime = (iso, now = Date.now()) => {
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return '';
    const diff = Math.max(0, now - t);
    const min = Math.floor(diff / 60000);
    if (min < 1) return '刚刚';
    if (min < 60) return `${min} 分钟前`;
    const hours = Math.floor(min / 60);
    if (hours < 24) return `${hours} 小时前`;
    const days = Math.floor(hours / 24);
    if (days === 1) return '昨天';
    if (days < 7) return `${days} 天前`;
    const d = new Date(iso);
    return new Intl.DateTimeFormat('zh-CN', { timeZone: TZ, month: 'long', day: 'numeric' }).format(d);
  };

  function highlight(text, query) {
    const raw = String(text ?? '');
    if (!query) return esc(raw);
    const needle = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    if (!needle) return esc(raw);
    const re = new RegExp(`(${needle})`, 'gi');
    return raw
      .split(re)
      .map((part, i) => (i % 2 ? `<mark class="hl">${esc(part)}</mark>` : esc(part)))
      .join('');
  }

  function sourceBadge(name) {
    const clean = String(name || '?').trim();
    if (!clean) return '?';
    if (/[\u4e00-\u9fff]/.test(clean)) return clean.slice(0, 1);
    const words = clean.split(/[\s·\-]+/).filter(Boolean);
    if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
    return clean.slice(0, 2).toUpperCase();
  }

  const authorLine = (item) => {
    const list = item.authors || [];
    if (!list.length) return '';
    const limit = window.innerWidth < 480 ? 2 : 3;
    const shown = list.slice(0, limit).join('、');
    const total = item.author_count || list.length;
    return total > limit ? `${shown} 等 ${total} 人` : shown;
  };

  /* 中文约 350 字/分钟，英文约 180 词/分钟；用来给阅读层一个时间预期 */
  const readingMinutes = (text) => {
    const raw = String(text || '');
    const cn = (raw.match(/[\u4e00-\u9fff]/g) || []).length;
    const words = (raw.replace(/[\u4e00-\u9fff]/g, ' ').match(/[A-Za-z0-9]+/g) || []).length;
    return Math.max(1, Math.ceil(cn / 350 + words / 180));
  };

  const isEnglishText = (text) => {
    const raw = String(text || '');
    if (raw.length < 40) return false;
    const cn = (raw.match(/[\u4e00-\u9fff]/g) || []).length;
    const latin = (raw.match(/[A-Za-z]/g) || []).length;
    return latin > 40 && latin > cn * 2;
  };

  const eventSourceList = (item) => {
    const seen = new Set();
    const list = [];
    const push = (url, source, title, published) => {
      if (!url || seen.has(url)) return;
      seen.add(url);
      list.push({ url, source, title, published });
    };
    const primary = (item.links || []).find((link) => link.url === item.primary_url);
    push(item.primary_url || item.link, primary ? primary.source : '原始报道', primary ? primary.title : item.title_zh || item.title);
    (item.links || []).forEach((link) => push(link.url, link.source, link.title, link.published));
    return list;
  };

  const looksLikeUrl = (value) => /^https?:\/\//i.test(String(value || ''));

  /* ----------------------------------------------------------------- state */

  const state = {
    tab: 'events',
    query: '',
    sort: 'date',
    keyword: '',
    filter: 'all',
    papers: [],
    news: [],
    events: [],
    meta: {},
    saved: safeRead(STORE.saved, {}),
    reading: safeRead(STORE.reading, {}),
    streak: 0,
    loading: true,
    error: '',
    lastSync: 0,
    reader: null,
  };

  /* --------------------------------------------------------------- storage */

  const itemKey = (kind, id) => `${kind}:${id}`;

  function findItem(kind, id) {
    const pool = kind === 'event' ? state.events : kind === 'news' ? state.news : state.papers;
    return pool.find((item) => String(item.id) === String(id)) || state.saved[itemKey(kind, id)]?.item || null;
  }

  function isSaved(kind, id) {
    return Boolean(state.saved[itemKey(kind, id)]);
  }

  function toggleSave(kind, item) {
    const key = itemKey(kind, item.id);
    if (state.saved[key]) {
      delete state.saved[key];
      toast('已取消收藏');
    } else {
      state.saved[key] = {
        kind,
        id: item.id,
        at: new Date().toISOString(),
        item: {
          id: item.id,
          title: item.title,
          title_zh: item.title_zh || null,
          abstract: item.abstract || '',
          summary: item.summary || '',
          summary_zh: item.summary_zh || null,
          link: item.link || item.url,
          url: item.url,
          pdf: item.pdf,
          source: item.source || 'arXiv',
          primary_category: item.primary_category,
          published: item.published,
          day: item.day,
          authors: item.authors || [],
          author_count: item.author_count || (item.authors || []).length,
          matched: item.matched || [],
          // 事件卡片的字段
          primary_url: item.primary_url,
          event_summary: item.event_summary,
          potential_impact: item.potential_impact,
          key_entities: item.key_entities || [],
          topics: item.topics || [],
          source_level: item.source_level,
          policy_stage: item.policy_stage,
          first_seen_at: item.first_seen_at,
          source_count: item.source_count,
          links: item.links || [],
        },
      };
      toast('已收藏，可在「收藏」里复习');
    }
    safeWrite(STORE.saved, state.saved);
    render();
    refreshReaderActions();
  }

  function markRead(kind, id) {
    const day = todayStr();
    const bucket = state.reading[day] || [];
    if (!bucket.includes(itemKey(kind, id))) {
      bucket.push(itemKey(kind, id));
      state.reading[day] = bucket;
      const days = Object.keys(state.reading).sort().slice(-MAX_READ_DAYS);
      const pruned = {};
      days.forEach((d) => { pruned[d] = state.reading[d]; });
      state.reading = pruned;
      safeWrite(STORE.reading, state.reading);
    }
    updateStats();
    const node = document.querySelector(`.card[data-key="${kind}:${id}"]`);
    if (node) node.classList.add('is-read');
  }

  function readToday() {
    return (state.reading[todayStr()] || []).length;
  }

  function isRead(kind, id) {
    return (state.reading[todayStr()] || []).includes(itemKey(kind, id));
  }

  function bumpStreak() {
    const today = todayStr();
    const record = safeRead(STORE.streak, null);
    let streak = 1;
    if (record && record.last === today) {
      streak = record.streak || 1;
    } else if (record && record.last === shiftDay(today, -1)) {
      streak = (record.streak || 1) + 1;
    }
    safeWrite(STORE.streak, { last: today, streak });
    state.streak = streak;
  }

  /* ----------------------------------------------------------------- toast */

  let toastTimer = 0;
  function toast(message) {
    const el = $('#toast');
    el.textContent = message;
    el.classList.add('show');
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => el.classList.remove('show'), 2000);
  }

  /* ------------------------------------------------------------------ data */

  async function loadData(force = false) {
    const cacheBust = force ? `?v=${Date.now()}` : '';
    setLoading(true);
    try {
      const [papersRes, newsRes, eventsRes, metaRes] = await Promise.all([
        fetch(DATA_URLS.papers + cacheBust, { cache: force ? 'reload' : 'default' }),
        fetch(DATA_URLS.news + cacheBust, { cache: force ? 'reload' : 'default' }).catch(() => null),
        fetch(DATA_URLS.events + cacheBust, { cache: force ? 'reload' : 'default' }).catch(() => null),
        fetch(DATA_URLS.meta + cacheBust, { cache: force ? 'reload' : 'default' }).catch(() => null),
      ]);
      const papers = papersRes.ok ? await papersRes.json() : [];
      const news = newsRes && newsRes.ok ? await newsRes.json() : [];
      const events = eventsRes && eventsRes.ok ? await eventsRes.json() : [];
      const meta = metaRes && metaRes.ok ? await metaRes.json() : {};

      state.papers = normalizeList(papers);
      state.news = normalizeList(news);
      const eventList = normalizeList(events);
      // 事件层是后加的：老部署或精炼失败时用新闻池兜底，页面不会空着
      state.events = eventList.length ? eventList : eventsFromNews(state.news);
      state.meta = meta || {};
      state.error = '';
      state.lastSync = Date.now();
      safeWrite(STORE.lastSync, state.lastSync);
    } catch (err) {
      state.error = '数据加载失败，可能正在更新，请稍后下拉刷新。';
      console.error(err);
    } finally {
      setLoading(false);
      render();
    }
  }

  function eventsFromNews(news) {
    return news.map((item) => ({
      id: item.id,
      title_zh: item.title,
      event_summary: item.summary || '',
      source_level: item.level || 'L2',
      policy_stage: '不适用',
      key_entities: [],
      potential_impact: null,
      topics: item.matched || [],
      first_seen_at: item.first_seen || item.published,
      published: item.published,
      primary_url: item.link,
      source_count: 1,
      links: [],
    }));
  }

  const normalizeList = (payload) => {
    if (Array.isArray(payload)) return payload.filter((x) => x && typeof x === 'object');
    if (payload && Array.isArray(payload.items)) return payload.items;
    return [];
  };

  function setLoading(on) {
    state.loading = on;
    $('#skeleton').hidden = !on;
    $('#btn-refresh').classList.toggle('busy', on);
  }

  /* -------------------------------------------------------------- filtering */

  function matchesQuery(item, query) {
    if (!query) return true;
    const q = query.toLowerCase();
    const haystack = [
      item.title,
      item.title_zh,
      item.abstract,
      item.summary,
      item.event_summary,
      item.potential_impact,
      Array.isArray(item.summary_zh) ? item.summary_zh.join(' ') : item.summary_zh,
      item.source,
      item.primary_category,
      (item.authors || []).join(' '),
      (item.matched || []).join(' '),
      (item.tags || []).join(' '),
      (item.categories || []).join(' '),
      (item.key_entities || []).join(' '),
      (item.topics || []).join(' '),
      (item.links || []).map((l) => `${l.source} ${l.title}`).join(' '),
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    return haystack.includes(q);
  }

  function sorted(items) {
    const list = items.slice();
    if (state.sort === 'score') {
      list.sort((a, b) => (b.score || 0) - (a.score || 0) || String(b.published).localeCompare(String(a.published)));
    } else {
      list.sort((a, b) => String(b.published || b.first_seen_at || '').localeCompare(String(a.published || a.first_seen_at || '')));
    }
    return list;
  }

  function visibleItems() {
    const q = state.query.trim();
    if (state.tab === 'saved') {
      const items = Object.values(state.saved)
        .sort((a, b) => String(b.at).localeCompare(String(a.at)))
        .map((entry) => ({ ...entry.item, kind: entry.kind }));
      return items.filter((item) => matchesQuery(item, q));
    }
    const pool = state.tab === 'events' ? state.events : state.papers;
    let items = pool.filter((item) => matchesQuery(item, q));
    if (state.tab === 'events') {
      if (state.filter === 'l1') items = items.filter((item) => item.source_level === 'L1');
      if (state.filter === 'unread') items = items.filter((item) => !isRead('event', item.id));
    }
    if (state.tab === 'papers' && state.keyword) {
      items = items.filter((item) => (item.matched || []).includes(state.keyword));
    }
    return sorted(items);
  }

  /* ----------------------------------------------------------------- render */

  function render() {
    renderHeader();
    renderTabs();
    renderChips();

    const items = visibleItems();
    const list = $('#list');
    const empty = $('#empty');
    const info = $('#result-info');

    if (state.loading) {
      list.innerHTML = '';
      empty.hidden = true;
      info.textContent = '正在同步今日内容…';
      return;
    }

    if (!items.length) {
      list.innerHTML = '';
      empty.hidden = false;
      $('#empty-title').textContent = state.error
        ? '暂时拿不到数据'
        : state.query
          ? '没有匹配的内容'
          : state.tab === 'saved'
            ? '还没有收藏'
            : state.tab === 'events'
              ? '今天还没有事件'
              : '今天这里还是空的';
      $('#empty-text').textContent = state.error
        ? state.error
        : state.query
          ? `没有找到包含「${state.query}」的条目，换个词试试。`
          : state.tab === 'saved'
            ? '在论文或事件卡片右上角点一下 ☆，就会收藏到这里，方便之后复习。'
            : state.tab === 'events'
              ? '采集还在跑，下拉刷新一次试试。'
              : 'arXiv 在周末不发布新论文，可以先看看之前的内容。';
      info.textContent = '';
      return;
    }

    empty.hidden = true;
    info.textContent = summarize(items);
    list.innerHTML = renderGrouped(items);
  }

  function summarize(items) {
    const total = `${items.length} 条`;
    const label = state.tab === 'papers' ? '论文' : state.tab === 'events' ? '事件' : '收藏';
    const bits = [`共 ${total}${label}`];
    if (state.query) bits.push(`匹配「${state.query}」`);
    if (state.tab === 'events' && state.filter === 'l1') bits.push('仅官方 L1');
    if (state.tab === 'events' && state.filter === 'unread') bits.push('仅未读');
    if (state.tab === 'papers' && state.keyword) bits.push(`关键词 ${state.keyword}`);
    if (state.tab === 'papers' && state.sort === 'score') bits.push('按相关度');
    return bits.join(' · ');
  }

  function renderGrouped(items) {
    const groups = new Map();
    items.forEach((item) => {
      const day = item.day || cnDay(item.first_seen_at || item.published);
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day).push(item);
    });
    const days = Array.from(groups.keys()).sort((a, b) => b.localeCompare(a));
    return days
      .map((day) => {
        const rows = groups.get(day).map((item) => renderCard(item)).join('');
        const unit = state.tab === 'events' ? '件' : state.tab === 'papers' ? '篇' : '条';
        return `<div class="day-head"><span>${esc(dayLabel(day))}</span><span class="count">${groups.get(day).length} ${unit}</span></div>${rows}`;
      })
      .join('');
  }

  function renderCard(item) {
    const raw = item.kind || (state.tab === 'saved' ? (item.url ? 'paper' : 'news') : state.tab);
    // tab 叫 events（复数），卡片类型叫 event（单数）；不归一化会把事件当论文渲染
    const kind = raw === 'events' ? 'event' : raw;
    if (kind === 'event') return renderEventCard(item);
    return kind === 'news' ? renderNewsCard(item) : renderPaperCard(item);
  }

  function renderPaperCard(item) {
    const q = state.query.trim();
    const id = esc(item.id || '');
    const saved = isSaved('paper', item.id);
    const titleZh = item.title_zh
      ? `<h3 class="zh"><button class="card-open" type="button" data-open-kind="paper" data-open-id="${id}">${highlight(item.title_zh, q)}</button></h3>`
      : '';
    const titleEn = `<h3 class="title-en"><button class="card-open" type="button" data-open-kind="paper" data-open-id="${id}">${highlight(item.title, q)}</button></h3>`;
    // AI 主题词优先；没有摘要时退回「命中关键词」，读者总能知道这篇在讲什么
    const topics = (item.tags && item.tags.length ? item.tags : item.matched) || [];
    const matched = topics
      .slice(0, 4)
      .map((kw) => `<span class="kw">${esc(kw)}</span>`)
      .join('');
    const bullets = Array.isArray(item.summary_zh) && item.summary_zh.length
      ? `<ul class="bullets">${item.summary_zh.map((b) => `<li>${highlight(b, q)}</li>`).join('')}</ul>`
      : '';
    const abstract = item.abstract
      ? `<details class="abstract"><summary>原文摘要</summary><p>${highlight(item.abstract, q)}</p></details>`
      : '';
    const cat = item.primary_category ? `<span class="cat">${esc(item.primary_category)}</span>` : '';
    const authors = authorLine(item);
    const read = isRead('paper', item.id);

    return `
      <article class="card paper${read ? ' is-read' : ''}" data-key="paper:${id}" data-card-kind="paper" data-card-id="${id}">
        <div class="card-top">
          ${cat}
          <span class="dot"></span>
          <span>${esc(clock(item.published))}</span>
          <span class="spacer"></span>
          <button class="save-btn" type="button" data-action="save" data-kind="paper" data-id="${id}"
            aria-pressed="${saved}" aria-label="${saved ? '取消收藏' : '收藏'}">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4L4.2 9.7l5.4-.8Z"/></svg>
          </button>
        </div>
        ${titleZh}
        ${titleEn}
        ${matched ? `<div class="kw-row">${matched}</div>` : ''}
        ${bullets}
        ${abstract}
        <div class="card-foot">
          <span class="authors">${esc(authors)}</span>
          <a class="btn ghost" href="${esc(item.pdf || item.url)}" target="_blank" rel="noopener" data-read="paper" data-id="${id}">PDF</a>
          <a class="btn" href="${esc(item.url)}" target="_blank" rel="noopener" data-read="paper" data-id="${id}">arXiv ↗</a>
        </div>
      </article>`;
  }

  function renderEventCard(item) {
    const q = state.query.trim();
    const id = esc(item.id || '');
    const saved = isSaved('event', item.id);
    const read = isRead('event', item.id);
    const level = item.source_level || 'L2';
    const levelLabel = { L1: 'L1 官方', L2: 'L2 媒体', L3: 'L3 社区' }[level] || 'L2 媒体';
    const stage = item.policy_stage && item.policy_stage !== '不适用'
      ? `<span class="stage">${esc(item.policy_stage)}</span>`
      : '';
    const summary = item.event_summary
      ? `<p class="summary">${highlight(item.event_summary, q)}</p>`
      : '';
    const chips = [...(item.key_entities || []), ...(item.topics || [])]
      .slice(0, 3)
      .map((tag) => `<span class="kw">${esc(tag)}</span>`)
      .join('');
    const primary = (item.links || []).find((link) => link.url === item.primary_url) || (item.links || [])[0];
    const sourceName = primary ? primary.source : '原始报道';
    const count = (item.source_count || 1) > 1 ? ` · ${item.source_count} 家来源` : '';
    const url = item.primary_url || item.link || '#';

    return `
      <article class="card event${read ? ' is-read' : ''}" data-key="event:${id}" data-card-kind="event" data-card-id="${id}">
        <div class="src-row">
          <span class="lv lv-${esc(level.toLowerCase())}">${levelLabel}</span>
          ${stage}
          <span class="dot"></span>
          <span>${esc(relTime(item.first_seen_at))}</span>
          <span class="spacer"></span>
          <button class="save-btn" type="button" data-action="save" data-kind="event" data-id="${id}"
            aria-pressed="${saved}" aria-label="${saved ? '取消收藏' : '收藏'}">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4L4.2 9.7l5.4-.8Z"/></svg>
          </button>
        </div>
        <h3 class="zh">
          <button class="card-open" type="button" data-open-kind="event" data-open-id="${id}">${highlight(item.title_zh, q)}</button>
        </h3>
        ${summary}
        ${chips ? `<div class="kw-row">${chips}</div>` : ''}
        <div class="card-foot">
          <span class="authors">${esc(sourceName)}${count}</span>
          <a class="src-link" href="${esc(url)}" target="_blank" rel="noopener" data-read="event" data-id="${id}">
            原文
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M9 7h8v8"/></svg>
          </a>
        </div>
      </article>`;
  }

  function renderNewsCard(item) {
    const q = state.query.trim();
    const saved = isSaved('news', item.id);
    const id = esc(item.id || '');
    const summary = item.summary ? `<p class="summary">${highlight(item.summary, q)}</p>` : '';
    const read = isRead('news', item.id);
    return `
      <article class="card news${read ? ' is-read' : ''}" data-key="news:${id}" data-card-kind="news" data-card-id="${id}">
        <div class="src-row">
          <span class="src-badge">${esc(sourceBadge(item.source))}</span>
          <span>${esc(item.source || '新闻')}</span>
          <span class="dot"></span>
          <span>${esc(relTime(item.published))}</span>
          <span class="spacer"></span>
          <button class="save-btn" type="button" data-action="save" data-kind="news" data-id="${id}"
            aria-pressed="${saved}" aria-label="${saved ? '取消收藏' : '收藏'}">
            <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4L4.2 9.7l5.4-.8Z"/></svg>
          </button>
        </div>
        <h3>
          <button class="card-open" type="button" data-open-kind="news" data-open-id="${id}">${highlight(item.title, q)}</button>
        </h3>
        ${summary}
        <div class="card-foot">
          <span class="authors">${esc(item.source || '新闻')}</span>
          <a class="src-link" href="${esc(item.link)}" target="_blank" rel="noopener" data-read="news" data-id="${id}">
            原文
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M9 7h8v8"/></svg>
          </a>
        </div>
      </article>`;
  }

  /* ------------------------------------------------------------------ reader */

  function readerEvent(item) {
    const q = state.query.trim();
    const level = item.source_level || 'L2';
    const levelLabel = { L1: 'L1 官方', L2: 'L2 媒体', L3: 'L3 社区' }[level] || 'L2 媒体';
    const stage = item.policy_stage && item.policy_stage !== '不适用'
      ? `<span class="stage">${esc(item.policy_stage)}</span>`
      : '';
    const first = item.first_seen_at || item.published;
    const summary = item.event_summary || item.summary || '';
    const impact = item.potential_impact || '';
    const tags = [...(item.key_entities || []), ...(item.topics || [])];
    const links = eventSourceList(item);
    const english = isEnglishText(summary);
    return `
      <div class="r-meta">
        <span class="lv lv-${esc(level.toLowerCase())}">${levelLabel}</span>
        ${stage}
        <span class="dot"></span>
        <span>${esc(dayLabel(cnDay(first)))} ${esc(clock(first))}</span>
        <span class="dot"></span>
        <span>约 ${readingMinutes(summary + impact)} 分钟</span>
      </div>
      <h2 id="reader-title" class="r-title">${highlight(item.title_zh || item.title, q)}</h2>
      ${english && summary ? `<p class="r-eyebrow">英文原文摘要 · 可点底部「阅读原文」查看全文</p>` : ''}
      ${summary ? `<p class="r-summary">${highlight(summary, q)}</p>` : ''}
      ${impact ? `<div class="r-impact"><span>潜在影响</span><p>${highlight(impact, q)}</p></div>` : ''}
      ${tags.length ? `<div class="r-tags">${tags.map((tag) => `<span class="kw">${esc(tag)}</span>`).join('')}</div>` : ''}
      ${links.length ? `
        <div class="r-sources">
          <h3>信息来源</h3>
          <ul>${links.map((link) => `
            <li>
              <a href="${esc(link.url)}" target="_blank" rel="noopener">
                <span class="r-src-name">${esc(link.source || '来源')}</span>
                <span class="r-src-title">${esc(link.title || '')}</span>
                ${link.published ? `<span class="r-src-time">${esc(clock(link.published))}</span>` : ''}
              </a>
            </li>`).join('')}
          </ul>
        </div>` : ''}
    `;
  }

  function readerPaper(item) {
    const q = state.query.trim();
    const cat = item.primary_category ? `<span class="cat">${esc(item.primary_category)}</span>` : '';
    const day = item.day || cnDay(item.published);
    const bullets = Array.isArray(item.summary_zh) ? item.summary_zh : [];
    const list = item.authors || [];
    const total = item.author_count || list.length;
    const authors = list.length
      ? `${list.slice(0, 8).join('、')}${total > Math.min(list.length, 8) ? ` 等 ${total} 人` : ''}`
      : '';
    return `
      <div class="r-meta">
        ${cat}
        <span class="dot"></span>
        <span>${esc(dayLabel(day))} ${esc(clock(item.published))}</span>
        <span class="dot"></span>
        <span>约 ${readingMinutes(bullets.join('') + (item.abstract || ''))} 分钟</span>
      </div>
      <h2 id="reader-title" class="r-title">${highlight(item.title_zh || item.title, q)}</h2>
      ${item.title_zh ? `<p class="r-subtitle">${highlight(item.title, q)}</p>` : ''}
      ${bullets.length ? `
        <div class="r-block">
          <h3>AI 要点</h3>
          <ul class="r-bullets">${bullets.map((b) => `<li>${highlight(b, q)}</li>`).join('')}</ul>
        </div>` : ''}
      ${item.abstract ? `<details class="r-abstract"><summary>原文摘要</summary><p>${highlight(item.abstract, q)}</p></details>` : ''}
      ${authors ? `<p class="r-author">${esc(authors)}</p>` : ''}
    `;
  }

  function readerNews(item) {
    const q = state.query.trim();
    const summary = item.summary || '';
    return `
      <div class="r-meta">
        <span class="src-badge">${esc(sourceBadge(item.source))}</span>
        <span>${esc(item.source || '新闻')}</span>
        <span class="dot"></span>
        <span>${esc(dayLabel(cnDay(item.published)))} ${esc(clock(item.published))}</span>
        <span class="dot"></span>
        <span>约 ${readingMinutes(summary)} 分钟</span>
      </div>
      <h2 id="reader-title" class="r-title">${highlight(item.title, q)}</h2>
      ${isEnglishText(summary) ? '<p class="r-eyebrow">英文原文摘要</p>' : ''}
      ${summary ? `<p class="r-summary">${highlight(summary, q)}</p>` : ''}
      ${(item.matched || []).length ? `<div class="r-tags">${item.matched.slice(0, 8).map((tag) => `<span class="kw">${esc(tag)}</span>`).join('')}</div>` : ''}
    `;
  }

  function readerActions(kind, item) {
    const saved = isSaved(kind, item.id);
    const url = item.link || item.primary_url || item.url;
    const primary = looksLikeUrl(url) ? url : '';
    const pdf = kind === 'paper' && looksLikeUrl(item.pdf) ? item.pdf : '';
    return `
      <button class="r-btn ghost" type="button" data-action="save" data-kind="${kind}" data-id="${esc(item.id)}" aria-pressed="${saved}">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" ${saved ? 'fill="currentColor"' : ''}><path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4-4.8-2.5-4.8 2.5.9-5.4L4.2 9.7l5.4-.8Z"/></svg>
        ${saved ? '已收藏' : '收藏'}
      </button>
      ${pdf ? `<a class="r-btn ghost" href="${esc(pdf)}" target="_blank" rel="noopener">PDF</a>` : ''}
      ${primary ? `
        <a class="r-btn primary" href="${esc(primary)}" target="_blank" rel="noopener">
          阅读原文
          <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M9 7h8v8"/></svg>
        </a>` : ''}
    `;
  }

  let readerReturnFocus = null;

  function openReader(kind, id) {
    const item = findItem(kind, id);
    if (!item) return;
    if (!state.reader) {
      readerReturnFocus = document.activeElement;
      try { history.pushState({ reader: true }, '', location.href); } catch (err) { /* file:// 等环境 */ }
    }
    state.reader = { kind, id: String(item.id) };
    const body = kind === 'event' ? readerEvent(item) : kind === 'news' ? readerNews(item) : readerPaper(item);
    $('#reader-body').innerHTML = body;
    $('#reader-actions').innerHTML = readerActions(kind, item);
    $('#reader-scroll').scrollTop = 0;

    const el = $('#reader');
    el.hidden = false;
    document.body.classList.add('is-locked');
    requestAnimationFrame(() => el.classList.add('is-open'));
    markRead(kind, item.id);
    window.setTimeout(() => {
      const close = $('.reader-close');
      if (close) close.focus({ preventScroll: true });
    }, 40);
  }

  function closeReader({ fromHistory = false } = {}) {
    if (!state.reader) return;
    state.reader = null;
    const el = $('#reader');
    el.classList.remove('is-open');
    document.body.classList.remove('is-locked');
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.setTimeout(() => {
      if (!state.reader) el.hidden = true;
    }, reduce ? 0 : 200);
    if (!fromHistory && history.state && history.state.reader) {
      history.back();
    }
    if (readerReturnFocus && readerReturnFocus.isConnected) {
      readerReturnFocus.focus({ preventScroll: true });
      readerReturnFocus = null;
    }
  }

  function trapReaderFocus(event) {
    const nodes = $$('#reader a[href], #reader button:not([disabled])').filter((node) => node.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function refreshReaderActions() {
    if (!state.reader) return;
    const item = findItem(state.reader.kind, state.reader.id);
    if (!item) return;
    $('#reader-actions').innerHTML = readerActions(state.reader.kind, item);
  }

  /* ------------------------------------------------------------- chrome bits */

  function renderHeader() {
    $('#today-label').textContent = `${dayLabel(todayStr())} · 每天进步一点点`;
    updateStats();
  }

  function updateStats() {
    const today = todayStr();
    const papersToday = state.papers.filter((p) => (p.day || cnDay(p.published)) === today).length;
    const eventsToday = state.events.filter((e) => cnDay(e.first_seen_at || e.published) === today).length;
    $('#st-papers').textContent = state.loading ? '–' : papersToday;
    $('#st-events').textContent = state.loading ? '–' : eventsToday;
    $('#st-read').textContent = readToday();
    $('#st-streak').textContent = state.streak || 1;

    const meta = state.meta || {};
    const stamp = (meta.events && meta.events.generated_at) || (meta.papers && meta.papers.generated_at);
    $('#foot-meta').textContent = stamp
      ? `数据更新于 ${relTime(stamp)} · 由 GitHub Actions 每日自动生成`
      : '数据由 GitHub Actions 每天自动生成';
  }

  function renderTabs() {
    $$('.tab').forEach((tab) => {
      tab.setAttribute('aria-selected', String(tab.dataset.tab === state.tab));
    });
    $('#sort-wrap').hidden = state.tab !== 'papers';
    const placeholders = {
      events: '搜索事件、来源、话题…',
      papers: '搜索论文、作者、方向…',
      saved: '搜索收藏…',
    };
    $('#search').placeholder = placeholders[state.tab] || placeholders.events;
  }

  function renderChips() {
    const wrap = $('#keyword-chips');
    if (state.loading) {
      wrap.hidden = true;
      return;
    }
    if (state.tab === 'events') {
      const l1 = state.events.filter((item) => item.source_level === 'L1').length;
      const unread = state.events.filter((item) => !isRead('event', item.id)).length;
      const chip = (key, label, count) =>
        `<button class="chip" type="button" data-filter="${key}" aria-pressed="${state.filter === key}">${esc(label)}<span class="chip-n">${count}</span></button>`;
      wrap.hidden = false;
      wrap.innerHTML = [
        chip('all', '全部', state.events.length),
        chip('l1', '官方 L1', l1),
        chip('unread', '未读', unread),
      ].join('');
      return;
    }

    const keywords = (state.meta.papers && state.meta.papers.keywords) || [];
    if (state.tab !== 'papers' || !keywords.length) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    const all = [`<button class="chip" type="button" data-kw="" aria-pressed="${state.keyword === ''}">全部</button>`];
    keywords.forEach((kw) => {
      const label = kw.replace(/\s*\+\s*/g, ' + ');
      all.push(`<button class="chip" type="button" data-kw="${esc(kw)}" aria-pressed="${state.keyword === kw}">${esc(label)}</button>`);
    });
    wrap.innerHTML = all.join('');
  }

  /* ---------------------------------------------------------------- actions */

  function setTab(tab, { push = true } = {}) {
    if (tab === 'news') tab = 'events'; // 旧链接兼容
    state.tab = ['events', 'papers', 'saved'].includes(tab) ? tab : 'events';
    state.keyword = '';
    state.filter = 'all';
    if (push) {
      const target = `#${state.tab}`;
      if (location.hash !== target) history.replaceState(null, '', target);
    }
    render();
    window.scrollTo({ top: 0 });
  }

  function applyTheme(next) {
    document.documentElement.dataset.theme = next;
    if (next === 'auto') {
      localStorage.removeItem(STORE.theme);
    } else {
      localStorage.setItem(STORE.theme, next);
    }
    const btn = $('#btn-theme');
    btn.title = `当前：${next === 'auto' ? '跟随系统' : next === 'dark' ? '深色' : '浅色'}`;
    toast(next === 'auto' ? '跟随系统外观' : next === 'dark' ? '已切换到深色' : '已切换到浅色');
  }

  function cycleTheme() {
    const current = document.documentElement.dataset.theme || 'auto';
    const next = current === 'auto' ? 'dark' : current === 'dark' ? 'light' : 'auto';
    applyTheme(next);
  }

  /* -------------------------------------------------------------- SW / install */

  let deferredPrompt = null;

  function registerSW() {
    if (!('serviceWorker' in navigator)) return;
    const secure = location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname);
    if (!secure) return;
    navigator.serviceWorker
      .register('./sw.js', { scope: './' })
      .then((reg) => {
        reg.addEventListener('updatefound', () => {
          const sw = reg.installing;
          if (!sw) return;
          sw.addEventListener('statechange', () => {
            if (sw.state === 'installed' && navigator.serviceWorker.controller) {
              toast('新版本已就绪，重新打开即可生效');
            }
          });
        });
      })
      .catch((err) => console.warn('SW registration failed', err));
  }

  const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const isStandalone = () =>
    window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

  function wireInstall() {
    const btn = $('#btn-install');
    if (isStandalone()) return;

    window.addEventListener('beforeinstallprompt', (event) => {
      event.preventDefault();
      deferredPrompt = event;
      btn.hidden = false;
    });

    window.addEventListener('appinstalled', () => {
      deferredPrompt = null;
      btn.hidden = true;
      toast('已安装到主屏幕 🎉');
    });

    if (isIOS()) btn.hidden = false;

    btn.addEventListener('click', async () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        const choice = await deferredPrompt.userChoice;
        if (choice && choice.outcome === 'accepted') btn.hidden = true;
        deferredPrompt = null;
        return;
      }
      $('#ios-tip').hidden = false;
    });

    $('#ios-close').addEventListener('click', () => { $('#ios-tip').hidden = true; });
    $('#ios-tip').addEventListener('click', (event) => {
      if (event.target.id === 'ios-tip') $('#ios-tip').hidden = true;
    });
  }

  /* 顶部栏说明：
     常驻栏（.app-bar）用 CSS sticky 贴在顶部，高度恒定、由浏览器负责；
     JS 只读取它的高度写进 --bar-h，供吸顶日期栏对齐，不参与滚动计算。 */

  /* ----------------------------------------------------------------- events */

  function wireEvents() {
    $$('.tab').forEach((tab) => {
      tab.addEventListener('click', () => setTab(tab.dataset.tab));
    });

    const search = $('#search');
    let searchTimer = 0;
    search.addEventListener('input', () => {
      $('#search-clear').hidden = !search.value;
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        state.query = search.value.trim();
        render();
      }, 110);
    });
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        search.value = '';
        state.query = '';
        $('#search-clear').hidden = true;
        render();
      }
    });
    $('#search-clear').addEventListener('click', () => {
      search.value = '';
      state.query = '';
      $('#search-clear').hidden = true;
      search.focus();
      render();
    });

    // sort dropdown
    const sortBtn = $('#btn-sort');
    const sortMenu = $('#sort-menu');
    const closeSort = () => {
      sortMenu.hidden = true;
      sortBtn.setAttribute('aria-expanded', 'false');
    };
    sortBtn.addEventListener('click', (event) => {
      event.stopPropagation();
      const open = sortMenu.hidden;
      sortMenu.hidden = !open;
      sortBtn.setAttribute('aria-expanded', String(open));
    });
    $$('#sort-menu button').forEach((option) => {
      option.addEventListener('click', () => {
        state.sort = option.dataset.sort;
        $('#sort-label').textContent = option.textContent;
        $$('#sort-menu button').forEach((b) => b.setAttribute('aria-selected', String(b === option)));
        closeSort();
        render();
      });
    });
    document.addEventListener('click', (event) => {
      if (!$('#sort-wrap').contains(event.target)) closeSort();
    });

    // chips: 事件用来源/未读筛选，论文用关键词
    $('#keyword-chips').addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip) return;
      if (chip.dataset.filter) {
        state.filter = chip.dataset.filter;
      } else {
        state.keyword = chip.dataset.kw || '';
      }
      render();
    });

    // card interactions (delegated)
    $('#list').addEventListener('click', (event) => {
      const saveBtn = event.target.closest('[data-action="save"]');
      if (saveBtn) {
        event.preventDefault();
        const kind = saveBtn.dataset.kind;
        const id = saveBtn.dataset.id;
        const item = findItem(kind, id);
        if (item) toggleSave(kind, item);
        return;
      }
      const openBtn = event.target.closest('button[data-open-kind]');
      if (openBtn) {
        openReader(openBtn.dataset.openKind, openBtn.dataset.openId);
        return;
      }
      const readLink = event.target.closest('[data-read]');
      if (readLink) markRead(readLink.dataset.read, readLink.dataset.id);
      if (event.target.closest('a, button, summary, details')) return;
      const card = event.target.closest('.card[data-card-kind]');
      if (card) openReader(card.dataset.cardKind, card.dataset.cardId);
    });

    // reader: 关闭按钮 / 遮罩 / 收藏
    $$('[data-reader-close]').forEach((node) => {
      node.addEventListener('click', () => closeReader());
    });
    $('#reader-actions').addEventListener('click', (event) => {
      const btn = event.target.closest('[data-action="save"]');
      if (!btn || !state.reader) return;
      event.preventDefault();
      const item = findItem(btn.dataset.kind, btn.dataset.id);
      if (!item) return;
      toggleSave(btn.dataset.kind, item);
      const next = $('#reader-actions [data-action="save"]');
      if (next) next.focus({ preventScroll: true });
    });
    window.addEventListener('popstate', () => {
      if (state.reader) closeReader({ fromHistory: true });
    });

    $('#btn-theme').addEventListener('click', cycleTheme);

    $('#btn-refresh').addEventListener('click', async () => {
      if ('serviceWorker' in navigator) {
        const reg = await navigator.serviceWorker.getRegistration();
        if (reg) reg.update().catch(() => {});
      }
      await loadData(true);
      toast('已同步最新内容');
    });

    window.addEventListener('hashchange', () => {
      const tab = location.hash.replace('#', '');
      if (tab && tab !== state.tab) setTab(tab, { push: false });
    });

    // refresh when the app returns to the foreground after a while
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      const stale = Date.now() - (state.lastSync || 0) > 30 * 60 * 1000;
      if (stale && !state.loading) loadData(false);
      updateStats();
    });

    // 长列表滚动后出现「回到顶部」
    const toTop = $('#to-top');
    let topTick = false;
    window.addEventListener('scroll', () => {
      if (topTick) return;
      topTick = true;
      requestAnimationFrame(() => {
        toTop.hidden = window.scrollY < 640;
        topTick = false;
      });
    }, { passive: true });
    toTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

    // keyboard: "/" focuses search, 1/2/3 switch tabs, Esc 关闭阅读层
    document.addEventListener('keydown', (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (state.reader) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeReader();
          return;
        }
        if (event.key === 'Tab') {
          trapReaderFocus(event);
          return;
        }
      }
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (event.key === '/' && !typing) {
        event.preventDefault();
        $('#search').focus();
      }
      if (!typing && !state.reader && ['1', '2', '3'].includes(event.key)) {
        setTab({ 1: 'events', 2: 'papers', 3: 'saved' }[event.key]);
      }
    });
  }

  /* ------------------------------------------------------------------- boot */

  /* 给吸顶日期栏提供实际高度：手机端常驻栏只有搜索行，桌面端还含标签行 */
  function syncBarHeight() {
    const bar = $('.app-bar');
    if (bar) document.documentElement.style.setProperty('--bar-h', `${bar.offsetHeight}px`);
  }

  function boot() {
    bumpStreak();
    applyThemeSilently();
    wireEvents();
    wireInstall();
    registerSW();
    syncBarHeight();
    if ('ResizeObserver' in window) {
      new ResizeObserver(syncBarHeight).observe($('.app-bar'));
    }

    const hash = location.hash.replace('#', '');
    if (['events', 'papers', 'saved', 'news'].includes(hash)) {
      state.tab = hash === 'news' ? 'events' : hash;
    }

    // Deep link from the Feishu card: ?q=<paper title>#papers
    const params = new URLSearchParams(location.search);
    const deepQuery = (params.get('q') || '').trim();
    if (deepQuery) {
      state.query = deepQuery;
      const input = $('#search');
      if (input) {
        input.value = deepQuery;
        $('#search-clear').hidden = false;
      }
      state.tab = 'papers';
    }

    const cachedSync = safeRead(STORE.lastSync, 0);
    state.lastSync = typeof cachedSync === 'number' ? cachedSync : 0;

    render();
    loadData(false);
  }

  function applyThemeSilently() {
    const theme = document.documentElement.dataset.theme || 'auto';
    const btn = $('#btn-theme');
    if (btn) btn.title = `当前：${theme === 'auto' ? '跟随系统' : theme === 'dark' ? '深色' : '浅色'}`;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
