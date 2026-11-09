// Minimale service worker: maakt de app installeerbaar en houdt statische bestanden snel beschikbaar.
// Pagina's en API-verzoeken gaan altijd naar het netwerk (financiële gegevens mogen nooit verouderd zijn).
const CACHE = "mmm-static-v1";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(["/static/icons/icon.svg"])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/static/") && url.searchParams.has("v")) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        const copy = response.clone();
        if (response.ok) caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      }))
    );
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => new Response(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Offline</title><body style='font-family:system-ui;padding:2rem;text-align:center'><h1>Je bent offline</h1><p>MyMoneyManager heeft verbinding met je server nodig. Probeer het zo opnieuw.</p>",
        { headers: { "Content-Type": "text/html; charset=utf-8" }, status: 503 }
      ))
    );
  }
});
