/* ==========================================================================
   今日精进 — service worker
   Bump VERSION when the shell changes so old caches are dropped.
   ========================================================================== */

const VERSION = 'v1';
const SHELL_CACHE = `dl-shell-${VERSION}`;
const DATA_CACHE = `dl-data-${VERSION}`;

const SHELL_ASSETS = [
  './',
  './index.html',
  './assets/app.css',
  './assets/app.js',
  './manifest.webmanifest',
  './icons/favicon.svg',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      // addAll() rejects when any single URL fails: add one by one instead.
      await Promise.all(
        SHELL_ASSETS.map((url) =>
          cache.add(new Request(url, { cache: 'reload' })).catch(() => {})
        )
      );
      await self.skipWaiting();
    })()
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key !== SHELL_CACHE && key !== DATA_CACHE)
          .map((key) => caches.delete(key))
      );
      await self.clients.claim();
    })()
  );
});

/** Cache key without the ?v= cache-buster so offline lookups still hit. */
function cleanRequest(request) {
  const url = new URL(request.url);
  url.search = '';
  return new Request(url.toString(), { method: 'GET' });
}

async function networkFirst(request, cacheName, fallbackUrl) {
  const cache = await caches.open(cacheName);
  const key = cleanRequest(request);
  try {
    const response = await fetch(request);
    if (response && (response.ok || response.type === 'opaque')) {
      cache.put(key, response.clone()).catch(() => {});
    }
    return response;
  } catch (err) {
    const cached = (await cache.match(key)) || (await cache.match(request));
    if (cached) return cached;
    if (fallbackUrl) {
      const shell = await caches.open(SHELL_CACHE);
      const fallback = await shell.match(fallbackUrl);
      if (fallback) return fallback;
    }
    return new Response('离线状态下暂无这份数据', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  }
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(cleanRequest(request));
  if (cached) {
    // Refresh in the background (stale-while-revalidate).
    fetch(request)
      .then((response) => {
        if (response && response.ok) cache.put(cleanRequest(request), response.clone());
      })
      .catch(() => {});
    return cached;
  }
  const response = await fetch(request);
  if (response && response.ok) cache.put(cleanRequest(request), response.clone()).catch(() => {});
  return response;
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // cross-origin: browser default

  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request, SHELL_CACHE, './index.html'));
    return;
  }

  if (/\/data\/[^/]+\.json$/.test(url.pathname)) {
    event.respondWith(networkFirst(request, DATA_CACHE));
    return;
  }

  event.respondWith(cacheFirst(request, SHELL_CACHE));
});

self.addEventListener('message', (event) => {
  if (event.data === 'skip-waiting') self.skipWaiting();
});
