/* Service worker do Vocalis.
 *
 * - Casco do app (HTML/CSS/JS/vendor) pré-armazenado para o PWA abrir offline.
 * - Áudios baixados (/data/downloads/) em cache com limite de itens (para não
 *   estourar a quota do navegador).
 * - APIs GET (faixas, fila, letras) em "network-first": cai para o cache
 *   quando o servidor está fora, para o app continuar consultável.
 */
const CACHE_NAME = 'vocalis-shell-v2';
const CACHE_AUDIO = 'vocalis-audio-v2';
const MAX_ITENS = 8;

const CASCO = [
  '/',
  '/static/manifest.webmanifest',
  '/static/icon.svg',
  '/static/styles.css?v=20261014a',
  '/static/offline.js?v=20261014a',
  '/static/app.js?v=20261014a',
  '/static/karaoke.js?v=20261014a',
  '/static/vendor/pitchy.min.js',
  '/static/vendor/qrcode-generator.js',
];

async function guardarComLimite(request, response) {
  const cache = await caches.open(CACHE_AUDIO);
  await cache.put(request, response);
  const chaves = await cache.keys();
  if (chaves.length > MAX_ITENS) await cache.delete(chaves[0]);
}

async function devolverDoCacheOuRede(request) {
  const cache = await caches.open(CACHE_NAME);
  const emCache = await cache.match(request);
  if (emCache) return emCache;
  const resposta = await fetch(request);
  if (resposta.ok) cache.put(request, resposta.clone()).catch(() => {});
  return resposta;
}

async function redeOuCache(request) {
  try {
    const resposta = await fetch(request);
    if (resposta.ok && request.method === 'GET') {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, resposta.clone()).catch(() => {});
    }
    return resposta;
  } catch (_) {
    const cache = await caches.open(CACHE_NAME);
    const emCache = await cache.match(request, { ignoreSearch: true });
    if (emCache) return emCache;
    throw new Error('sem rede e sem cache');
  }
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(CASCO)).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((chaves) => Promise.all(
      chaves.filter((nome) => nome !== CACHE_NAME && nome !== CACHE_AUDIO)
        .map((nome) => caches.delete(nome)),
    )).then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;

  // Navegação (abrir o app): primeira rede, depois o casco em cache.
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(async () => {
      const cache = await caches.open(CACHE_NAME);
      const pagina = await cache.match('/');
      return pagina || Response.error();
    }));
    return;
  }

  // Áudios baixados: cache-first com limite para não estourar a quota.
  if (url.pathname.startsWith('/data/downloads/')) {
    event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request).then((response) => {
      if (response.ok && response.status !== 206) {
        guardarComLimite(event.request, response.clone()).catch(() => {});
      }
      return response;
    })));
    return;
  }

  // Estáticos do app: cache-first (são versionados por ?v=).
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(devolverDoCacheOuRede(event.request));
    return;
  }

  // APIs de leitura: rede primeiro, cache como fallback offline.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(redeOuCache(event.request));
  }
});