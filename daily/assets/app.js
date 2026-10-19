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

  /* ----------------------------------------------------------------- state */

  const state = {
    tab: 'papers',
    query: '',
    sort: 'date',
    keyword: '',
    papers: [],
    news: [],
    meta: {},
    saved: safeRead(STORE.saved, {}),
    reading: safeRead(STORE.reading, {}),
    streak: 0,
    loading: true,
    error: '',
    lastSync: 0,
  };

  /* --------------------------------------------------------------- storage */

  const itemKey = (kind, id) => `${kind}:${id}`;

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
        },
      };
      toast('已收藏，可在「收藏」里复习');
    }
    safeWrite(STORE.saved, state.saved);
    render();
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
      const [papersRes, newsRes, metaRes] = await Promise.all([
        fetch(DATA_URLS.papers + cacheBust, { cache: force ? 'reload' : 'default' }),
        fetch(DATA_URLS.news + cacheBust, { cache: force ? 'reload' : 'default' }),
        fetch(DATA_URLS.meta + cacheBust, { cache: force ? 'reload' : 'default' }).catch(() => null),
      ]);
      const papers = papersRes.ok ? await papersRes.json() : [];
      const news = newsRes.ok ? await newsRes.json() : [];
      const meta = metaRes && metaRes.ok ? await metaRes.json() : {};

      state.papers = normalizeList(papers);
      state.news = normalizeList(news);
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
      Array.isArray(item.summary_zh) ? item.summary_zh.join(' ') : item.summary_zh,
      item.source,
      item.primary_category,
      (item.authors || []).join(' '),
      (item.matched || []).join(' '),
      (item.tags || []).join(' '),
      (item.categories || []).join(' '),
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
      list.sort((a, b) => String(b.published || '').localeCompare(String(a.published || '')));
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
    const pool = state.tab === 'news' ? state.news : state.papers;
    let items = pool.filter((item) => matchesQuery(item, q));
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
            : '今天这里还是空的';
      $('#empty-text').textContent = state.error
        ? state.error
        : state.query
          ? `没有找到包含「${state.query}」的条目，换个词试试。`
          : state.tab === 'saved'
            ? '在论文或新闻卡片右上角点一下 ☆，就会收藏到这里，方便之后复习。'
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
    const label = state.tab === 'papers' ? '论文' : state.tab === 'news' ? '新闻' : '收藏';
    const bits = [`共 ${total}${label}`];
    if (state.query) bits.push(`匹配「${state.query}」`);
    if (state.tab === 'papers' && state.keyword) bits.push(`关键词 ${state.keyword}`);
    if (state.tab === 'papers' && state.sort === 'score') bits.push('按相关度');
    return bits.join(' · ');
  }

  function renderGrouped(items) {
    const groups = new Map();
    items.forEach((item) => {
      const day = item.day || cnDay(item.published);
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day).push(item);
    });
    const days = Array.from(groups.keys()).sort((a, b) => b.localeCompare(a));
    return days
      .map((day) => {
        const rows = groups.get(day).map((item) => renderCard(item)).join('');
        const unit = state.tab === 'news' ? '条' : '篇';
        return `<div class="day-head"><span>${esc(dayLabel(day))}</span><span class="count">${groups.get(day).length} ${unit}</span></div>${rows}`;
      })
      .join('');
  }

  function renderCard(item) {
    const kind = item.kind || (state.tab === 'saved' ? (item.url ? 'paper' : 'news') : state.tab === 'news' ? 'news' : 'paper');
    return kind === 'news' ? renderNewsCard(item) : renderPaperCard(item);
  }

  function renderPaperCard(item) {
    const q = state.query.trim();
    const id = esc(item.id || '');
    const saved = isSaved('paper', item.id);
    const titleZh = item.title_zh ? `<h3 class="zh">${highlight(item.title_zh, q)}</h3>` : '';
    const titleEn = `<h3 class="${item.title_zh ? 'title-en' : ''}">${highlight(item.title, q)}</h3>`;
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
      <article class="card paper${read ? ' is-read' : ''}" data-key="paper:${id}">
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

  function renderNewsCard(item) {
    const q = state.query.trim();
    const saved = isSaved('news', item.id);
    const id = esc(item.id || '');
    const summary = item.summary ? `<p class="summary">${highlight(item.summary, q)}</p>` : '';
    const read = isRead('news', item.id);
    return `
      <article class="card news${read ? ' is-read' : ''}" data-key="news:${id}">
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
          <a href="${esc(item.link)}" target="_blank" rel="noopener" data-read="news" data-id="${id}">${highlight(item.title, q)}</a>
        </h3>
        ${summary}
        <a class="open-hint" href="${esc(item.link)}" target="_blank" rel="noopener" data-read="news" data-id="${id}">
          阅读原文
          <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17 17 7M9 7h8v8"/></svg>
        </a>
      </article>`;
  }

  /* ------------------------------------------------------------- chrome bits */

  function renderHeader() {
    $('#today-label').textContent = `${dayLabel(todayStr())} · 每天进步一点点`;
    updateStats();
  }

  function updateStats() {
    const today = todayStr();
    const papersToday = state.papers.filter((p) => (p.day || cnDay(p.published)) === today).length;
    const newsToday = state.news.filter((n) => (n.day || cnDay(n.published)) === today).length;
    $('#st-papers').textContent = state.loading ? '–' : papersToday;
    $('#st-news').textContent = state.loading ? '–' : newsToday;
    $('#st-read').textContent = readToday();
    $('#st-streak').textContent = state.streak || 1;

    const meta = state.meta || {};
    const stamp = meta.papers && meta.papers.generated_at;
    $('#foot-meta').textContent = stamp
      ? `数据更新于 ${relTime(stamp)} · 由 GitHub Actions 每日自动生成`
      : '数据由 GitHub Actions 每天自动生成';
  }

  function renderTabs() {
    $$('.tab').forEach((tab) => {
      tab.setAttribute('aria-selected', String(tab.dataset.tab === state.tab));
    });
    $('#sort-wrap').hidden = state.tab !== 'papers';
  }

  function renderChips() {
    const wrap = $('#keyword-chips');
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
    state.tab = ['papers', 'news', 'saved'].includes(tab) ? tab : 'papers';
    state.keyword = '';
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
     常驻栏（.app-bar）用 CSS sticky 贴在顶部，高度恒定、由浏览器负责，
     不参与这里任何滚动计算，因此不需要 JS，也不会抖动。 */

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

    // keyword chips
    $('#keyword-chips').addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip) return;
      state.keyword = chip.dataset.kw || '';
      render();
    });

    // card interactions (delegated)
    $('#list').addEventListener('click', (event) => {
      const saveBtn = event.target.closest('[data-action="save"]');
      if (saveBtn) {
        event.preventDefault();
        const kind = saveBtn.dataset.kind;
        const id = saveBtn.dataset.id;
        const pool = kind === 'news' ? state.news : state.papers;
        const item = pool.find((x) => String(x.id) === id) || state.saved[itemKey(kind, id)]?.item;
        if (item) toggleSave(kind, item);
        return;
      }
      const readLink = event.target.closest('[data-read]');
      if (readLink) markRead(readLink.dataset.read, readLink.dataset.id);
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

    // keyboard: "/" focuses search, 1/2/3 switch tabs
    document.addEventListener('keydown', (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (event.key === '/' && !typing) {
        event.preventDefault();
        $('#search').focus();
      }
      if (!typing && ['1', '2', '3'].includes(event.key)) {
        setTab({ 1: 'papers', 2: 'news', 3: 'saved' }[event.key]);
      }
    });
  }

  /* ------------------------------------------------------------------- boot */

  function boot() {
    bumpStreak();
    applyThemeSilently();
    wireEvents();
    wireInstall();
    registerSW();

    const hash = location.hash.replace('#', '');
    if (['papers', 'news', 'saved'].includes(hash)) state.tab = hash;

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
