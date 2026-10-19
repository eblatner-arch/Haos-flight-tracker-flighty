// Flight Tracker Flighty: keeps the app itself on the phone, so it opens instantly.
// The page and its files (app.js, style.css, the map library, icons) are answered from the phone's
// copy right away while a fresh copy is fetched in the background for next time. Data (api/...)
// always goes to the add-on. Files are named by their content (?v=...), so an old page always finds
// the files it was built with; older copies are dropped as new ones arrive.
const SHELL = "ft-shell-v1";
const TILES = "ft-tiles-v1";
const TILE = /(^|\.)arcgisonline\.com$|(^|\.)basemaps\.cartocdn\.com$/;
// Data the app reads (flights, history, airports, alerts, map extras): fresh from the add-on when it
// answers, else the last copy on the phone, so every page opens with no signal. Live streams and
// one-off actions are left alone.
const DATA_BOX = "ft-data-v1";
const DATA = /^api\/(state|leg\/|history|hist\/|alerts|airports|nas|places|artcc|airport\/[^/]+\/layout|settings|rules|backup|push\/key|liveries|tail\/)/;
async function dataFirst(req) {
  const box = await caches.open(DATA_BOX);
  try {
    const r = await fetch(req);
    if (r && r.ok) box.put(req, r.clone()).catch(() => {});
    return r;
  } catch (err) {
    const hit = await box.match(req, { ignoreVary: true });
    if (hit) {   // marked, so the app shows it's offline (Live dot gray)
      const h = new Headers(hit.headers); h.set("X-From-Phone", "1");
      return new Response(await hit.blob(), { status: 200, headers: h });
    }
    throw err;
  }
}
const ASSET = /^(app\.js|style\.css|maplibre-gl\.(?:js|css)|leaflet\.(?:js|css)|icon[\w.-]*\.(?:png|svg)|ac-[\w-]+\.png|manifest\.webmanifest)$/;

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
  // Map tiles (Esri satellite, CARTO dark): the phone's copy if it has one, else download and keep it.
  // The trip-map download (app.js) fills the same store ahead of time.
  if (TILE.test(url.hostname)) {
    e.respondWith(caches.open(TILES).then(async (box) => {
      const hit = await box.match(req.url);
      if (hit) return hit;
      const r = await fetch(req);
      if (r && r.ok) box.put(req.url, r.clone()).catch(() => {});
      return r;
    }));
    return;
  }
  if (url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname)) return;
  const rel = url.pathname.slice(scope.pathname.length);
  const page = rel === "" || rel === "index.html";
  if (/^api\/livery\//.test(rel)) {   // aircraft pictures never change: the phone's copy, else fetch and keep
    e.respondWith(caches.open(SHELL).then(async (box) => (await box.match(req)) || fetch(req).then((r) => { if (r.ok) box.put(req, r.clone()); return r; })));
    return;
  }
  if (/^api\/airport\/[^/]+\/layout$/.test(rel)) {   // airport diagrams hardly change: the phone's copy at once, refreshed behind it
    e.respondWith(caches.open(DATA_BOX).then(async (box) => {
      const hit = await box.match(req, { ignoreVary: true });
      const fresh = fetch(req).then((r) => { if (r && r.ok) box.put(req, r.clone()); return r; });
      if (hit) { e.waitUntil(fresh.catch(() => {})); return hit; }
      return fresh;
    }));
    return;
  }
  if (/^api\//.test(rel)) { if (DATA.test(rel)) e.respondWith(dataFirst(req)); return; }
  if (!page && !ASSET.test(rel)) return;
  e.respondWith((async () => {
    const box = await caches.open(SHELL);
    const key = page ? scope.href : req.url;
    const hit = await box.match(key);
    const fresh = fetch(req).then(async (r) => {
      if (r && r.ok) {
        // a new version of the page: tell the open app, so it switches to it (see app.js)
        if (page && hit && hit.headers.get("ETag") && r.headers.get("ETag") && hit.headers.get("ETag") !== r.headers.get("ETag")) {
          const tell = () => self.clients.matchAll({ type: "window" }).then((all) => all.forEach((c) => c.postMessage({ updated: true })));
          setTimeout(tell, 300);
        }
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

// ---------- notifications straight from the add-on ----------
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: "Flighty", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.title || "Flighty", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag, icon: "icon-180.png", badge: "icon-180.png",
    data: { url: d.url || "#/" },
  }));
});
// a tap opens the app on that flight (or brings the open app there)
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = new URL(self.registration.scope);
  target.hash = ((e.notification.data || {}).url || "#/").replace(/^#/, "");
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of all) {
      if (c.url.startsWith(self.registration.scope)) {
        await c.focus();
        try { c.postMessage({ go: target.hash }); } catch {}
        return;
      }
    }
    await self.clients.openWindow(target.href);
  })());
});
