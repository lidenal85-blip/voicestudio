/**
 * sw.js — Service Worker для Voice Studio PWA
 *
 * Стратегии кеширования:
 *   - Static assets (JS, CSS, шрифты) → Cache First
 *   - HTML страницы                   → Network First (с fallback на кеш)
 *   - API запросы                     → Network Only (данные должны быть свежими)
 *   - Аудиофайлы (/api/.../assets/)  → Cache First с ограничением размера
 *
 * Версия кеша — при деплое изменить CACHE_VERSION для инвалидации.
 */

const CACHE_VERSION   = 'vs-v1';
const STATIC_CACHE    = `${CACHE_VERSION}-static`;
const AUDIO_CACHE     = `${CACHE_VERSION}-audio`;
const PAGE_CACHE      = `${CACHE_VERSION}-pages`;

const MAX_AUDIO_CACHE = 20;   // максимум аудиофайлов в кеше

// Статика которую кешируем при установке
const PRECACHE_URLS = [
  '/',
  '/static/js/karaoke-engine.js',
  '/static/js/lyric-sync.js',
  '/static/js/waveform.js',
  '/static/js/lrc-editor.js',
  '/static/js/ws-client.js',
  '/static/js/recorder-engine.js',
  '/static/js/latency-measurer.js',
];

// ── Install ────────────────────────────────────────────────────────────────────

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
      .catch(err => console.warn('[SW] precache failed:', err))
  );
});

// ── Activate ───────────────────────────────────────────────────────────────────

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(k => k.startsWith('vs-') && !k.startsWith(CACHE_VERSION))
          .map(k => { console.log('[SW] deleting old cache:', k); return caches.delete(k); })
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch ──────────────────────────────────────────────────────────────────────

self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // Только GET
  if (request.method !== 'GET') return;

  // API запросы → Network Only (кроме аудиофайлов)
  if (url.pathname.startsWith('/api/')) {
    if (isAudioAsset(url.pathname)) {
      event.respondWith(cacheFirstAudio(request));
    }
    // Остальные API → сеть, без кеша
    return;
  }

  // Статические ресурсы → Cache First
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(cacheFirstStatic(request));
    return;
  }

  // WebSocket → пропускаем
  if (url.pathname.startsWith('/ws/')) return;

  // HTML страницы → Network First
  event.respondWith(networkFirstPage(request));
});

// ── Стратегии ──────────────────────────────────────────────────────────────────

async function cacheFirstStatic(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(STATIC_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return new Response('Static asset unavailable offline', { status: 503 });
  }
}

async function networkFirstPage(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(PAGE_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    return offlineFallback(request.url);
  }
}

async function cacheFirstAudio(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(AUDIO_CACHE);
      // LRU: удаляем старые если превышен лимит
      const keys = await cache.keys();
      if (keys.length >= MAX_AUDIO_CACHE) {
        await cache.delete(keys[0]);
      }
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached2 = await caches.match(request);
    if (cached2) return cached2;
    return new Response('Audio unavailable offline', { status: 503 });
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function isAudioAsset(pathname) {
  return pathname.includes('/assets/') || pathname.includes('/recordings/');
}

function offlineFallback(url) {
  return new Response(
    `<!DOCTYPE html><html lang="ru"><head>
      <meta charset="UTF-8"><title>Офлайн — Voice Studio</title>
      <meta name="viewport" content="width=device-width,initial-scale=1">
      <style>
        body{background:#07070f;color:#c8d8e8;font-family:monospace;
             display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
        .box{text-align:center;padding:32px}
        h1{color:#7afdd6;font-size:1.3rem;margin-bottom:12px}
        p{color:#4a5568;font-size:0.85rem}
        a{color:#7afdd6}
      </style>
    </head><body>
      <div class="box">
        <h1>◈ Voice Studio</h1>
        <p>Нет соединения с сервером.</p>
        <p>Проверь что <code>start.sh</code> запущен на телефоне.</p>
        <p style="margin-top:16px"><a href="/">Попробовать снова</a></p>
      </div>
    </body></html>`,
    { headers: { 'Content-Type': 'text/html;charset=utf-8' }, status: 503 }
  );
}
