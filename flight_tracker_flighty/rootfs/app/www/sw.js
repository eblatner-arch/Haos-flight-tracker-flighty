// Flight Tracker Flighty: keeps the app itself on the phone, so it opens instantly.
// The page and its files (app.js, style.css, the map library, icons) are answered from the phone's
// copy right away while a fresh copy is fetched in the background for next time. Data (api/...)
// always goes to the add-on. Files are named by their content (?v=...), so an old page always finds
// the files it was built with; older copies are dropped as new ones arrive.
const SHELL = "ft-shell-v1";
const ASSET = /^(app\.js|style\.css|maplibre-gl\.(?:js|css)|icon[\w.-]*\.(?:png|svg)|ac-[\w-]+\.png|manifest\.webmanifest)$/;

self.addEventListener("install", (e) => {
  self.skipWaiting();
  // keep the page from the start, so the very next launch already opens from the phone
  e.waitUntil(caches.open(SHELL).then((box) => box.add(new Request(self.registration.scope, { cache: "reload" }))).catch(() => {}));
});
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url), scope = new URL(self.registration.scope);
  if (url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname)) return;
  const rel = url.pathname.slice(scope.pathname.length);
  const page = rel === "" || rel === "index.html";
  if (!page && !ASSET.test(rel)) return;
  e.respondWith((async () => {
    const box = await caches.open(SHELL);
    const key = page ? scope.href : req.url;
    const hit = await box.match(key);
    const fresh = fetch(req).then(async (r) => {
      if (r && r.ok) {
        await box.put(key, r.clone());
        if (!page) {   // drop older versions of this file
          for (const k of await box.keys()) {
            const u = new URL(k.url);
            if (u.pathname === url.pathname && u.search !== url.search) await box.delete(k);
          }
        }
      }
      return r;
    });
    if (hit) { e.waitUntil(fresh.catch(() => {})); return hit; }
    return fresh;
  })());
});
