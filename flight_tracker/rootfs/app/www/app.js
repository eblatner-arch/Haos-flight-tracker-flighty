/* Flight Tracker web app. Served through Home Assistant ingress, so every URL is relative. */
(() => {
  "use strict";
  const $root = document.getElementById("app");
  // Views write `$app.innerHTML = …`. On a live update of the same page the new markup is
  // patched into the existing DOM instead of replacing it, so nothing blinks: unchanged
  // elements stay put, only changed text/attributes are touched, and maps are left alone.
  let morphing = false;
  const $app = {
    set innerHTML(html) { if (morphing) morph($root, html); else $root.innerHTML = html; },
    querySelectorAll: (s) => $root.querySelectorAll(s),
    querySelector: (s) => $root.querySelector(s),
  };
  function morph(target, html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    patchChildren(target, tpl.content);
  }
  const keepNode = (x, y) => x.classList && x.classList.contains("leaflet-container") && x.id && x.id === y.id;
  function sameKind(x, y) {
    return x.nodeType === y.nodeType && x.nodeName === y.nodeName && (x.nodeType !== 1 || (x.id || "") === (y.id || ""));
  }
  function patchChildren(a, b) {
    const an = [...a.childNodes], bn = [...b.childNodes];
    bn.forEach((y, i) => {
      const x = an[i];
      if (!x) a.appendChild(y);
      else if (sameKind(x, y)) patchNode(x, y);
      else a.replaceChild(y, x);
    });
    for (let i = bn.length; i < an.length; i++) a.removeChild(an[i]);
  }
  function patchNode(x, y) {
    if (x.nodeType !== 1) { if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue; return; }
    if (keepNode(x, y)) return;
    for (const { name } of [...x.attributes]) if (!y.hasAttribute(name)) x.removeAttribute(name);
    for (const { name, value } of [...y.attributes]) if (x.getAttribute(name) !== value) x.setAttribute(name, value);
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(x.nodeName)) return;
    patchChildren(x, y);
  }
  const $updated = document.getElementById("updated");
  const $refresh = document.getElementById("refresh");
  let STATE = null;
  // Maps survive re-renders: keyed, moved back into the new page, layers repainted in place.
  const MAPS = {};
  const LEGS = {};  // last full copy of each flight, for instant redraws
  let usedMaps = new Set();
  let lastRoute = null;
  let ROUTE = location.hash && location.hash.length > 2 ? location.hash : "#/";
  // Back remembers where you were: each step forward saves the page and its scroll position;
  // "‹" and the edge swipe return to it (same scroll), not to the top.
  const NAV = [];
  let restoreY = null;
  function go(r, opts) {
    r = r || "#/";
    opts = opts || {};
    if (opts.reset) NAV.length = 0;
    else if (r !== ROUTE) { NAV.push({ route: ROUTE, y: window.scrollY }); if (NAV.length > 50) NAV.shift(); }
    ROUTE = r;
    render(false);
  }
  function goBack(fallback) {
    const prev = NAV.pop();
    if (!prev) { ROUTE = fallback || "#/"; return render(false); }
    ROUTE = prev.route;
    restoreY = prev.y;
    return render(false);
  }
  // change the address of the page you're on (search filters) without a step in Back
  function replaceRoute(r) { ROUTE = r; lastRoute = r; }

  const PLANE = '<svg viewBox="0 0 24 24"><path fill="currentColor" stroke="#000" stroke-opacity=".35" stroke-width=".6" d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg>';

  // ---------- helpers ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  function fmt(ts, tz, opts) {
    if (!ts) return "—";
    try { return new Intl.DateTimeFormat(undefined, { timeZone: tz || undefined, ...opts }).format(new Date(ts * 1000)); }
    catch { return new Intl.DateTimeFormat(undefined, opts).format(new Date(ts * 1000)); }
  }
  const hm = (ts, tz) => fmt(ts, tz, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const tzAbbr = (ts, tz) => {
    if (!ts) return "";
    try {
      const p = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "short" }).formatToParts(new Date(ts * 1000));
      return (p.find((x) => x.type === "timeZoneName") || {}).value || "";
    } catch { return ""; }
  };
  const dayLabel = (ts, tz) => fmt(ts, tz, { weekday: "short", day: "numeric", month: "short" });
  const relDay = (ts, tz) => {
    const d = (x) => fmt(x, tz, { year: "numeric", month: "2-digit", day: "2-digit" });
    const now = Date.now() / 1000;
    if (d(ts) === d(now)) return "Today";
    if (d(ts) === d(now + 86400)) return "Tomorrow";
    return "";
  };
  function dur(sec) {
    if (sec == null || !isFinite(sec)) return "";
    const neg = sec < 0; sec = Math.abs(Math.round(sec / 60));
    const h = Math.floor(sec / 60), m = sec % 60;
    return (neg ? "-" : "") + (h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`);
  }
  const ago = (ts) => {
    if (!ts) return "never";
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    return `${Math.round(s / 3600)} h ago`;
  };
  const best = (t, k) => t && t[k] ? (t[k].actual || t[k].estimated || t[k].scheduled) : null;
  const slug = (s) => String(s || "").toLowerCase().replace(/[^a-z]+/g, "-");
  const destroyMaps = () => { usedMaps = new Set(); };
  function sweepMaps() {
    for (const k of Object.keys(MAPS)) {
      if (!usedMaps.has(k)) { try { MAPS[k].map.remove(); } catch {} delete MAPS[k]; }
    }
  }

  // Map dragging is decided per touch: before anything else sees a touch, each map's dragging is
  // switched on only if the finger landed inside that map's visible box. A swipe that starts
  // anywhere else can't move a map, whatever iOS decides the touch hit.
  const EDGE = 24;  // px from the left edge where a swipe means "back"
  function gateMapDrag(e) {
    const p = e.touches ? e.touches[0] : e;
    if (!p) return;
    const edge = e.touches && p.clientX <= EDGE && document.querySelector("#app > .back");
    for (const m of Object.values(MAPS)) {
      if (!m.interactive || !m.map.dragging) continue;
      const r = m.el.getBoundingClientRect();
      const inside = !edge && p.clientX >= r.left && p.clientX <= r.right && p.clientY >= r.top && p.clientY <= r.bottom;
      if (inside && !m.map.dragging.enabled()) m.map.dragging.enable();
      else if (!inside && m.map.dragging.enabled()) m.map.dragging.disable();
    }
  }
  for (const ev of ["touchstart", "pointerdown", "mousedown"]) document.addEventListener(ev, gateMapDrag, { capture: true, passive: true });

  // Swipe in from the left edge to go back (to wherever the page's "‹" link points), like iOS.
  let sw = null;
  document.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    const back = document.querySelector("#app > .back");
    sw = e.touches.length === 1 && back && t.clientX <= EDGE ? { x: t.clientX, y: t.clientY, dx: 0, back, horiz: null } : null;
  }, { capture: true, passive: true });
  document.addEventListener("touchmove", (e) => {
    if (!sw) return;
    const t = e.touches[0], dx = t.clientX - sw.x, dy = t.clientY - sw.y;
    if (sw.horiz === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) sw.horiz = Math.abs(dx) > Math.abs(dy);
    if (!sw.horiz) { if (sw.horiz === false) { $root.style.transform = ""; sw = null; } return; }
    if (e.cancelable) e.preventDefault();  // keep the page from scrolling sideways while swiping
    sw.dx = Math.max(0, dx);
    if (sw.raf) return;
    sw.raf = requestAnimationFrame(() => {  // one paint per frame, following the finger 1:1
      if (!sw) return;
      sw.raf = 0;
      $root.style.transition = "none";
      $root.style.willChange = "transform";
      $root.style.transform = `translate3d(${sw.dx}px,0,0)`;
      $root.style.opacity = String(1 - Math.min(0.35, sw.dx / 900));
    });
  }, { capture: true, passive: false });
  document.addEventListener("touchend", () => {
    if (!sw) return;
    const go_ = sw.horiz && sw.dx > Math.min(110, window.innerWidth * 0.28), href = sw.back.getAttribute("href");
    if (sw.raf) cancelAnimationFrame(sw.raf);
    sw = null;
    if (!go_) {
      $root.style.transition = "transform .2s cubic-bezier(.2,.8,.2,1), opacity .2s";
      $root.style.transform = ""; $root.style.opacity = "";
      return;
    }
    // slide the page off, then show the one underneath sliding in from a little to the left
    $root.style.transition = "transform .16s ease-out, opacity .16s ease-out";
    $root.style.transform = `translate3d(${window.innerWidth}px,0,0)`; $root.style.opacity = "0.6";
    setTimeout(async () => {
      $root.style.transition = "none";
      $root.style.transform = "translate3d(-24px,0,0)"; $root.style.opacity = "0";
      await Promise.race([goBack(href), new Promise((r) => setTimeout(r, 1500))]);
      requestAnimationFrame(() => {
        $root.style.transition = "transform .2s cubic-bezier(.2,.8,.2,1), opacity .2s";
        $root.style.transform = ""; $root.style.opacity = ""; $root.style.willChange = "";
      });
    }, 150);
  }, { capture: true, passive: true });
  document.addEventListener("touchcancel", () => { sw = null; $root.style.transform = ""; $root.style.opacity = ""; }, { capture: true, passive: true });

  async function api(path, opts) {
    // GETs revalidate (the add-on answers "not changed" with a tiny 304); changes always go through
    const r = await fetch(path, { cache: opts && opts.method && opts.method !== "GET" ? "no-store" : "no-cache", ...opts });
    if (!r.ok) throw new Error(`${r.status}`);
    if (opts && opts.method === "POST") changed(path);
    return r.json();
  }
  // Anything you change here turns the Drive pill amber right away (the server confirms and uploads ~15 s later).
  function changed(path) {
    if (/api\/(backup|refresh|test-notify)/.test(path) || typeof setDrive !== "function") return;
    const el = document.getElementById("drive");
    if (el && !el.hidden && !el.classList.contains("disconnected")) setDrive({ ...(LAST_DRIVE || {}), state: "waiting" });
    setTimeout(driveSoon, 600);
  }

  // ---------- map ----------
  function gcPoints(a, b, n = 64) {
    const R = Math.PI / 180, toD = 180 / Math.PI;
    const [la1, lo1, la2, lo2] = [a[0] * R, a[1] * R, b[0] * R, b[1] * R];
    const d = 2 * Math.asin(Math.sqrt(Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2));
    if (!d) return [a, b];
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
      const x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
      const y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
      const z = A * Math.sin(la1) + B * Math.sin(la2);
      pts.push([Math.atan2(z, Math.sqrt(x * x + y * y)) * toD, Math.atan2(y, x) * toD]);
    }
    return pts;
  }
  function bearing(p, q) {
    const R = Math.PI / 180;
    const y = Math.sin((q[1] - p[1]) * R) * Math.cos(q[0] * R);
    const x = Math.cos(p[0] * R) * Math.sin(q[0] * R) - Math.sin(p[0] * R) * Math.cos(q[0] * R) * Math.cos((q[1] - p[1]) * R);
    return (Math.atan2(y, x) / R + 360) % 360;
  }
  const ll = (ap) => ap && ap.lat != null && ap.lon != null ? [ap.lat, ap.lon] : null;

  function planeMarker(pos, heading, color, title) {
    const icon = L.divIcon({
      className: "plane-icon",
      html: `<div style="color:${color};transform:rotate(${heading || 0}deg)">${PLANE}</div>`,
      iconSize: [30, 30], iconAnchor: [15, 15],
    });
    return L.marker(pos, { icon, title, zIndexOffset: 1000 });
  }

  // Basemaps: CARTO's OpenStreetMap tiles (kept current: new terminals, taxiways; detailed to zoom 20).
  function addTiles(map, dark) {
    // CARTO now watermarks tiles fetched without its free key. With your key (add-on Configuration)
    // tiles come from its keyed address (?key=…); a tile address of your own wins: {style} becomes
    // dark_all or light_all, {key} your key. If tiles keep failing it steps down: keyless CARTO,
    // then Esri's gray map (OpenStreetMap's own servers refuse apps like this one).
    const mt = (STATE && STATE.map_tiles) || {};
    const style = dark ? "dark_all" : "light_all";
    const esri = (name) => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${name}/MapServer/tile/{z}/{y}/{x}`;
    const carto = "&copy; OpenStreetMap contributors &copy; CARTO";
    const chain = [];
    if (mt.url) chain.push([mt.url.replace("{style}", style).replace("{key}", encodeURIComponent(mt.key || "")), { maxZoom: 20, subdomains: "abcd", attribution: carto }]);
    else if (mt.key) chain.push([`https://basemaps.cartocdn.com/rastertiles/${style}/{z}/{x}/{y}.png?key=${encodeURIComponent(mt.key)}`, { maxZoom: 20, attribution: carto }]);
    chain.push([`https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png`, { maxZoom: 20, subdomains: "abcd", attribution: carto }]);
    chain.push([esri(dark ? "World_Dark_Gray_Base" : "World_Light_Gray_Base"), { maxZoom: 20, maxNativeZoom: 16, attribution: "Tiles &copy; Esri" }]);
    let i = 0, layer = null, errors = 0, loaded = 0;
    const use = () => {
      if (layer) map.removeLayer(layer);
      const [url, opts] = chain[i];
      errors = 0; loaded = 0;
      layer = L.tileLayer(url, opts).addTo(map);
      layer.on("tileload", () => { loaded++; });
      layer.on("tileerror", () => {
        // a few bad tiles happen; give up on a source only when it loads nothing
        if (++errors >= 4 && loaded === 0 && i < chain.length - 1) { i++; use(); }
      });
    };
    use();
  }

  // Airport layout labels: zoomed in on one of the flight's airports, runways, terminals, taxiways
  // and gates are labelled (OpenStreetMap data, fetched once per airport by the add-on). Smaller
  // things appear as you zoom further: runways and terminals from 14, taxiways 15, gates 16.
  const LAYOUTS = {};
  const LOG_ALL = new Set();   // flights whose whole lookup log you asked to see
  function kmBetween(a, b) {
    const R = Math.PI / 180, x = (b[1] - a[1]) * R * Math.cos((a[0] + b[0]) / 2 * R), y = (b[0] - a[0]) * R;
    return Math.hypot(x, y) * 6371;
  }
  function layoutZoom(entry) {
    const z = entry.map.getZoom(), c = entry.map.getContainer();
    for (const n of [8, 15, 16, 17]) c.classList.toggle(`z${n}`, z >= n);
  }
  async function layoutCheck(entry) {
    const map = entry.map, leg = entry.leg;
    if (!leg) return;
    layoutZoom(entry);
    if (map.getZoom() < 14) { if (entry.layout && map.hasLayer(entry.layout)) map.removeLayer(entry.layout); return; }
    const c = map.getCenter(), here = [c.lat, c.lng];
    const ap = [leg.dep_ap, leg.arr_ap].map((a) => a && ll(a) ? { code: a.iata || a.icao, pos: ll(a) } : null)
      .filter((a) => a && a.code && kmBetween(here, a.pos) < 8)[0];
    if (!ap) return;
    if (entry.layoutFor === ap.code) {
      if (!map.hasLayer(entry.layout)) entry.layout.addTo(map);
      if (entry.layout.rwyWidth) entry.layout.rwyWidth(map);
      return;
    }
    if (!LAYOUTS[ap.code]) {
      LAYOUTS[ap.code] = api(`api/airport/${encodeURIComponent(ap.code)}/layout`).catch(() => { delete LAYOUTS[ap.code]; return null; });
    }
    const d = await LAYOUTS[ap.code];
    if (!d || !d.features || entry.layoutFor === ap.code) return;
    if (entry.layout) map.removeLayer(entry.layout);
    entry.layout = paintLayout(d.features).addTo(map);
    entry.layoutFor = ap.code;
    if (entry.layout.rwyWidth) entry.layout.rwyWidth(map);
  }
  function paintLayout(features) {
    const g = L.layerGroup(), muted = css("--muted"), faint = css("--faint");
    const lbl = (pos, text, cls) => g.addLayer(L.marker(pos, { interactive: false, keyboard: false,
      icon: L.divIcon({ className: "", iconSize: null, html: `<span class="ap-lbl ${cls}">${esc(text)}</span>` }) }));
    const mid = (pts) => {
      let tot = 0; const seg = [];
      for (let i = 1; i < pts.length; i++) { const d = kmBetween(pts[i - 1], pts[i]); seg.push(d); tot += d; }
      let half = tot / 2;
      for (let i = 0; i < seg.length; i++) {
        if (half <= seg[i]) { const t = seg[i] ? half / seg[i] : 0; return [[pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t], tot]; }
        half -= seg[i];
      }
      return [pts[0], tot];
    };
    const centroid = (pts) => [pts.reduce((a, p) => a + p[0], 0) / pts.length, pts.reduce((a, p) => a + p[1], 0) / pts.length];
    const placed = {};
    const near = (ref, p, km) => (placed[ref] || []).some((q) => kmBetween(p, q) < km);
    for (const f of features) {
      if (f.k === "terminal") {
        g.addLayer(L.polygon(f.g, { color: muted, weight: 1, opacity: 0.6, fillColor: muted, fillOpacity: 0.12, interactive: false }));
        if (f.name || f.ref) lbl(centroid(f.g), f.name || f.ref, "ap-term");
      }
    }
    // Runways drawn on top of the basemap, about their real width (45 m) at every zoom, with a
    // dashed centerline, so they stand out from the taxiways on the dark map.
    const rwys = [], twys = [];
    for (const f of features) {
      if (f.k !== "runway" || !Array.isArray(f.g[0])) continue;
      const body = L.polyline(f.g, { color: css("--text"), opacity: 0.22, weight: 8, lineCap: "butt", interactive: false });
      const cl = L.polyline(f.g, { color: css("--text"), opacity: 0.55, weight: 1, dashArray: "8 8", interactive: false });
      g.addLayer(body); g.addLayer(cl); rwys.push(body);
    }
    g.rwyWidth = (map) => {
      const z = map.getZoom(), lat = map.getCenter().lat;
      const mpp = 156543.03 * Math.cos(lat * Math.PI / 180) / Math.pow(2, z);
      const w = Math.max(3, Math.min(60, 45 / mpp));
      rwys.forEach((r) => r.setStyle({ weight: w }));
      // taxiways about 23 m wide, with the yellow centerline painted on them
      const tw = Math.max(2, Math.min(30, 23 / mpp));
      twys.forEach((r) => r.setStyle({ weight: tw }));
    };
    for (const f of features) {
      if (f.k === "runway" && f.ref) lbl(mid(f.g)[0], f.ref.replace(/^0/, "").replace(/\/0/, "/"), "ap-rwy");
      else if (f.k === "taxiway" && f.ref) {
        const body = L.polyline(f.g, { color: css("--text"), opacity: 0.12, weight: 4, lineJoin: "round", interactive: false });
        g.addLayer(body); twys.push(body);
        g.addLayer(L.polyline(f.g, { color: css("--amber"), opacity: 0.45, weight: 1, interactive: false, className: "twy-cl" }));
        const [p, len] = mid(f.g);
        if (len < 0.08 || near(f.ref, p, 0.35)) continue;
        (placed[f.ref] = placed[f.ref] || []).push(p);
        lbl(p, f.ref, "ap-twy");
      } else if ((f.k === "gate" || f.k === "stand") && f.ref && !Array.isArray(f.g[0])) {
        if (near("gate:" + f.ref, f.g, 0.05)) continue;
        (placed["gate:" + f.ref] = placed["gate:" + f.ref] || []).push(f.g);
        lbl(f.g, f.ref, "ap-gate");
      }
    }
    return g;
  }

  function drawMap(el, leg, { interactive = true, key = null } = {}) {
    if (!el) return;
    if (!window.L) { el.innerHTML = '<div class="empty small">Map library didn\'t load.</div>'; return; }
    key = key || el.id;
    const have = MAPS[key];
    if (!have && el.classList.contains("leaflet-container")) {
      // a kept map element now belongs to a different flight: swap in a clean container
      const f = document.createElement("div");
      f.id = el.id; f.className = el.dataset.cls || "";
      el.replaceWith(f); el = f;
    }
    if (have) {
      if (have.el !== el) el.replaceWith(have.el);
      usedMaps.add(key);
      paintMap(have, leg, false);
      setTimeout(() => have.map.invalidateSize(), 30);
      return;
    }
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    el.dataset.cls = el.className;
    // padding 0: the route layer is exactly the map's size instead of 10% larger
    const map = L.map(el, { renderer: L.svg({ padding: 0 }), zoomControl: interactive, attributionControl: true, dragging: interactive,
      scrollWheelZoom: interactive, touchZoom: interactive, doubleClickZoom: interactive, boxZoom: false, keyboard: false });
    if (map.attributionControl) map.attributionControl.setPrefix("");   // no "Leaflet" flag; the map credits stay
    addTiles(map, dark);
    const entry = { el, map, layer: L.layerGroup().addTo(map), bounds: [], interactive };
    entry.center = () => entry.bounds.length ? map.fitBounds(entry.bounds, { padding: [28, 28], maxZoom: 9 }) : map.setView([39.5, -98.35], 3);
    MAPS[key] = entry;
    usedMaps.add(key);
    if (interactive && L.Control) {
      const Btn = L.Control.extend({
        options: { position: "topright" },
        onAdd() {
          const b = L.DomUtil.create("button", "map-center");
          b.type = "button"; b.title = "Center on route"; b.setAttribute("aria-label", "Center on route");
          b.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm8.94 3A8.99 8.99 0 0 0 13 3.06V1h-2v2.06A8.99 8.99 0 0 0 3.06 11H1v2h2.06A8.99 8.99 0 0 0 11 20.94V23h2v-2.06A8.99 8.99 0 0 0 20.94 13H23v-2h-2.06zM12 19a7 7 0 1 1 0-14 7 7 0 0 1 0 14z"/></svg>';
          L.DomEvent.disableClickPropagation(b);
          L.DomEvent.on(b, "click", (e) => { L.DomEvent.preventDefault(e); entry.center(); });
          return b;
        },
      });
      map.addControl(new Btn());
      // Jump to the departure or arrival airport, zoomed in enough to see the surface traffic.
      const TAKEOFF = '<svg viewBox="0 0 24 24"><path d="M2.5 19h19v2h-19zm19.57-9.36c-.21-.8-1.04-1.28-1.84-1.06L14.92 10l-6.9-6.43-1.93.51 4.14 7.17-4.97 1.33-1.97-1.54-1.45.39 1.82 3.16.77 1.33 1.6-.43 5.31-1.42 4.35-1.16L21 11.49c.81-.23 1.28-1.05 1.07-1.85z"/></svg>';
      const LANDING = '<svg viewBox="0 0 24 24"><path d="M2.5 19h19v2h-19zm7.18-5.73 4.35 1.16 5.31 1.42c.8.21 1.62-.26 1.84-1.06.21-.8-.26-1.62-1.06-1.84l-5.31-1.42-2.76-9.02L10.12 2v8.28L5.15 8.95l-.93-2.32-1.45-.39v5.17l1.6.43 5.31 1.43z"/></svg>';
      for (const [side, svg, what] of [["dep_ap", TAKEOFF, "departure"], ["arr_ap", LANDING, "arrival"]]) {
        const Go = L.Control.extend({
          options: { position: "topright" },
          onAdd() {
            const b = L.DomUtil.create("button", "map-center");
            b.type = "button"; b.title = `Zoom to the ${what} airport`; b.setAttribute("aria-label", b.title);
            b.innerHTML = svg; b.style.marginTop = "6px";
            L.DomEvent.disableClickPropagation(b);
            L.DomEvent.on(b, "click", (e) => {
              L.DomEvent.preventDefault(e);
              const ap = entry.leg && entry.leg[side], p = ll(ap);
              if (p) map.setView(p, 15);
            });
            return b;
          },
        });
        map.addControl(new Go());
      }
      const Xp = L.Control.extend({
        options: { position: "topright" },
        onAdd() {
          const b = L.DomUtil.create("button", "map-center xpdr");
          b.type = "button"; b.setAttribute("aria-label", "Transponder filter");
          b.textContent = "XPDR";
          b.style.display = "none"; b.style.marginTop = "6px";
          L.DomEvent.disableClickPropagation(b);
          L.DomEvent.on(b, "click", (e) => {
            L.DomEvent.preventDefault(e);
            try { localStorage.setItem("ft-surf-all", localStorage.getItem("ft-surf-all") === "1" ? "0" : "1"); } catch {}
            if (entry.lastSurface) paintSurface(entry, entry.lastSurface);
          });
          entry.xpdrBtn = b;
          return b;
        },
      });
      map.addControl(new Xp());
      const Id = L.Control.extend({
        options: { position: "topright" },
        onAdd() {
          const b = L.DomUtil.create("button", "map-center xpdr");
          b.type = "button"; b.setAttribute("aria-label", "Show callsigns");
          b.textContent = "ID";
          b.style.display = "none"; b.style.marginTop = "6px";
          L.DomEvent.disableClickPropagation(b);
          L.DomEvent.on(b, "click", (e) => {
            L.DomEvent.preventDefault(e);
            try { localStorage.setItem("ft-surf-ids", localStorage.getItem("ft-surf-ids") === "1" ? "0" : "1"); } catch {}
            if (entry.lastSurface) paintSurface(entry, entry.lastSurface);
          });
          entry.idBtn = b;
          return b;
        },
      });
      map.addControl(new Id());
    }
    if (interactive) {
      oneFingerZoom(map, el);
      map.on("zoomend moveend", () => { layoutCheck(entry); surfaceNow(entry); });
      map.on("zoomend", () => { if (entry.lastSurface) paintSurface(entry, entry.lastSurface); });
    }
    paintMap(entry, leg, true);
    setTimeout(() => map.invalidateSize(), 50);
  }

  // Double-tap and hold, then slide down to zoom in or up to zoom out (like Apple and Google Maps);
  // a plain double tap zooms in one step. Driven the way Leaflet drives a pinch: the tiles already on
  // screen are scaled while your finger moves (nothing reloads mid-gesture, so the map never blanks),
  // then it settles on a whole zoom level with Leaflet's own zoom animation.
  function oneFingerZoom(map, el) {
    map.doubleClickZoom.disable();
    let last = 0, lastPt = null, zt = null;
    const target = (z) => {  // keep the spot under your finger where it is
      const size = map.getSize().divideBy(2);
      return map.unproject(map.project(zt.ll, z).subtract(zt.pt.subtract(size)), z);
    };
    el.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) { zt = null; return; }
      const t = e.touches[0], now = Date.now();
      if (now - last < 320 && lastPt && Math.hypot(t.clientX - lastPt.x, t.clientY - lastPt.y) < 40) {
        const pt = map.mouseEventToContainerPoint(t);
        zt = { y: t.clientY, z0: map.getZoom(), z: map.getZoom(), moved: false, pt, ll: map.containerPointToLatLng(pt), started: false };
        if (map.dragging) map.dragging.disable();
        e.preventDefault(); e.stopPropagation();
        last = 0;
      } else { last = now; lastPt = { x: t.clientX, y: t.clientY }; }
    }, { capture: true, passive: false });
    el.addEventListener("touchmove", (e) => {
      if (!zt) return;
      e.preventDefault(); e.stopPropagation();
      const dy = e.touches[0].clientY - zt.y;
      if (!zt.moved && Math.abs(dy) < 5) return;
      if (!zt.started) { map._stop(); map._moveStart(true, false); zt.started = true; }
      zt.moved = true;
      zt.z = Math.max(map.getMinZoom(), Math.min(map.getMaxZoom(), zt.z0 + dy / 90));
      const c = target(zt.z), z = zt.z;
      cancelAnimationFrame(zt.raf);
      zt.raf = requestAnimationFrame(() => map._move(c, z, { pinch: true, round: false }));
    }, { capture: true, passive: false });
    const end = (e) => {
      if (!zt) return;
      const g = zt;
      zt = null;
      cancelAnimationFrame(g.raf);
      if (e.cancelable) e.preventDefault();
      e.stopPropagation();
      if (!g.moved) { map.setZoomAround(g.pt, Math.min(map.getMaxZoom(), Math.round(g.z0) + 1)); return; }
      zt = g;
      const z = map._limitZoom(Math.round(g.z)), c = target(z);
      zt = null;
      if (map._zoomAnimated) map._animateZoom(c, z, true, map.options.zoomSnap);
      else map._resetView(c, z);
    };
    el.addEventListener("touchend", end, { capture: true, passive: false });
    el.addEventListener("touchcancel", end, { capture: true, passive: false });
  }


  // Live surface traffic (FAA STDDS) on a flight's map while it's on the ground at an airport with
  // surface radar: everything on the field, your flight highlighted. Polls only while that
  // flight's page is open: every 3 s while there's traffic to show, every 20 s otherwise.
  const SURF = { id: null, timer: null, zoomed: false };
  function stopSurface() {
    if (SURF.timer) clearTimeout(SURF.timer);
    SURF.timer = null; SURF.id = null;
    for (const m of Object.values(MAPS)) { if (m.surface) m.surface.clearLayers(); if (m.aptBtn) m.aptBtn.style.display = "none"; if (m.xpdrBtn) m.xpdrBtn.style.display = "none"; if (m.idBtn) m.idBtn.style.display = "none"; }
  }
  function surfaceWatch(leg) {
    if (!STATE || !STATE.stdds || STATE.stdds.status !== "connected") { stopSurface(); return; }
    if (SURF.id !== leg.id) { stopSurface(); SURF.id = leg.id; SURF.zoomed = false; }
    if (SURF.timer) return;
    const id = leg.id;
    const tick = async () => {
      SURF.timer = null;
      if (SURF.id !== id || !ROUTE.startsWith(`#/leg/${id}`)) { if (SURF.id === id) stopSurface(); return; }
      let d = null;
      const ent = MAPS[`leg-${id}`];
      const ap = ent ? zoomedAirport(ent, 13) : null;
      try { d = await api(`api/surface/${encodeURIComponent(id)}${ap ? `?ap=${encodeURIComponent(ap)}` : ""}`); } catch {}
      if (SURF.id !== id) return;
      const entry = MAPS[`leg-${id}`];
      if (entry) paintSurface(entry, d || {});
      SURF.timer = setTimeout(tick, d && d.targets && d.targets.length ? 3000 : 20000);
    };
    SURF.timer = setTimeout(tick, 0);
  }
  // The flight airport the map is zoomed in on (center within 8 km, at least zoom z), or null.
  function zoomedAirport(entry, z) {
    const leg = entry.leg;
    if (!leg || entry.map.getZoom() < z) return null;
    const c = entry.map.getCenter(), here = [c.lat, c.lng];
    const a = [leg.dep_ap, leg.arr_ap].find((a) => a && ll(a) && kmBetween(here, ll(a)) < 8);
    return a ? a.iata || a.icao : null;
  }
  // zoomed in on an airport: look for surface traffic now rather than at the next 20 s poll
  function surfaceNow(entry) {
    if (!SURF.id || !entry.leg || entry.leg.id !== SURF.id || !zoomedAirport(entry, 13)) return;
    if (entry.surfAp === zoomedAirport(entry, 13)) return;
    entry.surfAp = zoomedAirport(entry, 13);
    if (SURF.timer) clearTimeout(SURF.timer);
    SURF.timer = null;
    const l = entry.leg;
    SURF.id = null;
    surfaceWatch(l);
  }

  function paintSurface(entry, d) {
    if (!entry.surface) entry.surface = L.layerGroup().addTo(entry.map);
    entry.surface.clearLayers();
    let only = true, ids = false;
    try { only = localStorage.getItem("ft-surf-all") !== "1"; ids = localStorage.getItem("ft-surf-ids") === "1"; } catch {}
    if (entry.idBtn) {
      entry.idBtn.classList.toggle("on", ids);
      entry.idBtn.title = ids ? "Hide callsigns" : "Show callsigns";
    }
    const all = (d.targets || []).filter((t) => t.lat != null && t.lon != null);
    const tg = only ? all.filter((t) => t.mine || (t.xpdr && t.kind !== "vehicle")) : all;
    if (entry.xpdrBtn) {
      entry.xpdrBtn.style.display = all.length ? "" : "none";
      if (entry.idBtn) entry.idBtn.style.display = all.length ? "" : "none";
      entry.xpdrBtn.classList.toggle("on", only);
      entry.xpdrBtn.title = only ? "Showing aircraft with transponders on (tap for everything)" : "Showing everything on the surface (tap for transponders only)";
    }
    entry.airport = all.length && d.center ? d.center : null;
    entry.lastSurface = d;
    if (entry.aptBtn) entry.aptBtn.style.display = entry.airport ? "" : "none";
    if (!SURF.zoomed && entry.interactive && entry.airport && ["taxi_out", "landed"].includes(d.phase)) {
      const me = all.find((t) => t.mine);
      entry.map.setView(me ? [me.lat, me.lon] : entry.airport, 15);
      SURF.zoomed = true;
    }
    // zoomed out past the airport, surface traffic is just clutter over the route (and its tags
    // pile up): draw it from zoom 12 in
    if (entry.map.getZoom() < 12) {
      entry.surfaceMine = 0;
      if (entry.ownMarker) entry.ownMarker.setOpacity(1);
      return;
    }
    const mine = tg.some((t) => t.mine);
    entry.surfaceMine = mine ? Date.now() : 0;
    if (entry.ownMarker) entry.ownMarker.setOpacity(mine ? 0 : 1);
    if (!tg.length) return;
    const route = css("--route"), muted = css("--muted");
    for (const t of tg) {
      const label = [t.cs, t.type].filter(Boolean).join(" · ") || (t.kind === "vehicle" ? "Vehicle" : "Unidentified");
      if (!t.mine && (t.kind === "vehicle" || (!t.cs && !t.type))) {
        entry.surface.addLayer(L.circleMarker([t.lat, t.lon], { radius: 2.5, color: muted, fillColor: muted, fillOpacity: 0.8, weight: 0 }).bindTooltip(label, { className: "surf-tip" }));
        continue;
      }
      const sz = t.mine ? 22 : 15;
      const icon = L.divIcon({ className: "surf-icon", iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2],
        html: `<div style="width:${sz}px;height:${sz}px;color:${t.mine ? route : css("--text")};opacity:${t.mine ? 1 : 0.85};transform:rotate(${t.hdg || 0}deg)">${PLANE}</div>` });
      const m = L.marker([t.lat, t.lon], { icon, zIndexOffset: t.mine ? 1200 : 500 });
      if (t.mine) m.bindTooltip(label, { direction: "top", offset: [0, -sz / 2], permanent: true, className: "surf-tip" });
      else if (ids && (t.cs || t.squawk)) m.bindTooltip(t.cs || t.squawk, { direction: "right", offset: [sz / 2, 0], permanent: true, className: "surf-tip surf-id" });
      else m.bindTooltip(label, { direction: "top", offset: [0, -sz / 2], className: "surf-tip" });
      entry.surface.addLayer(m);
    }
  }

  function paintMap(entry, leg, fit) {
    const { layer, interactive } = entry;
    entry.leg = leg;
    layer.clearLayers();
    const add = (x) => x.addTo(layer);
    const route = css("--route"), violet = css("--violet"), muted = css("--faint");
    const bounds = [];
    const A = ll(leg.dep_ap), B = ll(leg.arr_ap);
    const f = leg.flight || {};
    // The route ahead: the FAA's trajectory along the route ATC has cleared (it follows every
    // amendment), else FlightAware's filed waypoints. In the air it's drawn from the airplane on,
    // so flown track + where it is + the (new) route ahead read as one line.
    const atc = f.swim_route && (f.swim_route.pts || []).length > 1 ? f.swim_route.pts : null;
    const wps = atc || (f.waypoints || []).filter((p) => p[0] != null);
    let here = null;
    if (leg.live && ["airborne", "taxi_out"].includes(leg.phase)) here = [leg.live.lat, leg.live.lon];
    else if (leg.phase === "airborne") { const tr = (f.track || []).filter((p) => p[0] != null); if (tr.length) here = [tr[tr.length - 1][0], tr[tr.length - 1][1]]; }
    if (wps.length > 1) {
      let ahead = wps;
      if (here && leg.phase === "airborne") {
        let bi = 0, bd = Infinity;
        wps.forEach((p, i) => { const d = kmBetween(here, p); if (d < bd) { bd = d; bi = i; } });
        ahead = [here, ...wps.slice(bi + 1)];
      }
      add(L.polyline(ahead, { color: route, weight: 2, opacity: atc ? 0.75 : 0.55, dashArray: "6 6" }));
      wps.forEach((p) => bounds.push(p));
      if (atc && interactive) {
        for (const [la, lo, name] of (f.swim_route.fixes || []).slice(1, -1)) {
          add(L.circleMarker([la, lo], { radius: 1.5, color: route, weight: 0, fillColor: route, fillOpacity: 0.8, interactive: false }));
          add(L.marker([la, lo], { interactive: false, keyboard: false,
            icon: L.divIcon({ className: "", iconSize: null, html: `<span class="fix-lbl">${esc(name)}</span>` }) }));
        }
      }
    } else if (A && B) {
      const g = gcPoints(A, B);
      add(L.polyline(g, { color: muted, weight: 2, opacity: 0.8, dashArray: "4 7" }));
      g.forEach((p) => bounds.push(p));
    }
    const track = (f.track || []).filter((p) => p[0] != null);
    if (track.length > 1) add(L.polyline(track.map((p) => [p[0], p[1]]), { color: route, weight: 3.5, opacity: 0.95 }));
    // the airplane's live position is fresher than the drawn track: join them so it sits on its line
    if (leg.live && track.length && ["airborne", "taxi_out", "landed"].includes(leg.phase))
      add(L.polyline([[track[track.length - 1][0], track[track.length - 1][1]], [leg.live.lat, leg.live.lon]], { color: route, weight: 3.5, opacity: 0.95 }));
    const inb = leg.inbound;
    // The inbound (and the airplane's earlier legs) only frame the map while they still matter:
    // not yet arrived and your flight not yet off the gate. Afterwards they're drawn faintly and
    // centering is on your own route.
    const inbOpen = !!inb && !["arrived", "cancelled", "diverted"].includes(inb.phase) && leg.phase === "scheduled";
    const ibounds = inbOpen ? bounds : [];
    if (inb) {
      const ia = ll(inb.origin), itr = (inb.track || []).filter((p) => p[0] != null);
      if (itr.length > 1) add(L.polyline(itr.map((p) => [p[0], p[1]]), { color: violet, weight: 2.5, opacity: inbOpen ? 0.9 : 0.35 }));
      if (leg.inbound_live && itr.length) add(L.polyline([[itr[itr.length - 1][0], itr[itr.length - 1][1]], [leg.inbound_live.lat, leg.inbound_live.lon]], { color: violet, weight: 2.5, opacity: inbOpen ? 0.9 : 0.35 }));
      const iw = (inb.waypoints || []).filter((p) => p[0] != null);
      if (iw.length > 1) add(L.polyline(iw, { color: violet, weight: 1.5, opacity: 0.45, dashArray: "5 6" }));
      else if (ia && A) add(L.polyline(gcPoints(ia, A), { color: violet, weight: 1.5, opacity: 0.45, dashArray: "5 6" }));
      let ip = null, ih = 0;
      if (leg.inbound_live) { ip = [leg.inbound_live.lat, leg.inbound_live.lon]; ih = leg.inbound_live.heading; }
      else if (inb.phase === "airborne" && itr.length) { ip = itr[itr.length - 1]; ih = itr.length > 1 ? bearing(itr[itr.length - 2], ip) : 0; }
      if (ip) { add(planeMarker([ip[0], ip[1]], ih, violet, "Inbound")); ibounds.push([ip[0], ip[1]]); }
      if (ia && interactive) { ibounds.push(ia); add(L.marker(ia, { icon: L.divIcon({ className: "", html: `<span class="apt-label" style="border-color:${violet}">${esc(inb.origin.iata || "")}</span>`, iconSize: null }) })); }
    }
    // Earlier legs of the same airplane that still matter (not yet arrived), fainter the further back.
    (leg.chain || []).forEach((c, i) => {
      const ca = ll(c.origin), cb = ll(c.destination), op = Math.max(0.35, 0.75 - i * 0.15);
      const ct = (c.track || []).filter((p) => p[0] != null);
      if (ct.length > 1) add(L.polyline(ct.map((p) => [p[0], p[1]]), { color: violet, weight: 2, opacity: op }));
      const cw = (c.waypoints || []).filter((p) => p[0] != null);
      if (cw.length > 1) add(L.polyline(cw, { color: violet, weight: 1.3, opacity: op * 0.6, dashArray: "3 6" }));
      else if (ca && cb) add(L.polyline(gcPoints(ca, cb), { color: violet, weight: 1.3, opacity: op * 0.6, dashArray: "3 6" }));
      if (c.phase === "airborne" && ct.length) {
        const p = ct[ct.length - 1];
        add(planeMarker([p[0], p[1]], ct.length > 1 ? bearing(ct[ct.length - 2], p) : 0, violet, c.display_ident || "Earlier leg"));
        ibounds.push([p[0], p[1]]);
      }
      if (ca) { ibounds.push(ca); add(L.marker(ca, { icon: L.divIcon({ className: "", html: `<span class="apt-label faint" style="border-color:${violet}">${esc(c.origin.iata || "")}</span>`, iconSize: null }) })); }
    });
    for (const [ap, pos] of [[leg.dep_ap, A], [leg.arr_ap, B]]) {
      if (!pos) continue;
      bounds.push(pos);
      add(L.circleMarker(pos, { radius: 4, color: route, fillColor: route, fillOpacity: 1, weight: 0 }));
      add(L.marker(pos, { icon: L.divIcon({ className: "", html: `<span class="apt-label">${esc(ap.iata || "")}</span>`, iconSize: null, iconAnchor: [-6, 8] }) }));
    }
    let pos = null, hdg = 0;
    if (leg.live) { pos = [leg.live.lat, leg.live.lon]; hdg = leg.live.heading; }
    else if (["airborne", "taxi_out", "landed"].includes(leg.phase) && track.length) {
      pos = track[track.length - 1]; hdg = track.length > 1 ? bearing(track[track.length - 2], pos) : 0;
    } else if (f.last_position && ["airborne", "taxi_out"].includes(leg.phase)) { pos = f.last_position; }
    // on the airport surface radar, that (more exact) target is your airplane: don't draw it twice
    entry.ownMarker = pos && !(entry.surfaceMine && Date.now() - entry.surfaceMine < 10000)
      ? add(planeMarker([pos[0], pos[1]], hdg, route, leg.display)) : null;
    entry.bounds = bounds;
    if (fit || !interactive) entry.center();
  }


  // ---------- pieces ----------
  function statusPill(leg) {
    let txt = leg.status;
    if (leg.status === "Delayed" && leg.delay) txt = `Delayed ${dur(leg.delay * 60)}`;
    return `<span class="pill ${slug(leg.status)}">${esc(txt)}</span>`;
  }

  const notFound = (leg) => leg.extra && !leg.flight;
  // Calendar flights come from the crew schedule; app-added ones are yours (Manual) or a friend's.
  const kindTag = (leg) => !leg.extra ? "" : leg.kind === "manual" ? '<span class="tag">MANUAL</span>' : '<span class="tag friend">FRIEND</span>';
  function timeBlock(label, t, sched, tz, right, src) {
    if (sched === "nf") return `<div class="t ${right ? "right" : ""}"><div class="lbl">${label}</div><div><span class="big">—</span></div><div class="sub">Not found</div></div>`;
    const shown = t ? (t.actual || t.estimated || t.scheduled) : null;
    const s = (t && t.scheduled) || sched;
    const diff = shown && s ? (shown - s) / 60 : 0;
    const cls = diff >= 5 ? "late" : diff <= -5 ? "early" : "";
    const kind = t && t.actual ? "Actual" : t && t.estimated ? "Estimated" : "Scheduled";
    return `<div class="t ${right ? "right" : ""}">
      <div class="lbl">${label}</div>
      <div><span class="big ${cls}">${hm(shown || s, tz)}</span>${Math.abs(diff) >= 5 ? `<span class="was">${hm(s, tz)}</span>` : ""}</div>
      <div class="sub">${shown || s ? `${kind} · ${tzAbbr(shown || s, tz)} · ${dayLabel(shown || s, tz)}` : "Not known yet"}</div>
      ${src && t && (t.actual ? src.actual : t.estimated ? src.estimated : null) ? `<div class="sub faint">from ${esc(t.actual ? src.actual : src.estimated)}</div>` : ""}
    </div>`;
  }

  function progress(leg) {
    const f = leg.flight; if (!f) return "";
    const off = f.times.off.actual, on = best(f.times, "on") || best(f.times, "in");
    if (leg.phase !== "airborne" || !off || !on) return "";
    const p = Math.max(0, Math.min(1, (Date.now() / 1000 - off) / (on - off)));
    return `<div class="progress"><span style="width:${p * 100}%"></span><i style="left:${p * 100}%"></i></div>
      <div class="small muted" style="display:flex;justify-content:space-between"><span>${dur(Date.now() / 1000 - off)} flown</span><span>${dur(on - Date.now() / 1000)} to go</span></div>`;
  }

  function chips(leg) {
    const f = leg.flight || {}, o = f.origin || {}, d = f.destination || {};
    const c = [];
    if (o.terminal) c.push(["Terminal", o.terminal]);
    c.push(["Gate", o.gate || "—"]);
    if (d.gate || d.terminal) c.push([`${leg.arr} gate`, [d.terminal && `T${d.terminal}`, d.gate].filter(Boolean).join(" ") || "—"]);
    if (d.baggage) c.push(["Bags", d.baggage]);
    if (f.aircraft_type || f.tail) c.push(["Aircraft", [f.aircraft_type, f.tail].filter(Boolean).join(" · ")]);
    return `<div class="chips">${c.map(([k, v]) => `<div class="chip"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>`;
  }

  function countdown(leg) {
    const now = Date.now() / 1000, f = leg.flight;
    const out = (f && best(f.times, "out")) || leg.sched_out;
    if (leg.phase === "scheduled" && out > now) return `<div class="countdown">Departs in <b>${dur(out - now)}</b></div>`;
    if (leg.phase === "taxi_out") return `<div class="countdown">Taxiing · pushed ${dur(now - f.times.out.actual)} ago</div>`;
    if (leg.phase === "landed") return `<div class="countdown">Landed · taxiing in</div>`;
    return "";
  }

  function inboundCard(leg) {
    const i = leg.inbound;
    if (!i || leg.phase !== "scheduled") return "";
    const tz = leg.dep_ap.tz;
    const due = best(i.times, "in");
    const minTurn = STATE.settings.min_turn_minutes;
    const turn = leg.turn;
    const cls = turn == null ? "" : turn < 0 ? "bad" : turn < minTurn ? "tight" : "";
    const landed = !!((i.times || {}).in || {}).actual;
    // once the airplane is at your gate the wait for it is over: full bar
    const pct = landed ? 100 : turn == null ? 0 : Math.max(4, Math.min(100, (turn / Math.max(minTurn * 2, 60)) * 100));
    const from = (i.origin || {}).iata || "";
    return `<a class="card inbound" href="#/aircraft/${leg.id}/0">
      <div class="head"><span class="title">Inbound aircraft ›</span><span class="pill ${slug(i.status)}">${esc(i.status)}</span></div>
      <div class="line">${esc(i.display_ident || "")} ${from ? `from ${esc(from)}` : ""}</div>
      <div class="small muted">${due ? `${landed ? "At the gate" : "Due at"} ${esc(leg.dep)} ${hm(due, tz)} ${tzAbbr(due, tz)}` : "Arrival time not known yet"}${i.tail ? ` · ${esc(i.tail)}` : ""}${(i.destination || {}).gate ? ` · gate ${esc(i.destination.gate)}` : ""}</div>
      ${turn != null ? `<div class="turnbar ${cls}"><div class="bar"><span style="width:${pct}%"></span></div><div class="num">${dur(turn * 60)} turn</div></div>` : ""}
    </a>`;
  }

  // ---------- the airplane's day: your inbound, its inbound, ... ----------
  // From rotation index `start` back: legs that still matter. Stops before an arrived leg, and
  // after the one the airplane is flying now (nothing earlier matters then).
  function chainFrom(rot, start) {
    const out = [];
    for (let i = start; i < (rot || []).length; i++) {
      const r = rot[i];
      if (!r || ["arrived", "cancelled", "diverted"].includes(r.phase)) break;
      out.push(r);
      if (["taxi_out", "airborne", "landed"].includes(r.phase)) break;
    }
    return out;
  }

  // Each map shows one flight and its own inbound only; earlier legs live on their own pages.
  function withChain(leg) {
    leg.chain = [];
    return leg;
  }

  async function aircraftView(id, n) {
    let leg;
    try { leg = await api(`api/leg/${id}?rotation=1`); } catch { go("#/"); return; }
    const rot = (leg.rotation || []).length ? leg.rotation : (leg.inbound ? [leg.inbound] : []);
    n = Math.max(0, Math.min(n, rot.length - 1));
    const h = rot[n];
    destroyMaps();
    if (!h) { $app.innerHTML = `<a class="back" href="#/leg/${id}">‹ ${esc(leg.display || "Flight")}</a><div class="card empty"><b>No inbound linked yet</b>FlightAware hasn't linked the flight bringing your aircraft.</div>`; return; }
    const t = h.times || { out: {}, off: {}, on: {}, in: {} };
    const o = h.origin || {}, d = h.destination || {};
    const tzA = o.tz, tzB = d.tz;
    const cell = (lbl, x, tz) => {
      const v = x ? (x.actual || x.estimated || x.scheduled) : null;
      const k = x && x.actual ? "actual" : x && x.estimated ? "estimated" : x && x.scheduled ? "scheduled" : "";
      const d = x && x.scheduled && v ? (v - x.scheduled) / 60 : 0;
      return `<div class="tcell"><div class="lbl">${lbl}</div><div class="v ${d >= 5 ? "late" : d <= -5 ? "early" : ""}">${hm(v, tz)}</div><div class="s">${k}${x && x.scheduled && v && Math.abs(v - x.scheduled) >= 300 ? ` · sched ${hm(x.scheduled, tz)}` : ""}</div></div>`;
    };
    const finished = ["arrived", "cancelled", "diverted"].includes(h.phase);
    const flying = ["taxi_out", "airborne", "landed"].includes(h.phase);
    const more = n + 1 < rot.length;
    // the whole day as a strip, newest (your flight) on the left
    const strip = [`<a class="rot-step mine" href="#/leg/${id}"><b>${esc(leg.display || "You")}</b><span>${esc(leg.dep)}→${esc(leg.arr)}</span></a>`]
      .concat(rot.map((r, i) => `<a class="rot-step ${i === n ? "on" : ""} ph-${slug(r.phase)}" href="#/aircraft/${id}/${i}"><b>${esc(r.display_ident || r.ident || "?")}</b><span>${esc((r.origin || {}).iata || "?")}→${esc((r.destination || {}).iata || "?")}</span><i>${esc(r.status)}</i></a>`))
      .join('<span class="rot-arrow">‹</span>');
    $app.innerHTML = `
      <a class="back" href="${n === 0 ? `#/leg/${id}` : `#/aircraft/${id}/${n - 1}`}">‹ ${n === 0 ? esc(leg.display || "Your flight") : "Later leg"}</a>
      <div class="rot-strip">${strip}</div>
      <div class="detail-map" id="ac-map"></div>
      <div class="card">
        <div class="hero-top"><div class="flightno">${esc(h.display_ident || h.ident || "")}<span class="tag">${n === 0 ? "YOUR INBOUND" : `${n + 1} LEGS BACK`}</span></div><span class="pill ${slug(h.status)}">${esc(h.status)}</span></div>
        <div class="route">
          <div class="ap"><div class="code">${esc(o.iata || "?")}</div><div class="city">${esc(o.city || o.name || "")}</div></div>
          <div class="mid"><span class="dur">${t.out && t.in && best(t, "in") && best(t, "out") ? dur(best(t, "in") - best(t, "out")) : ""}</span></div>
          <div class="ap right"><div class="code">${esc(d.iata || "?")}</div><div class="city">${esc(d.city || d.name || "")}</div></div>
        </div>
        <div class="grid4" style="margin-top:14px">
          ${cell(`Gate out · ${tzAbbr(best(t, "out"), tzA)}`, t.out, tzA)}
          ${cell(`Gate in · ${tzAbbr(best(t, "in"), tzB)}`, t.in, tzB)}
          ${cell("Takeoff", t.off, tzA)}
          ${cell("Landing", t.on, tzB)}
        </div>
        <div class="chips">
          ${o.gate ? `<div class="chip"><div class="k">${esc(o.iata || "")} gate</div><div class="v">${esc(o.gate)}</div></div>` : ""}
          ${d.gate ? `<div class="chip"><div class="k">${esc(d.iata || "")} gate</div><div class="v">${esc(d.gate)}</div></div>` : ""}
          ${h.aircraft_type || h.tail ? `<div class="chip"><div class="k">Aircraft</div><div class="v">${esc([h.aircraft_type, h.tail].filter(Boolean).join(" · "))}</div></div>` : ""}
          ${h.dep_delay ? `<div class="chip ${h.dep_delay >= 15 ? "changed" : ""}"><div class="k">Departure</div><div class="v">${h.dep_delay > 0 ? "+" : ""}${h.dep_delay} min</div></div>` : ""}
        </div>
      </div>
      ${finished ? `<div class="card small muted">This leg has ${esc(h.status.toLowerCase())}${h.phase === "arrived" && t.in.actual ? ` (${hm(t.in.actual, tzB)} ${tzAbbr(t.in.actual, tzB)})` : ""}, so the airplane's earlier legs don't matter. This is as far back as it goes.</div>`
        : flying ? `<div class="card small muted">The airplane is on this leg now, so nothing earlier matters.</div>`
        : more ? `<a class="card rot-next" href="#/aircraft/${id}/${n + 1}"><span class="small muted">Before this, the airplane flies</span><b>${esc(rot[n + 1].display_ident || "")} ${esc((rot[n + 1].origin || {}).iata || "")}→${esc((rot[n + 1].destination || {}).iata || "")}</b><span class="pill ${slug(rot[n + 1].status)}">${esc(rot[n + 1].status)}</span></a>`
        : `<div class="card small muted">${h.inbound_fa_id ? "The earlier leg will appear on the next check." : "FlightAware hasn't linked the leg before this one yet."}</div>`}
      <p class="small faint" style="margin:4px 4px 10px">${h.fetched ? `Data from FlightAware, fetched ${ago(h.fetched)}${finished ? "" : " · refreshes every 10 min"}` : "Data from FlightAware with your flight's last check"}</p>
      ${h.fa_id ? `<p style="margin:4px"><a class="btn" href="https://www.flightaware.com/live/flight/id/${encodeURIComponent(h.fa_id)}" target="_blank" rel="noopener">FlightAware ↗</a></p>` : ""}`;
    const earlier = h.phase === "scheduled" ? chainFrom(rot, n + 1) : [];
    drawMap(document.getElementById("ac-map"), { dep_ap: o, arr_ap: d, flight: h, phase: h.phase, inbound: earlier[0] || null,
      chain: [], live: null, display: h.display_ident }, { key: `ac-${id}-${n}` });
  }

  function problemBanners(leg) {
    const nf = notFound(leg) && leg.note && /^Not found/.test(leg.note) ? `<div class="banner warn"><span class="dot warn"></span><div>${esc(leg.note)}</div></div>` : "";
    return nf + (leg.problems || []).filter((p) => !p.nas).map((p) => `<div class="banner ${p.level}"><span class="dot ${p.level}"></span><div>${esc(p.text)}</div></div>`).join("");
  }

  function hero(leg) {
    const f = leg.flight || { times: {} }, tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz;
    const block = (leg.sched_in - leg.sched_out);
    return `<a class="card hero" href="#/leg/${leg.id}">
      <div class="map" id="hero-map"></div>
      <div class="hero-body">
        <div class="hero-top">
          <div class="flightno">${esc(leg.display || leg.ident || "Flight")}${leg.dh ? '<span class="tag">DH</span>' : ""}${kindTag(leg)}</div>
          ${statusPill(leg)}
        </div>
        <div class="route">
          <div class="ap"><div class="code">${esc(leg.dep)}</div><div class="city">${esc(leg.dep_ap.city || leg.dep_ap.name || "")}</div></div>
          <div class="mid"><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg><span class="dur">${notFound(leg) ? "" : dur(block)}</span></div>
          <div class="ap right"><div class="code">${esc(leg.arr)}</div><div class="city">${esc(leg.arr_ap.city || leg.arr_ap.name || "")}</div></div>
        </div>
        ${progress(leg)}
        <div class="times">
          ${timeBlock(f.times.out && f.times.out.actual ? "Departed" : "Departs", f.times.out, notFound(leg) ? "nf" : leg.sched_out, tzA)}
          ${timeBlock(f.times.in && f.times.in.actual ? "Arrived" : f.times.on && f.times.on.actual ? "Landed" : "Arrives", f.times.in, notFound(leg) ? "nf" : leg.sched_in, tzB, true)}
        </div>
        ${chips(leg)}
        ${countdown(leg)}
      </div>
    </a>`;
  }

  // Flighty-style flight card for the timeline.
  function row(leg) {
    const f = leg.flight || null, tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz;
    const t = f ? f.times : { out: {}, in: {} };
    const out = best(t, "out") || leg.sched_out, inn = best(t, "in") || leg.sched_in;
    const outS = (t.out && t.out.scheduled) || leg.sched_out, inS = (t.in && t.in.scheduled) || leg.sched_in;
    const late = (a, b) => a && b && Math.abs(a - b) >= 300;
    const known = leg.extra ? !!f : true;
    const timeCell = (ts, sched, tz, right) => `<div class="fc-ap ${right ? "right" : ""}">
        <div class="fc-time ${late(ts, sched) ? (ts > sched ? "late" : "early") : ""}">${known ? hm(ts, tz) : "—"}</div>
        <div class="fc-sub">${late(ts, sched) ? `<s>${hm(sched, tz)}</s> ` : ""}${known ? tzAbbr(ts, tz) : ""}</div></div>`;
    const now = Date.now() / 1000;
    let foot = [];
    if (f && f.origin && f.origin.gate) foot.push(`Gate ${esc(f.origin.terminal ? f.origin.terminal + f.origin.gate.replace(f.origin.terminal, "") : f.origin.gate)}`);
    if (f && f.tail) foot.push(esc(f.tail));
    if (leg.note && !f) foot.push(`<span class="faint">${esc(leg.note)}</span>`);
    if (!leg.finished && leg.phase === "scheduled" && out > now && out - now < 86400) foot.push(`<b>in ${dur(out - now)}</b>`);
    if (leg.problems.length) foot.push(`<span class="warnmark">${esc(leg.problems[0].text)}</span>`);
    const tags = (leg.dh ? '<span class="tag">DH</span>' : "") + kindTag(leg)
      + ((leg.source || "").includes("Gemini") ? '<span class="tag gem">GEMINI</span>' : "");
    return `<a class="fcard s-${slug(leg.status)} ${leg.finished ? "done" : ""}" href="#/leg/${leg.id}">
      <div class="fc-top"><span class="fc-no">${esc(leg.display || leg.ident || "")}</span>${tags}<span class="fc-date">${esc(dayLabel(out, tzA))}</span>${statusPill(leg)}</div>
      <div class="fc-route">
        <div class="fc-ap"><div class="fc-code">${esc(leg.dep || "···")}</div></div>
        <div class="fc-line"><i></i><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg><i></i></div>
        <div class="fc-ap right"><div class="fc-code">${esc(leg.arr || "···")}</div></div>
        ${timeCell(out, outS, tzA)}
        <div class="fc-dur">${known && inn > out ? dur(inn - out) : ""}</div>
        ${timeCell(inn, inS, tzB, true)}
      </div>
      ${foot.length ? `<div class="fc-foot">${foot.join(" · ")}</div>` : ""}
    </a>`;
  }

  function nasCard(items) {
    const sorted = [...items].sort((a, b) => a.airport.localeCompare(b.airport));
    return `<div class="card">${sorted.map((n) => `<div class="nas-item ${n.kind === "delay" ? "" : "mine"}">
      <div class="apt">${esc(n.airport)}</div>
      <div><div class="lbl">${esc(n.label)}</div><div class="det">${esc([n.detail, n.reason].filter(Boolean).join(" · "))}</div></div></div>`).join("")}
      ${STATE.nas_error ? `<div class="err">${esc(STATE.nas_error)}</div>` : ""}</div>`;
  }

  // ---------- NAS ----------
  const MAJOR = ["gs", "closure", "gdp"];
  const aptTz = (leg, apt) => apt === leg.dep ? leg.dep_ap.tz : apt === leg.arr ? leg.arr_ap.tz : undefined;
  // groups: [{n, tz, uses: [{leg, impact}]}] — one card per program, listing every flight it touches
  function nasGroups(legs) {
    const by = new Map();
    for (const l of legs) for (const n of l.nas || []) {
      if (!by.has(n.key)) by.set(n.key, { n, tz: aptTz(l, n.airport), uses: [] });
      by.get(n.key).uses.push({ leg: l, impact: n.impact || [] });
    }
    return [...by.values()].sort((a, b) => MAJOR.indexOf(a.n.kind) - MAJOR.indexOf(b.n.kind) || a.n.airport.localeCompare(b.n.airport));
  }
  function nasAlert(g) {
    const n = g.n, tz = g.tz;
    const hits = new Set(), lines = [];
    for (const u of g.uses) for (const i of u.impact) {
      if (!i.airborne) { if (i.center) hits.add(i.center); hits.add(i.from); }
      const who = i.who === "you" ? `${esc(u.leg.display || u.leg.ident || "Your flight")} from ${esc(i.from)}` : `${esc(u.leg.display || "Your flight")}'s inbound from ${esc(i.from)}`;
      const where = i.center ? ` (${esc(i.center)})` : "";
      if (i.airborne) continue;  // already flying: nothing to say
      lines.push(i.hit === true ? `<div class="na-hit yes">Includes ${who}${where}</div>`
        : i.hit === false ? `<div class="na-hit no">Doesn't include ${who}${where} — yet</div>`
        : `<div class="na-hit">Can't tell if it includes ${who}</div>`);
    }
    if (!lines.length) lines.push(...g.uses.map((u) => `<div class="na-hit">${esc(u.leg.display || u.leg.ident || "")} ${esc(u.leg.dep)}→${esc(u.leg.arr)}</div>`));
    const when = n.end ? `${n.start && n.start > Date.now() / 1000 ? hm(n.start, tz) + "–" : "until "}${hm(n.end, tz)} ${tzAbbr(n.end, tz)}` : "";
    const sub = [n.reason && n.reason[0].toUpperCase() + n.reason.slice(1), n.detail, n.prob && `extension probability ${n.prob.toLowerCase()}`].filter(Boolean).join(" · ");
    const scope = (n.scope || []).length ? `<div class="na-scope"><span class="faint">Departures from</span> ${n.scope.map((c) => `<span class="cc ${hits.has(c) ? "on" : ""}">${esc(c)}</span>`).join("")}</div>`
      : n.flights ? `<div class="na-scope faint">${esc(n.flights)}</div>` : "";
    const holds = g.uses.some((u) => u.impact.some((i) => i.hit !== false));
    const tappable = ["gs", "gdp"].includes(n.kind) && (n.scope || []).length;
    return `<div class="nas-alert k-${esc(n.kind)} ${holds ? "" : "clear"} ${tappable ? "tappable" : ""}" ${tappable ? `data-nas="${esc(n.key)}"` : ""}>
      <div class="na-top"><span class="na-apt">${esc(n.airport)}</span><span class="na-kind">${esc(n.label)}</span><span class="na-when">${esc(when)}</span></div>
      ${sub ? `<div class="na-sub">${esc(sub)}</div>` : ""}
      ${scope}${lines.join("")}
      ${n.advisory ? `<a class="na-adv" href="${esc(n.advisory)}" target="_blank" rel="noopener">FAA advisory ↗</a>` : ""}
      ${n.source ? `<div class="na-src faint small">from ${esc(n.source)}</div>` : ""}
    </div>`;
  }
  // FAA delay trends: they do affect you, so they sit up top too, but as a quiet card, not an alert.
  function nasTrend(g) {
    const n = g.n;
    const sub = [n.detail, n.reason && n.reason[0].toUpperCase() + n.reason.slice(1)].filter(Boolean).join(" · ");
    const forWhom = g.uses.map((u) => `${u.leg.display || u.leg.ident || ""} ${u.leg.dep}→${u.leg.arr}${u.leg.dep !== n.airport && u.leg.arr !== n.airport ? "" : u.leg.dep === n.airport && n.dir === "arr" ? " (inbound)" : ""}`).join(" · ");
    return `<div class="nas-trend"><span class="na-apt">${esc(n.airport)}</span><div><b>${esc(n.label)}</b>${sub ? `<div class="na-sub">${esc(sub)}</div>` : ""}${g.uses.length && legsShown > 1 ? `<div class="na-for">${esc(forWhom)}</div>` : ""}</div></div>`;
  }
  const TREND = ["delay"];
  let legsShown = 1;
  const nasTop = (legs) => {
    legsShown = legs.length;
    const gs = nasGroups(legs);
    return gs.filter((g) => MAJOR.includes(g.n.kind)).map(nasAlert).join("") + gs.filter((g) => TREND.includes(g.n.kind)).map(nasTrend).join("");
  };
  const nasMinor = (legs) => nasGroups(legs).filter((g) => !MAJOR.includes(g.n.kind) && !TREND.includes(g.n.kind)).map((g) => g.n);

  function alertsCard(list) {
    if (!list.length) return "";
    return `<div class="card">${list.slice(0, 15).map((a) => `<div class="alert">
      <span class="dot ${a.level}"></span>
      <div><div class="ttl">${esc(a.title)}</div><div class="msg">${esc(a.message)}</div></div>
      <div class="at">${fmt(a.at, undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}<br>${fmt(a.at, undefined, { day: "numeric", month: "short" })}</div>
    </div>`).join("")}</div>`;
  }

  // Collapsible sections that remember whether you left them open (per browser).
  const openKey = (id) => `ft-open-${id}`;
  function isOpen(id, dflt) { try { const v = localStorage.getItem(openKey(id)); return v === null ? dflt : v === "1"; } catch { return dflt; } }
  function fold(id, title, body, dflt = false, extra = "") {
    return `<details class="fold" data-fold="${esc(id)}" ${isOpen(id, dflt) ? "open" : ""}><summary><span>${esc(title)}</span>${extra}<i></i></summary>${body}</details>`;
  }
  document.addEventListener("click", (e) => {
    const c = e.target.closest && e.target.closest("[data-nas]");
    if (!c || e.target.closest("a")) return;
    go(`#/nas/${encodeURIComponent(c.dataset.nas)}`);   // in-page navigation, like every link (never the URL)
  });
  function wireFolds() {
    $app.querySelectorAll("details[data-fold]").forEach((d) => d.addEventListener("toggle", () => {
      try { localStorage.setItem(openKey(d.dataset.fold), d.open ? "1" : "0"); } catch {}
    }));
  }

  // ---------- history ----------
  const nm = (x) => x == null ? "—" : Math.round(x).toLocaleString();
  const mins = (m) => m == null ? "—" : `${m > 0 ? "+" : ""}${Math.round(m)}`;
  // average arrival vs schedule, in words: "usually 15 min early"
  const avgArr = (m) => m == null ? "" : Math.round(m) === 0 ? "usually right on schedule" : `usually ${Math.abs(Math.round(m))} min ${m < 0 ? "early" : "late"}`;
  const delayCls = (m) => m == null ? "" : m >= 15 ? "late" : m <= -5 ? "early" : "";
  function histTabs(cur, year) {
    const y = year ? `/${year}` : "";
    return `<div class="tabs">${[["flights", "Flights", `#/history${y}`], ["stats", "Stats", `#/history/stats${y}`], ["find", "Search", `#/history/find${year ? `?year=${year}` : ""}`], ["alerts", "Alerts", "#/history/alerts"]]
      .map(([k, l, href]) => `<a class="tab ${cur === k ? "on" : ""}" href="${href}">${l}</a>`).join("")}</div>`;
  }
  function yearChips(cur, years, base) {
    return `<div class="chips-row">${["", ...years].map((y) => `<a class="ychip ${y === cur ? "on" : ""}" href="${base}${y ? "/" + y : ""}">${y || "All"}</a>`).join("")}</div>`;
  }
  function histRow(f) {
    const t = f.times || {}, tzA = f.dep_tz, tzB = f.arr_tz;
    const out = (t.out && (t.out.a || t.out.s)), inn = (t.in && (t.in.a || t.in.s));
    return `<a class="hrow" href="#/hist/${encodeURIComponent(f.id)}">
      <div class="hd"><b>${esc(fmt(out, tzA, { day: "numeric" }))}</b><span>${esc(fmt(out, tzA, { weekday: "short" }))}</span></div>
      <div class="hm"><div class="hr"><b>${esc(f.dep)}</b> → <b>${esc(f.diverted_to || f.arr)}</b>${f.diverted_to ? ` <span class="tag warn">DIVERTED</span>` : ""}${f.cancelled ? ' <span class="tag warn">CANCELLED</span>' : ""}</div>
        <div class="small muted">${esc(f.display || f.ident || "")} · ${esc(hm(out, tzA))}–${esc(hm(inn, tzB))}${f.tail ? " · " + esc(f.tail) : ""}</div></div>
      <div class="hdl ${delayCls(f.arr_delay)}">${f.continuation_of ? '<span class="faint">continuation</span>' : f.arr_delay == null ? "" : f.arr_delay >= 15 ? `${dur(f.arr_delay * 60)} late` : f.arr_delay <= -5 ? `${Math.round(-f.arr_delay)}m early` : "on time"}${f.continued_by && f.arr_delay != null ? `<small>into ${esc(f.final_ap || f.arr)}</small>` : ""}</div>
    </a>`;
  }
  function importCard(empty, counts) {
    counts = counts || {};
    return `<div class="card">
      <h3 style="margin:0 0 4px">${empty ? "Bring in your past flights" : "Import"}</h3>
      <p class="small muted" style="margin:0 0 12px">A Flighty export (.xlsx or .csv). Flights already here are kept as they are, so importing again is safe. To re-import from scratch, delete the imported flights first. A tracks .zip from the Backfill tracks run adds the flown tracks to flights that don't have one.</p>
      <label class="btn primary filebtn">Choose file<input type="file" id="h-file" accept=".xlsx,.csv,.xlsm,.zip" hidden></label>
      <div id="h-msg" class="small" style="margin-top:8px"></div></div>
      ${empty ? "" : fold("hist-manage", "Export & delete", `<div class="card">
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <a class="btn" href="api/history/export.csv" download>Export CSV</a>
          ${counts.flighty ? `<button class="btn" id="h-clear-flighty">Delete ${counts.flighty} imported flights</button>` : ""}
          <button class="btn danger" id="h-clear-all">Delete all History</button>
        </div>
        <p class="small faint" style="margin:10px 0 0">The CSV uses Flighty's columns and local airport times, so it imports back here (or elsewhere). Deleting imported flights keeps the ones tracked here.</p></div>`)}`;
  }
  function wireImport() {
    wireFolds();
    const clr = async (what, q) => {
      if (!confirm(q)) return;
      const d = await api("api/history/clear", { method: "POST", body: JSON.stringify({ what }) });
      alert(`Deleted ${d.removed} flight${d.removed === 1 ? "" : "s"}.`);
      render(false);
    };
    const cf = document.getElementById("h-clear-flighty");
    if (cf) cf.onclick = () => clr("flighty", "Delete every flight imported from Flighty? Flights tracked here stay.");
    const ca = document.getElementById("h-clear-all");
    if (ca) ca.onclick = () => { if (confirm("Delete ALL of History, including flights tracked here? This can't be undone (unless you have a Drive backup).")) clr("all", "Really delete everything in History?"); };
    const inp = document.getElementById("h-file");
    if (!inp) return;
    inp.onchange = async () => {
      const file = inp.files[0], msg = document.getElementById("h-msg");
      if (!file) return;
      msg.className = "small muted"; msg.textContent = `Importing ${file.name}…`;
      try {
        const r = await fetch("api/history/import", { method: "POST", headers: { "X-Filename": file.name }, body: await file.arrayBuffer() });
        changed("api/history/import");
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || r.status);
        msg.className = "small"; msg.textContent = d.tracks != null ? `${d.added} tracks added, ${d.kept} flights already had one${d.unknown ? `, ${d.unknown} for flights not in History (import those flights first)` : ""}.${d.partial ? " The file was cut off in the download, so the tracks at its end are missing: download it again and import that copy to get the rest." : ""}` : `${d.added} added, ${d.already} already here${d.future ? `, ${d.future} future flights left out` : ""}${d.problems.length ? `, ${d.problems.length} rows skipped` : ""}.`;
        setTimeout(() => render(false), 900);
      } catch (e) { msg.className = "small err"; msg.textContent = `Import failed: ${e.message}`; }
    };
  }

  async function historyView(year) {
    destroyMaps();
    const d = await api(`api/history${year ? `?year=${year}` : ""}`);
    let h = histTabs("flights", year) + yearChips(year || "", d.years, "#/history");
    if (!d.flights.length) h += `<div class="card empty"><b>No flights yet</b>Your Calendar and Manual flights are saved here when they finish.</div>` + importCard(true);
    else {
      const st = d.stats;
      h += `<div class="tiles">${[["Flights", st.flights], ["Distance", nm(st.distance_nm) + " nm"], ["In the air", nm(st.air_hours) + " h"], ["On time", st.on_time_pct == null ? "—" : st.on_time_pct + "%"]]
        .map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("")}</div>`;
      let month = "";
      for (const f of d.flights) {
        const out = f.times.out && (f.times.out.a || f.times.out.s);
        const mo = fmt(out, f.dep_tz, { month: "long", year: "numeric" });
        if (mo !== month) { if (month) h += "</div>"; h += `<h2 class="section">${esc(mo)}</h2><div class="card hlist">`; month = mo; }
        h += histRow(f);
      }
      h += "</div>" + importCard(false, { flighty: d.flighty_count });
    }
    $app.innerHTML = h;
    wireImport();
  }

  function bars(rows, fmtv, link) {
    const mx = Math.max(1, ...rows.map((r) => Math.abs(r[1])));
    return rows.map(([k, v, sub]) => {
      const tag = link ? "a" : "div", href = link ? ` href="${esc(link(k))}"` : "";
      return `<${tag} class="bar-row${link ? " link" : ""}"${href}><div class="bk">${esc(k)}</div><div class="bt"><span class="${fmtv ? (v >= 1 ? "late" : v <= -1 ? "early" : "") : ""}" style="width:${Math.max(3, 100 * Math.abs(v) / mx)}%"></span></div><div class="bv">${fmtv ? fmtv(v) : v}${sub ? `<small>${sub}</small>` : ""}</div></${tag}>`;
    }).join("");
  }
  // a link to Search with these filters (and the year being viewed)
  const findLink = (year, f) => "#/history/find?" + new URLSearchParams({ ...(year ? { year } : {}), ...f }).toString();
  async function statsView(year) {
    destroyMaps();
    const d = await api(`api/history${year ? `?year=${year}` : ""}`);
    const st = d.stats;
    let h = histTabs("stats", year) + yearChips(year || "", d.years, "#/history/stats");
    if (!st.flights) { $app.innerHTML = h + `<div class="card empty"><b>No flights yet</b>Import your Flighty history on the Flights tab.</div>`; return; }
    const F = (f) => findLink(year, f);
    const tile = (k, v, sub, f) => `<${f ? `a href="${esc(F(f))}"` : "div"} class="tile${f ? " link" : ""}"><div class="k">${k}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</${f ? "a" : "div"}>`;
    const rt = (k) => F({ route: k.replace("–", "-") });
    h += `<div class="tiles">
      ${tile("Flights", st.flights, st.diverted ? `${st.diverted} diverted` : "", st.diverted ? { st: "diverted" } : {})}
      ${tile("Distance", nm(st.distance_nm) + " nm", `${(st.distance_nm / 21639).toFixed(1)}× around the world`)}
      ${tile("In the air", nm(st.air_hours) + " h", `${nm(st.block_hours)} h gate to gate`)}
      ${tile("Airports", st.airports, `${st.routes} routes · ${st.tails} tails`)}
    </div>
    <h2 class="section">Delays</h2>
    <div class="tiles">
      ${tile("On time", st.on_time_pct == null ? "—" : st.on_time_pct + "%", "at the gate, under 15 min late", { st: "ontime" })}
      ${tile("Avg arrival", st.avg_arr_delay == null ? "—" : mins(st.avg_arr_delay) + " min", `vs schedule (− is early) · departure ${mins(st.avg_dep_delay)} min`, { sort: "late" })}
      ${tile("Early", st.early_pct == null ? "—" : st.early_pct + "%", "5+ min early", { st: "early" })}
      ${tile("30+ late", st.late_30_pct == null ? "—" : st.late_30_pct + "%", "", { st: "late30" })}
    </div>
    <div class="card"><div class="small muted" style="margin-bottom:8px">Average arrival delay by scheduled departure time</div>
      ${bars(st.by_hour.map(([b, n, avg]) => [b, avg, ` · ${n}`]), (v) => mins(v) + "m", (b) => F({ hr: b, sort: "late" }))}</div>
    ${st.late_routes.length ? `<div class="card"><div class="small muted" style="margin-bottom:8px">Most delayed routes (3+ flights)</div>
      ${bars(st.late_routes.map(([r, n, avg, ot]) => [r, avg, ` · ${n} · ${Math.round(ot * 100)}% on time`]), (v) => mins(v) + "m", (k) => rt(k) + "&sort=late")}</div>` : ""}
    <h2 class="section">Most flown</h2>
    <div class="card"><div class="small muted" style="margin-bottom:8px">Routes</div>${bars(st.top_routes, null, rt)}</div>
    <div class="card"><div class="small muted" style="margin-bottom:8px">Airports</div>${bars(st.top_airports, null, (k) => F({ ap: k }))}</div>
    <div class="card"><div class="small muted" style="margin-bottom:8px">Aircraft</div>${bars(st.top_aircraft, null, (k) => F({ ac: k }))}</div>
    <div class="card"><div class="small muted" style="margin-bottom:8px">Tails</div>${bars(st.top_tails, null, (k) => F({ tail: k }))}</div>
    <h2 class="section">Records</h2>
    <div class="card hlist">${st.longest ? `<div class="small muted">Longest · ${nm(st.longest.distance_nm)} nm</div>${histRow(st.longest)}` : ""}
      ${st.worst_delay ? `<div class="small muted" style="margin-top:10px">Latest arrival · ${Math.round(st.worst_delay.arr_delay)} min</div>${histRow(st.worst_delay)}` : ""}</div>`;
    $app.innerHTML = h;
  }

  // ---------- track backfill tokens (pasted into the flight-track-history workflow) ----------
  function bfToken(f) {
    const t = f.times || {}, g = (k, x) => (t[k] || {})[x];
    const o = g("out", "a") || g("off", "a") || g("out", "s"), i = g("in", "a") || g("on", "a") || g("in", "s");
    return f.tail && o && i && i > o ? `${f.id},${f.tail},${Math.round(o)},${Math.round(i)}` : null;
  }
  async function copyText(txt) {
    try { await navigator.clipboard.writeText(txt); return true; } catch {}
    const ta = document.createElement("textarea");
    ta.value = txt; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, txt.length);
    let ok = false; try { ok = document.execCommand("copy"); } catch {}
    ta.remove();
    return ok;
  }

  // ---------- History search ----------
  // Everything is filtered here in the page (a few thousand flights at most). Filters live in the
  // page address (#/history/find?st=late30&ap=EWR...), so Back returns to the same results.
  const ST_CHIPS = [["late15", "Late 15+"], ["late30", "Late 30+"], ["late60", "Late 60+"], ["ontime", "On time"], ["early", "Early"],
                    ["diverted", "Diverted"], ["cancelled", "Cancelled"], ["notrack", "No track"]];
  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const HOURS = ["00–03", "03–06", "06–09", "09–12", "12–15", "15–18", "18–21", "21–24"];
  let FIND_CACHE = null;
  function fdep(f) { const t = f.times || {}; return (t.out && (t.out.s || t.out.a)) || (t.off && (t.off.s || t.off.a)) || 0; }
  function localParts(f) {
    if (f._lp) return f._lp;
    const ts = fdep(f);
    let h = 0, dw = 0;
    try {
      const p = new Intl.DateTimeFormat("en-US", { timeZone: f.dep_tz || undefined, hour: "numeric", hourCycle: "h23", weekday: "short" }).formatToParts(new Date(ts * 1000));
      h = parseInt(p.find((x) => x.type === "hour").value, 10) % 24;
      dw = DOW.indexOf(p.find((x) => x.type === "weekday").value);
    } catch {}
    return (f._lp = { h, dw, hr: HOURS[Math.floor(h / 3)] });
  }
  function hay(f) {
    if (f._hay) return f._hay;
    const lp = localParts(f), mo = fmt(fdep(f), f.dep_tz, { month: "long" });
    return (f._hay = [f.ident, f.display, (f.display || "").replace(/\s/g, ""), f.flight_no, f.dep, f.arr, f.diverted_to,
      `${f.dep}-${f.arr}`, `${f.dep}${f.arr}`, f.tail, f.aircraft, f.type, f.dep_gate && `gate ${f.dep_gate}`, f.arr_gate && `gate ${f.arr_gate}`,
      f.date, mo, DOW[lp.dw], f.diverted_to && "diverted divert", f.cancelled && "cancelled", f.source === "flighty" ? "flighty" : "tracked",
      f.route].filter(Boolean).join(" ").toLowerCase());
  }
  function matchStatus(f, st) {
    const d = f.arr_delay;
    switch (st) {
      case "late15": return d != null && d >= 15;
      case "late30": return d != null && d >= 30;
      case "late60": return d != null && d >= 60;
      case "ontime": return d != null && d < 15 && !f.cancelled;
      case "early": return d != null && d <= -5;
      case "diverted": return !!f.diverted_to;
      case "cancelled": return !!f.cancelled;
      case "notrack": return !f.has_track && !f.cancelled;
    }
    return true;
  }
  function applyFind(all, P) {
    const toks = (P.get("q") || "").toLowerCase().split(/\s+/).filter(Boolean);
    const sts = (P.get("st") || "").split(",").filter(Boolean);
    const [rd, ra] = (P.get("route") || "").toUpperCase().replace(/[^A-Z0-9]+/g, " ").trim().replace(/^([A-Z0-9]{3})([A-Z0-9]{3})$/, "$1 $2").split(" ");
    const g = (k) => (P.get(k) || "").trim();
    const minD = g("min") === "" ? null : parseFloat(g("min")), maxD = g("max") === "" ? null : parseFloat(g("max"));
    const out = all.filter((f) => {
      if (g("year") && !(f.date || "").startsWith(g("year"))) return false;
      if (g("mo") && parseInt((f.date || "").slice(5, 7), 10) !== parseInt(g("mo").slice(-2), 10)) return false;
      // typed boxes: a partly typed airport or tail matches what starts with it
      const ap = (x, v) => !!x && x.startsWith(v.toUpperCase());
      if (rd && (!ap(f.dep, rd) || (ra && !ap(f.diverted_to || f.arr, ra) && !ap(f.arr, ra)))) return false;
      if (g("dep") && !ap(f.dep, g("dep"))) return false;
      if (g("arr") && !ap(f.arr, g("arr")) && !ap(f.diverted_to, g("arr"))) return false;
      if (g("ap") && ![f.dep, f.arr, f.diverted_to].some((x) => ap(x, g("ap")))) return false;
      if (g("tail") && !(f.tail || "").toUpperCase().includes(g("tail").toUpperCase().replace(/^(?!N)(?=\d)/, "N"))) return false;
      if (g("ac") && (f.aircraft || f.type || "") !== g("ac")) return false;
      if (g("hr") && localParts(f).hr !== g("hr")) return false;
      if (g("dow") && String(localParts(f).dw) !== g("dow")) return false;
      if (sts.length && !sts.every((s_) => matchStatus(f, s_))) return false;
      if (minD != null && !(f.arr_delay != null && f.arr_delay >= minD)) return false;
      if (maxD != null && !(f.arr_delay != null && f.arr_delay <= maxD)) return false;
      for (const t of toks) {
        const m = t.match(/^([<>]=?)(-?\d+)m?$/);  // >30 = 30+ min late, <-5 = 5+ early
        if (m) {
          const d = f.arr_delay, n = parseFloat(m[2]);
          if (d == null || !(m[1][0] === ">" ? d >= n : d <= n)) return false;
        } else if (!hay(f).includes(t)) return false;
      }
      return true;
    });
    const by = { new: (a, b) => fdep(b) - fdep(a), old: (a, b) => fdep(a) - fdep(b),
      late: (a, b) => (b.arr_delay ?? -1e9) - (a.arr_delay ?? -1e9), early: (a, b) => (a.arr_delay ?? 1e9) - (b.arr_delay ?? 1e9),
      long: (a, b) => (b.distance_nm || 0) - (a.distance_nm || 0), block: (a, b) => (b.block_min || 0) - (a.block_min || 0) }[g("sort") || "new"];
    return out.sort(by);
  }
  async function findView(route) {
    destroyMaps();
    const P = new URLSearchParams(route.split("?")[1] || "");
    if (!FIND_CACHE || Date.now() - FIND_CACHE.at > 60000 || !morphing) FIND_CACHE = { at: Date.now(), d: await api("api/history") };
    const all = FIND_CACHE.d.flights;
    const uniq = (fn) => [...new Set(all.map(fn).filter(Boolean))].sort();
    const opt = (k, vals, label, fmtOpt) => `<label class="ff"><span>${label}</span><select data-k="${k}"><option value="">Any</option>${vals.map((v) => `<option value="${esc(v)}" ${P.get(k) === String(v) ? "selected" : ""}>${esc(fmtOpt ? fmtOpt(v) : v)}</option>`).join("")}</select></label>`;
    // typed boxes, with the values you've flown as suggestions
    const txt = (k, label, ph, dl) => `<label class="ff"><span>${label}</span><input data-k="${k}" data-typed="1" list="${dl}" placeholder="${ph}" value="${esc(P.get(k) || "")}" autocomplete="off" autocapitalize="characters" spellcheck="false"></label>`;
    const apts = [...new Set(all.flatMap((f) => [f.dep, f.arr, f.diverted_to]).filter(Boolean))].sort();
    const lists = `<datalist id="dl-ap">${apts.map((v) => `<option value="${v}">`).join("")}</datalist>
      <datalist id="dl-rt">${uniq((f) => `${f.dep}-${f.arr}`).map((v) => `<option value="${v}">`).join("")}</datalist>
      <datalist id="dl-tail">${uniq((f) => f.tail).map((v) => `<option value="${v}">`).join("")}</datalist>`;
    const sts = (P.get("st") || "").split(",").filter(Boolean);
    const nFilters = [...P.keys()].filter((k) => !["q", "st", "sort"].includes(k)).length;
    let h = histTabs("find", P.get("year") || "") + `
      <input class="search" id="f-q" type="search" placeholder="Flight, airport, tail, aircraft, month, >30…" value="${esc(P.get("q") || "")}" autocomplete="off" autocapitalize="characters">
      <div class="chips-row wrap">${ST_CHIPS.map(([k, l]) => `<button class="ychip ${sts.includes(k) ? "on" : ""}" data-st="${k}">${l}</button>`).join("")}</div>
      ${fold("find-more", `More filters${nFilters ? ` · ${nFilters}` : ""}`, `<div class="card ffgrid">
        ${opt("year", uniq((f) => (f.date || "").slice(0, 4)).reverse(), "Year")}
        ${opt("mo", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], "Month", (v) => ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][v - 1])}
        ${txt("dep", "From", "EWR", "dl-ap")}
        ${txt("arr", "To", "FLL", "dl-ap")}
        ${txt("ap", "Either airport", "MCO", "dl-ap")}
        ${txt("route", "Route", "EWR-FLL", "dl-rt")}
        ${opt("ac", uniq((f) => f.aircraft || f.type), "Aircraft")}
        ${txt("tail", "Tail", "N37413", "dl-tail")}
        ${opt("hr", HOURS, "Departs (local)")}
        ${opt("dow", [0, 1, 2, 3, 4, 5, 6], "Day", (v) => DOW[v])}
        <label class="ff"><span>Late at least</span><input data-k="min" type="number" inputmode="numeric" placeholder="min" value="${esc(P.get("min") || "")}"></label>
        <label class="ff"><span>Late at most</span><input data-k="max" type="number" inputmode="numeric" placeholder="min (−5 = early)" value="${esc(P.get("max") || "")}"></label>
        <label class="ff"><span>Sort</span><select data-k="sort">${[["new", "Newest"], ["old", "Oldest"], ["late", "Latest arrival"], ["early", "Earliest arrival"], ["long", "Longest distance"], ["block", "Longest gate to gate"]]
          .map(([v, l]) => `<option value="${v}" ${(P.get("sort") || "new") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        ${[...P.keys()].length ? `<a class="btn" href="#/history/find" id="f-clear">Clear all</a>` : ""}
      </div>`, nFilters > 0)}
      <div id="f-out"></div>${lists}`;
    $app.innerHTML = h;
    wireFolds();
    const draw = () => {
      const res = applyFind(all, P);
      const dl = res.map((f) => f.arr_delay).filter((v) => v != null);
      const ot = dl.length ? Math.round(100 * dl.filter((v) => v < 15).length / dl.length) : null;
      const avg = dl.length ? dl.reduce((a, b) => a + b, 0) / dl.length : null;
      const dist = res.reduce((a, f) => a + (f.cancelled ? 0 : f.distance_nm || 0), 0);
      const noTrack = res.filter((f) => !f.has_track && !f.cancelled && bfToken(f));
      let o = `<div class="find-sum"><b>${res.length}</b> flight${res.length === 1 ? "" : "s"}${ot != null ? ` · ${ot}% on time · ${avgArr(avg)}` : ""}${dist ? ` · ${nm(dist)} nm` : ""}
        ${noTrack.length ? `<button class="linkbtn" id="f-bf">Copy ${noTrack.length} without a track for backfill</button>` : ""}</div>`;
      if (!res.length) o += `<div class="card empty small muted">No flights match.</div>`;
      const sorted = (P.get("sort") || "new");
      if (sorted === "new" || sorted === "old") {
        let month = "";
        for (const f of res.slice(0, 600)) {
          const mo = fmt(fdep(f), f.dep_tz, { month: "long", year: "numeric" });
          if (mo !== month) { if (month) o += "</div>"; o += `<h2 class="section">${esc(mo)}</h2><div class="card hlist">`; month = mo; }
          o += histRow(f);
        }
        if (month) o += "</div>";
      } else if (res.length) o += `<div class="card hlist">${res.slice(0, 600).map(histRow).join("")}</div>`;
      document.getElementById("f-out").innerHTML = o;
      const bf = document.getElementById("f-bf");
      if (bf) bf.onclick = async () => {
        const ok = await copyText(noTrack.map(bfToken).join(" "));
        bf.textContent = ok ? `Copied ${noTrack.length}: paste into Backfill tracks → flights` : "Couldn't copy";
      };
    };
    const commit = () => {
      for (const k of [...P.keys()]) if (!P.get(k)) P.delete(k);
      replaceRoute("#/history/find" + (P.toString() ? "?" + P.toString() : ""));
      draw();
    };
    draw();
    let qt = null;
    document.getElementById("f-q").oninput = (e) => { clearTimeout(qt); qt = setTimeout(() => { P.set("q", e.target.value.trim()); commit(); }, 150); };
    $app.querySelectorAll("[data-st]").forEach((b) => b.onclick = () => {
      const cur = new Set((P.get("st") || "").split(",").filter(Boolean));
      cur.has(b.dataset.st) ? cur.delete(b.dataset.st) : cur.add(b.dataset.st);
      b.classList.toggle("on", cur.has(b.dataset.st));
      P.set("st", [...cur].join(","));
      commit();
    });
    const setK = (el) => {
      P.set(el.dataset.k, el.dataset.typed ? el.value.trim().toUpperCase() : el.value);
      commit();
      const n = [...P.keys()].filter((k) => !["q", "st", "sort"].includes(k)).length;
      const t = $app.querySelector('[data-fold="find-more"] summary > span');
      if (t) t.textContent = `More filters${n ? ` · ${n}` : ""}`;
    };
    let tt = null;
    $app.querySelectorAll(".ffgrid [data-k]").forEach((el) => {
      el.onchange = () => setK(el);
      if (el.dataset.typed || el.type === "number") el.oninput = () => { clearTimeout(tt); tt = setTimeout(() => setK(el), 200); };
    });
  }

  async function alertsHistoryView() {
    destroyMaps();
    const d = await api("api/alerts");
    let h = histTabs("alerts") + `<input class="search" id="a-q" placeholder="Search notifications" autocomplete="off">`;
    h += `<div id="a-list"></div>`;
    $app.innerHTML = h;
    const draw = (q) => {
      q = (q || "").toLowerCase();
      const list = d.alerts.filter((a) => !q || `${a.title} ${a.message} ${a.display || ""} ${a.route || ""}`.toLowerCase().includes(q));
      let out = "", day = "";
      for (const a of list.slice(0, 400)) {
        const dd = fmt(a.at, undefined, { weekday: "short", day: "numeric", month: "short" });
        if (dd !== day) { if (day) out += "</div>"; out += `<h2 class="section">${esc(relDay(a.at) || dd)}</h2><div class="card">`; day = dd; }
        out += `<div class="alert"><span class="dot ${a.level}"></span><div><div class="ttl">${esc(a.title)}</div><div class="msg">${esc(a.message)}</div></div>
          <div class="at">${fmt(a.at, undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}</div></div>`;
      }
      document.getElementById("a-list").innerHTML = list.length ? out + "</div>" : `<div class="card empty small muted">Nothing${q ? " matches" : " yet"}.</div>`;
    };
    draw("");
    document.getElementById("a-q").oninput = (e) => draw(e.target.value);
  }

  async function histFlightView(id) {
    destroyMaps();
    let f;
    try { f = await api(`api/history/${encodeURIComponent(id)}`); } catch { go("#/history"); return; }
    const t = f.times || {}, tzA = f.dep_tz, tzB = f.arr_tz;
    const cont = !!f.continuation_of;  // its "schedule" is the original flight's: show actual times only
    const cell = (lbl, x, tz) => {
      const a = x && x.a, s_ = cont ? null : x && x.s, dlt = a && s_ ? (a - s_) / 60 : null;
      return `<div class="tcell"><div class="lbl">${lbl}</div><div class="v ${delayCls(dlt)}">${hm(a || s_, tz)}</div><div class="s">${a ? "actual" : "scheduled"}${dlt != null && Math.abs(dlt) >= 1 ? ` · sched ${hm(s_, tz)}` : ""}</div></div>`;
    };
    const rs = f.route_stats || {};
    $app.innerHTML = `
      <a class="back" href="#/history">‹ History</a>
      <div class="detail-map" id="hist-map"></div>
      <div class="card">
        <div class="hero-top"><div class="flightno">${esc(f.display || f.ident || "")}${f.diverted_to ? '<span class="tag warn">DIVERTED</span>' : ""}</div>
          <span class="small muted">${esc(fmt((t.out && (t.out.a || t.out.s)), tzA, { weekday: "short", day: "numeric", month: "short", year: "numeric" }))}</span></div>
        <div class="route">
          <div class="ap"><div class="code">${esc(f.dep)}</div><div class="city">${esc((f.dep_ap || {}).city || "")}</div></div>
          <div class="mid"><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg><span class="dur">${f.block_min ? dur(f.block_min * 60) : ""}</span></div>
          <div class="ap right"><div class="code">${esc(f.diverted_to || f.arr)}</div><div class="city">${esc((f.arr_ap || {}).city || "")}</div></div>
        </div>
        ${f.diverted_to ? `<p class="small" style="color:var(--amber)">Planned to ${esc(f.arr)}, diverted to ${esc(f.diverted_to)}.${f.continued_by
          ? ` Reached ${esc(f.final_ap || f.arr)} at ${esc(hm(f.final_arrival, tzB))}${f.arr_delay != null ? `, ${esc(dur(f.arr_delay * 60))} after the original schedule` : ""} · <a href="#/hist/${encodeURIComponent(f.continued_by)}" style="color:var(--accent)">${esc(f.continued_by_label || "continuation")} ›</a>` : ""}</p>` : ""}
        ${cont ? `<p class="small muted">Continuation of the diverted <a href="#/hist/${encodeURIComponent(f.continuation_of)}" style="color:var(--accent)">${esc(f.continuation_of_label || "flight")} ›</a>. Its lateness is counted on that flight, against the original schedule.</p>` : ""}
        <div class="grid4" style="margin-top:14px">
          ${cell(`Gate out · ${tzAbbr(t.out && (t.out.a || t.out.s), tzA)}`, t.out, tzA)}
          ${cell(`Gate in · ${tzAbbr(t.in && (t.in.a || t.in.s), tzB)}`, t.in, tzB)}
          ${cell("Takeoff", t.off, tzA)}
          ${cell("Landing", t.on, tzB)}
        </div>
        <div class="chips">
          ${f.dep_gate ? `<div class="chip"><div class="k">${esc(f.dep)} gate</div><div class="v">${esc(f.dep_gate)}</div></div>` : ""}
          ${f.arr_gate ? `<div class="chip"><div class="k">${esc(f.diverted_to || f.arr)} gate</div><div class="v">${esc(f.arr_gate)}</div></div>` : ""}
          ${f.aircraft || f.tail ? `<div class="chip"><div class="k">Aircraft</div><div class="v">${esc([f.type || f.aircraft, f.tail].filter(Boolean).join(" · "))}</div></div>` : ""}
          ${f.diverted_to ? `<div class="chip changed"><div class="k">Arrival</div><div class="v">Diverted${f.continued_by && f.arr_delay != null ? ` · ${esc(dur(f.arr_delay * 60))} late into ${esc(f.final_ap || f.arr)}` : ""}</div></div>` : ""}
          ${f.arr_delay != null && !f.diverted_to ? `<div class="chip ${f.arr_delay >= 15 ? "changed" : ""}"><div class="k">Arrival</div><div class="v">${f.arr_delay >= 1 ? Math.round(f.arr_delay) + " min late" : f.arr_delay <= -1 ? Math.round(-f.arr_delay) + " min early" : "on time"}</div></div>` : ""}
        </div>
      </div>
      <div class="card"><div class="kv">
        <div class="k">Distance</div><div>${nm(f.distance_nm)} nm</div>
        <div class="k">In the air</div><div>${f.air_min ? dur(f.air_min * 60) : "—"}</div>
        ${f.aircraft ? `<div class="k">Aircraft</div><div>${esc(f.aircraft)}</div>` : ""}
        ${f.tail ? `<div class="k">This tail</div><div>${f.tail_count} flight${f.tail_count === 1 ? "" : "s"} with you</div>` : ""}
        <div class="k">This route</div><div>${rs.flights} flight${rs.flights === 1 ? "" : "s"}${rs.on_time_pct != null ? ` · ${rs.on_time_pct}% on time · ${avgArr(rs.avg_arr_delay)}` : ""}</div>
        ${f.route ? `<div class="k">Filed route</div><div class="mono small">${esc(f.route)}</div>` : ""}
        <div class="k">Source</div><div>${f.source === "flighty" ? "Flighty import" : "Tracked here"}${f.has_track ? " · flown track saved" : ""}</div>
      </div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">${!f.has_track && bfToken(f) ? `<button class="btn" id="h-bf">Copy for track backfill</button>` : ""}<button class="btn" id="h-del">Delete from History</button></div>
      ${!f.has_track && bfToken(f) ? `<p class="small faint" style="margin:8px 0 0">No flown track yet. Copy, then in GitHub run Backfill tracks with this pasted in the flights box, and import its tracks download here.</p>` : ""}</div>`;
    drawMap(document.getElementById("hist-map"), { dep_ap: f.dep_ap || {}, arr_ap: f.arr_ap || {}, flight: { track: f.track || [], waypoints: [], times: {} }, phase: "arrived", inbound: null, chain: [], live: null, display: f.display },
      { key: `hist-${f.id}` });
    const hb = document.getElementById("h-bf");
    if (hb) hb.onclick = async () => { hb.textContent = (await copyText(bfToken(f))) ? "Copied" : "Couldn't copy"; };
    document.getElementById("h-del").onclick = async () => {
      if (!confirm("Delete this flight from History?")) return;
      await api("api/history/delete", { method: "POST", body: JSON.stringify({ id: f.id }) });
      go("#/history");
    };
  }

  // ---------- views ----------
  function homeView() {
    const sortKey = (l) => (l.flight && best(l.flight.times, "out")) || l.sched_out;
    const all = [...STATE.legs].sort((a, b) => sortKey(a) - sortKey(b));
    const live = all.filter((l) => !l.finished);
    const next = live[0];
    // NAS for airports on any flight in the watch window; programs go up top, minor delays below.
    const watching = live.filter((l) => l.watching);
    const myAirports = new Set(watching.flatMap((l) => [l.dep, l.arr]).filter(Boolean));
    let h = nasTop(watching);
    if (STATE.schedule && STATE.schedule.error) h += `<div class="banner critical"><span class="dot critical"></span><div><b>Schedule</b><br>${esc(STATE.schedule.error)}</div></div>`;
    if (next) {
      h += problemBanners(next);
      h += hero(next);
      h += inboundCard(next);
      if (next.note) h += `<div class="small muted" style="margin:-4px 4px 10px">${esc(next.note)}</div>`;
      if (next.error) h += `<div class="err" style="margin:-4px 4px 10px">${esc(next.error)}</div>`;
    } else {
      h += `<div class="card empty"><b>No upcoming flights</b>Nothing coming up in your calendar${STATE.schedule && STATE.schedule.count === 0 ? " (no flights found in your schedule)" : ""}. Tap + to add a flight.</div>`;
    }
    const rest = live.slice(1);
    h += `<h2 class="section"><span>Upcoming</span><a class="small" style="color:var(--accent)" href="#/add">+ Add flight</a></h2>`;
    if (rest.length) {
      let lastDay = "";
      for (const l of rest.slice(0, 40)) {
        const d = dayLabel(sortKey(l), l.dep_ap.tz);
        if (d !== lastDay) { h += `<div class="day">${esc(relDay(sortKey(l), l.dep_ap.tz) || d)}</div>`; lastDay = d; }
        h += row(l);
      }
    } else {
      h += `<div class="card small muted">Nothing else scheduled.</div>`;
    }
    if (myAirports.size) {
      const minor = nasMinor(watching), major = nasGroups(watching).length - minor.length;  // major = shown up top
      h += `<h2 class="section"><span>NAS · ${esc([...myAirports].join(" · "))}</span><span class="small">${STATE.nas_at ? ago(STATE.nas_at) : ""}</span></h2>`;
      h += minor.length ? nasCard(minor) : `<div class="card small muted">${major ? "Shown at the top." : "No ground stops, delay programs or closures at your airports."}${STATE.nas_error ? `<div class="err">${esc(STATE.nas_error)}</div>` : ""}</div>`;
    }
    // Short feeds here; everything older lives in History.
    const nowS = Date.now() / 1000;
    const done = all.filter((l) => l.finished && nowS - (best(l.flight && l.flight.times, "in") || l.sched_in) < 18 * 3600)
      .sort((a, b) => sortKey(b) - sortKey(a)).slice(0, 3);
    if (done.length) h += `<h2 class="section"><span>Recent</span><a class="small" style="color:var(--accent)" href="#/history">History ›</a></h2>` + done.map(row).join("");
    const fresh = STATE.alerts.filter((a) => nowS - a.at < 12 * 3600).slice(0, 5);
    if (fresh.length) h += `<h2 class="section"><span>Latest alerts</span><a class="small" style="color:var(--accent)" href="#/history/alerts">All ›</a></h2>` + alertsCard(fresh);
    h += `<p class="small faint" style="margin:22px 4px">v${esc(STATE.version)} · alerts go to ${esc(STATE.notify_target)} · watching ${STATE.settings.watch_hours} h before departure${STATE.schedule && STATE.schedule.checked ? ` · schedule read ${ago(STATE.schedule.checked)}` : ""}</p>`;
    destroyMaps();
    $app.innerHTML = h;
    const hm_ = document.getElementById("hero-map");
    if (hm_ && next) {
      const opt = { interactive: false, key: `hero-${next.id}` };
      // draw straight away from the last full copy so the map never shows blank, then update
      if (LEGS[next.id]) drawMap(hm_, withChain(LEGS[next.id]), opt);
      api(`api/leg/${next.id}`).then((full) => { LEGS[next.id] = full; drawMap(document.getElementById("hero-map"), withChain(full), opt); })
        .catch(() => { if (!LEGS[next.id]) drawMap(hm_, next, opt); });
    }
  }

  // A ground stop or delay program on a map: the centers whose departures it holds, filled; the
  // program airport; and where your flight (or its inbound) departs, red if it's held, green if not.
  let ARTCC = null;
  async function nasView(key) {
    const legs = (STATE.legs || []).filter((l) => (l.nas || []).some((n) => n.key === key));
    const g = nasGroups(legs).find((x) => x.n.key === key);
    destroyMaps();
    if (!g) { $app.innerHTML = `<a class="back" href="#/">‹ Flights</a><div class="card empty"><b>Program ended</b>It's no longer in the FAA's list.</div>`; return; }
    const n = g.n;
    $app.innerHTML = `<a class="back" href="#/">‹ Flights</a>
      <div class="detail-map" id="nas-map"></div>
      <p class="small faint" id="nas-map-note" style="margin:-6px 4px 10px" hidden></p>
      ${nasAlert(g).replace("tappable", "")}
      <div class="card small muted">Shaded: the en-route centers whose departures this ${n.kind === "gs" ? "ground stop" : "program"} holds. ${(n.scope || []).filter((c) => !/^Z[A-Z]{2}$/.test(c)).length ? `Also: ${esc(n.scope.filter((c) => !/^Z[A-Z]{2}$/.test(c)).join(" "))}.` : ""}</div>`;
    let el = document.getElementById("nas-map");
    if (!window.L) { el.innerHTML = '<div class="empty small">Map library didn\'t load.</div>'; return; }
    // a live update re-draws this page: keep the map you're looking at instead of starting over
    const have = MAPS.nas;
    if (have && have.key === key) {
      if (have.el !== el) el.replaceWith(have.el);
      usedMaps.add("nas");
      setTimeout(() => have.map.invalidateSize(), 30);
      return;
    }
    if (have) { try { have.map.remove(); } catch {} delete MAPS.nas; }
    if (el.classList.contains("leaflet-container") || el._leaflet_id) {
      const f = document.createElement("div"); f.id = el.id; f.className = "detail-map"; el.replaceWith(f); el = f;
    }
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    // canvas: thousands of boundary points draw without stalling the page
    const map = L.map(el, { renderer: L.canvas({ padding: 0.2 }), zoomControl: true, attributionControl: true, worldCopyJump: true });
    if (map.attributionControl) map.attributionControl.setPrefix("");
    addTiles(map, dark);
    MAPS.nas = { el, map, key, layer: L.layerGroup().addTo(map), bounds: [], interactive: true };
    usedMaps.add("nas");
    oneFingerZoom(map, el);
    const hot = n.kind === "gs" ? css("--red") : css("--amber");
    const ok = css("--green"), muted = css("--faint");
    const bounds = [];
    const apPos = {};
    for (const l of legs) for (const a of [l.dep_ap, l.arr_ap]) if (a && ll(a)) apPos[a.iata] = ll(a);
    // where the program is
    const prog = apPos[n.airport];
    if (prog) {
      L.circleMarker(prog, { radius: 6, color: hot, fillColor: hot, fillOpacity: 1, weight: 2 }).addTo(map);
      L.marker(prog, { icon: L.divIcon({ className: "", html: `<span class="apt-label" style="border-color:${hot}">${esc(n.airport)} ${n.kind === "gs" ? "ground stop" : "delay program"}</span>`, iconSize: null, iconAnchor: [-8, 30] }) }).addTo(map);
      bounds.push(prog);
    }
    // your departures / inbound departures it touches
    for (const u of g.uses) for (const i of u.impact) {
      const p = apPos[i.from] || (u.leg.inbound && u.leg.inbound.origin && ll(u.leg.inbound.origin));
      if (!p) continue;
      const c = i.airborne ? muted : i.hit === true ? hot : i.hit === false ? ok : muted;
      L.circleMarker(p, { radius: 5, color: c, fillColor: c, fillOpacity: 1, weight: 0 }).addTo(map);
      L.marker(p, { icon: L.divIcon({ className: "", html: `<span class="apt-label" style="border-color:${c}">${esc(i.from)} · ${i.who === "you" ? "you" : "inbound"}${i.airborne ? " · airborne" : i.hit === true ? " · held" : i.hit === false ? " · not held" : ""}</span>`, iconSize: null, iconAnchor: [-8, 8] }) }).addTo(map);
      bounds.push(p);
    }
    const fit = () => bounds.length ? map.fitBounds(bounds, { padding: [24, 24], maxZoom: 6 }) : map.setView([39.5, -98.35], 3);
    fit();
    try { ARTCC = ARTCC || await api("api/artcc"); } catch { ARTCC = null; }
    if (!ARTCC || !MAPS.nas || MAPS.nas.map !== map) {
      const note = document.getElementById("nas-map-note");
      if (note && !ARTCC) { note.hidden = false; note.textContent = "Center boundaries aren't available right now; the scope is listed below."; }
      return;
    }
    const scope = new Set(n.scope || []);
    for (const [id, c] of Object.entries(ARTCC)) {
      const inS = scope.has(id);
      for (const ring of c.rings) {
        L.polygon(ring, { color: inS ? hot : muted, weight: inS ? 1.5 : 0.8, opacity: inS ? 0.9 : 0.5,
          fillColor: hot, fillOpacity: inS ? 0.22 : 0, interactive: false }).addTo(map);
        if (inS) ring.forEach((p) => bounds.push(p));
      }
      const big = c.rings.slice().sort((a, b) => b.length - a.length)[0];
      if (big && big.length) {
        const lat = big.reduce((a, p) => a + p[0], 0) / big.length, lon = big.reduce((a, p) => a + p[1], 0) / big.length;
        L.marker([lat, lon], { interactive: false, icon: L.divIcon({ className: "", iconSize: null,
          html: `<span class="ctr-lbl ${inS ? "on" : ""}" style="${inS ? `color:${hot}` : ""}">${esc(id)}</span>` }) }).addTo(map);
      }
    }
    fit();
  }

  async function legView(id) {
    let leg;
    try { leg = await api(`api/leg/${id}${LOG_ALL.has(id) ? "?log=1" : ""}`); } catch { $app.innerHTML = `<a class="back" href="#/">‹ Flights</a><div class="card empty"><b>Flight not found</b>It may have dropped off your schedule.</div>`; return; }
    LEGS[id] = leg;
    const f = leg.flight || { times: { out: {}, off: {}, on: {}, in: {} } };
    const sw = f.swim || {};
    const tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz;
    const cell = (lbl, t, tz) => {
      const v = t ? (t.actual || t.estimated || t.scheduled) : null;
      const k = t && t.actual ? "actual" : t && t.estimated ? "estimated" : t && t.scheduled ? "scheduled" : "";
      const d = x && x.scheduled && v ? (v - x.scheduled) / 60 : 0;
      return `<div class="tcell"><div class="lbl">${lbl}</div><div class="v ${d >= 5 ? "late" : d <= -5 ? "early" : ""}">${hm(v, tz)}</div><div class="s">${k}${t && t.scheduled && v && Math.abs(v - t.scheduled) >= 300 ? ` · sched ${hm(t.scheduled, tz)}` : ""}</div></div>`;
    };
    const wx = Object.entries(leg.wx || {}).map(([c, w]) => !w ? `<div style="margin-top:8px"><b>${esc(c)}</b> <span class="small faint">no METAR yet</span></div>` :
      `<div style="margin-top:8px"><b>${esc(c)}</b>${w.category ? `<span class="cat ${esc(w.category)}">${esc(w.category)}</span>` : ""}
       <span class="small muted"> ${w.wind_kt != null ? `${w.wind_dir ?? "VRB"}° ${w.wind_kt}${w.gust_kt ? `G${w.gust_kt}` : ""} kt` : ""}${w.visibility != null ? ` · ${esc(w.visibility)} SM` : ""}${w.ceiling_ft != null ? ` · ceiling ${w.ceiling_ft} ft` : ""}</span>
       <div class="metar">${esc(w.raw || "")}</div></div>`).join("");
    const kv = [
      ["Aircraft", [f.aircraft_name || f.aircraft_type, f.tail].filter(Boolean).join(" · ")],
      ["Tail from", f.tail_source || (leg.phase === "scheduled" ? "not known yet" : "")],
      ["Aircraft's day", (leg.chain || []).length ? leg.chain.map((c) => `${c.ident} ${c.route} (${c.phase === "scheduled" ? "not flown yet" : c.phase.replace("_", " ")})`).join(" ← ") : ""],
      [f.route_source === "FAA" ? "Route (ATC)" : "Filed route", f.route ? f.route + (sw.route_changed ? ` · amended ${hm(sw.route_changed, tzA)}` : "") : ""],
      ["Previous route", sw.route_prev || ""],
      ["United", sw.airline ? [sw.airline.gate_out && `pushed ${hm(sw.airline.gate_out, tzA)}`, sw.airline.in && `gate arrival est. ${hm(sw.airline.in, tzB)}`,
        sw.airline.gate_in && `at gate ${hm(sw.airline.gate_in, tzB)}`, `updated ${ago(sw.airline.at)}`].filter(Boolean).join(" · ") : ""],
      [`Departure (${leg.dep} tower)`, sw.dep && !(f.times.off || {}).actual ? [sw.dep.runway && `runway ${sw.dep.runway}`, sw.dep.fix && `via ${sw.dep.fix}`,
        sw.dep.est_off && `est. takeoff ${hm(sw.dep.est_off, tzA)}`, sw.dep.delay ? `${Math.round(sw.dep.delay)} min predicted delay` : "",
        sw.dep.taxi && `${Math.round(sw.dep.taxi)} min taxi`].filter(Boolean).join(" · ") : ""],
      [`Arrival (${leg.arr} tower)`, sw.arr && (sw.arr.runway || sw.arr.est_on) ? [sw.arr.runway && `runway ${sw.arr.runway}`,
        sw.arr.est_on && `est. landing ${fmt(sw.arr.est_on, tzB, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })}`,
        sw.arr.taxi && `${Math.round(sw.arr.taxi)} min taxi in`, sw.arr.at && `updated ${ago(sw.arr.at)}`].filter(Boolean).join(" · ") : ""],
      ["Landing estimate from", sw.eta_src === "tower" ? `${leg.arr} tower (TFDM)` : sw.eta_src === "FAA" ? "FAA (TFMS)" : ""],
      ["EDCT", sw.edct ? [`${hm(sw.edct.at, tzA)}`, sw.edct.prev && `was ${hm(sw.edct.prev, tzA)}`, sw.edct.for && `${sw.edct.for} program`,
        sw.edct.cta && `arrival slot ${hm(sw.edct.cta, tzB)}`].filter(Boolean).join(" · ") : ""],
      ["Clearance (datalink)", sw.pdc ? [sw.pdc.cleared_to && `cleared to ${sw.pdc.cleared_to}`, sw.pdc.departure && `via ${sw.pdc.departure}`,
        sw.pdc.altitude && `maintain ${sw.pdc.altitude}`, sw.pdc.fl && `FL${sw.pdc.fl}`, sw.pdc.dep_freq && `departure ${sw.pdc.dep_freq}`,
        sw.pdc.runway && `expect ${sw.pdc.runway}`, sw.pdc.squawk && `squawk ${sw.pdc.squawk}`, sw.pdc.edct && `EDCT ${sw.pdc.edct}Z`,
        sw.pdc.tail, sw.pdc.response && sw.pdc.response.toLowerCase(), sw.pdc.at && `at ${hm(Date.parse(sw.pdc.at) / 1000, tzA)}`]
        .filter(Boolean).join(" · ") : ""],
      ["Cleared route", sw.pdc && sw.pdc.route ? sw.pdc.route : ""],
      [`${leg.dep} ground`, [sw.tower_dep && sw.tower_dep.clearance && `cleared ${hm(sw.tower_dep.clearance, tzA)}`,
        sw.tower_dep && sw.tower_dep.gate && `gate ${sw.tower_dep.gate}`, sw.surf_dep && sw.surf_dep.ramp_out && `left ramp ${hm(sw.surf_dep.ramp_out, tzA)}`,
        sw.surf_dep && sw.surf_dep.took_off && `took off${sw.surf_dep.took_off_rwy ? " " + sw.surf_dep.took_off_rwy : ""} ${fmt(sw.surf_dep.took_off, tzA, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })}`]
        .filter(Boolean).join(" · ")],
      [`${leg.arr} ground`, sw.surf_arr ? [sw.surf_arr.landed && `landed${sw.surf_arr.landed_rwy ? " " + sw.surf_arr.landed_rwy : ""} ${fmt(sw.surf_arr.landed, tzB, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })}`,
        sw.surf_arr.runway_exit && `off runway ${hm(sw.surf_arr.runway_exit, tzB)}`, sw.surf_arr.ramp_in && `on ramp ${hm(sw.surf_arr.ramp_in, tzB)}`]
        .filter(Boolean).join(" · ") : ""],
      ["Filed altitude", f.filed_altitude ? `FL${String(Math.round(f.filed_altitude / (f.filed_altitude > 1000 ? 100 : 1))).padStart(3, "0")}` : ""],
      ["Filed speed", f.filed_speed ? `${Math.round(f.filed_speed)} kt` : ""],
      ["Distance", f.distance_nm ? `${Math.round(f.distance_nm)} nm` : ""],
      ["Live", leg.live ? `${leg.live.alt_ft ?? "?"} ft · ${leg.live.gs_kt ?? "?"} kt · ${ago(leg.live.at)} · ${leg.live.src || "adsb.lol"}` : ""],
      ["Data from", leg.source || ""],
      ["Updated", leg.updated ? ago(leg.updated) : "not yet"],
      ["Next check", leg.finished ? "none — tracking stops at takeoff" : leg.watching && leg.next_check ? hm(leg.next_check) : leg.watching ? "soon" : `starts ${STATE.settings.watch_hours} h before departure`],
      ["Added as", leg.extra ? `${leg.kind === "manual" ? "yours (kept in History)" : "a friend's flight"} · ${leg.date || "next departure"}` : ""],
      ["Calendar", leg.summary],
    ].filter(([, v]) => v);
    destroyMaps();
    $app.innerHTML = `
      <a class="back" href="#/">‹ Flights</a>
      ${nasTop([leg])}
      ${problemBanners(leg)}
      <div class="detail-map" id="detail-map"></div>
      <div class="card">
        <div class="hero-top"><div class="flightno">${esc(leg.display || leg.ident || "")}${leg.dh ? '<span class="tag">DH</span>' : ""}</div>${statusPill(leg)}</div>
        <div class="route">
          <div class="ap"><div class="code">${esc(leg.dep)}</div><div class="city">${esc(leg.dep_ap.name || "")}</div></div>
          <div class="mid"><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg><span class="dur">${notFound(leg) ? "" : dur(leg.sched_in - leg.sched_out)}</span></div>
          <div class="ap right"><div class="code">${esc(leg.arr)}</div><div class="city">${esc(leg.arr_ap.name || "")}</div></div>
        </div>
        ${progress(leg)}
        <div class="times">
          ${timeBlock(f.times.out && f.times.out.actual ? "Departed" : "Departs", f.times.out, notFound(leg) ? "nf" : leg.sched_out, tzA, false, (f.time_src || {}).out)}
          ${timeBlock(f.times.in && f.times.in.actual ? "Arrived" : "Arrives", f.times.in, notFound(leg) ? "nf" : leg.sched_in, tzB, true, (f.time_src || {}).in)}
        </div>
        <div class="times sm" ${notFound(leg) ? 'hidden' : ""}>
          ${timeBlock(f.times.off && f.times.off.actual ? "Took off" : "Takeoff", f.times.off, null, tzA, false, (f.time_src || {}).off)}
          ${timeBlock(f.times.on && f.times.on.actual ? "Landed" : "Landing", f.times.on, null, tzB, true, (f.time_src || {}).on)}
        </div>
        ${chips(leg)}
        ${countdown(leg)}
      </div>
      ${inboundCard(leg)}
      ${leg.route_stats ? `<div class="card small muted">You've flown ${esc(leg.dep)}–${esc(leg.arr)} ${leg.route_stats.flights} time${leg.route_stats.flights === 1 ? "" : "s"}${leg.route_stats.on_time_pct != null ? ` · ${leg.route_stats.on_time_pct}% on time · ${avgArr(leg.route_stats.avg_arr_delay)}` : ""}${leg.tail_count ? ` · ${esc((leg.flight || {}).tail || "")}: ${leg.tail_count} time${leg.tail_count === 1 ? "" : "s"} before` : ""}</div>` : ""}
      ${wx ? `<h2 class="section">Weather</h2><div class="card">${wx}</div>` : ""}
      <h2 class="section">Details</h2>
      <div class="card"><div class="kv">${kv.map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>
        ${leg.note ? `<p class="small muted">${esc(leg.note)}</p>` : ""}${leg.error ? `<p class="err">${esc(leg.error)}</p>` : ""}
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
          ${leg.finished ? "" : '<button class="btn" id="leg-refresh">Refresh this flight</button>'}
          ${STATE.aeroapi ? '<button class="btn" id="leg-aero">Check AeroAPI now</button>' : ""}
          ${leg.manual ? `<button class="btn" id="leg-kind">${leg.kind === "manual" ? "Make it a friend's flight" : "It's my flight"}</button><button class="btn" id="leg-remove">Remove</button>` : ""}
          ${leg.fa_id ? `<a class="btn" href="https://www.flightaware.com/live/flight/id/${encodeURIComponent(leg.fa_id)}" target="_blank" rel="noopener">FlightAware ↗</a>` : leg.ident ? `<a class="btn" href="https://www.flightaware.com/live/flight/${encodeURIComponent(leg.ident)}" target="_blank" rel="noopener">FlightAware ↗</a>` : ""}
        </div></div>
      ${nasMinor([leg]).length ? `<h2 class="section">Other NAS items</h2>${nasCard(nasMinor([leg]))}` : ""}
      ${(leg.alerts || []).length ? `<h2 class="section">Alerts for this flight</h2>${alertsCard(leg.alerts)}` : ""}
      ${(leg.history || []).length ? `<h2 class="section">Lookup log</h2><div class="card">${leg.history.map((h) => `<div class="hist">
          <div class="hist-top"><span class="mono small">${fmt(h.at, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}${h.quiet ? `–${fmt(h.last, undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}` : ""}</span>
            ${h.sources.map((x) => `<span class="src ${/AeroAPI/.test(x) ? "aero" : /Gemini/.test(x) ? "gem" : /ADS-B|adsb/.test(x) ? "adsb" : ""}">${esc(x)}</span>`).join("")}</div>
          ${(h.seen || []).length ? `<div class="hist-seen">${h.seen.map((x) => `<div><span class="faint">${esc(x.src)}</span> ${x.tail ? `<b>${esc(x.tail)}</b>${x.note ? ` <span class="faint">· ${esc(x.note)}</span>` : ""}` : `<span class="faint">${esc(x.note || "no tail")}</span>`}</div>`).join("")}</div>` : ""}
          ${h.changes.length ? `<div class="hist-ch">${h.changes.map((c) => `<div><span class="faint">${esc(c.field)}</span> ${c.from ? `<s>${esc(c.from)}</s> → ` : ""}<b>${esc(c.to)}</b>${c.via ? ` <span class="faint small">via ${esc(c.via)}</span>` : ""}</div>`).join("")}</div>`
            : `<div class="small faint">No changes${h.quiet ? ` · ${h.quiet} checks` : ""}</div>`}
          ${h.error ? `<div class="small err">${esc(h.error)}</div>` : h.note && !h.changes.length ? `<div class="small faint">${esc(h.note)}</div>` : ""}
        </div>`).join("")}${leg.history_more ? `<button class="btn" id="log-more" style="margin-top:10px">Show ${leg.history_more} older</button>` : ""}</div>` : ""}
      ${leg.shot ? `<h2 class="section">Latest screenshot</h2><div class="card shot"><img loading="lazy" alt="FlightAware page" onerror="this.closest('.card').previousElementSibling.remove();this.closest('.card').remove()" src="api/shot/${leg.id}.png?t=${Math.round(leg.updated || 0)}"></div>` : ""}
    `;
    drawMap(document.getElementById("detail-map"), withChain(leg), { key: `leg-${leg.id}` });
    surfaceWatch(leg);
    const lm = document.getElementById("log-more");
    if (lm) lm.onclick = async () => {
      lm.disabled = true; lm.textContent = "Loading…";
      try { LOG_ALL.add(leg.id); await render(true); } catch { lm.disabled = false; }
    };
    const ab = document.getElementById("leg-aero");
    if (ab) ab.onclick = async () => {
      if (!confirm("Ask AeroAPI about this flight now? It's one paid query (about half a cent), counted in your monthly budget.")) return;
      ab.disabled = true; ab.textContent = "Asking AeroAPI…";
      try {
        const r = await fetch(`api/aero/${encodeURIComponent(leg.id)}`, { method: "POST" });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || r.status);
        ab.textContent = `AeroAPI: ${d.status || "answered"}${d.tail ? " · " + d.tail : ""}`;
        setTimeout(() => render(true), 1500);
      } catch (e) { ab.textContent = "AeroAPI: " + e.message; ab.disabled = false; }
    };
    const rb = document.getElementById("leg-refresh");
    if (rb) rb.onclick = async (e) => {
      e.target.textContent = "Refreshing…";
      await api(`api/refresh/${leg.id}`, { method: "POST" });
      setTimeout(() => render(true), 25000);
    };
    const kb = document.getElementById("leg-kind");
    if (kb) kb.onclick = async () => {
      await api("api/flights/kind", { method: "POST", body: JSON.stringify({ id: leg.id, kind: leg.kind === "manual" ? "friend" : "manual" }) });
      render(true);
    };
    const xb = document.getElementById("leg-remove");
    if (xb) xb.onclick = async () => {
      await api("api/flights/remove", { method: "POST", body: JSON.stringify({ id: leg.id }) });
      go("#/");
    };
  }

  // ---------- add a flight ----------
  function addView() {
    destroyMaps();
    const manual = STATE.legs.filter((l) => l.manual);
    const today = new Date().toISOString().slice(0, 10);
    const earliest = new Date(Date.now() - 10 * 86400000).toISOString().slice(0, 10);
    $app.innerHTML = `
      <a class="back" href="#/">‹ Flights</a>
      <div class="card">
        <h3 style="margin:0 0 4px">Add a flight</h3>
        <p class="small muted" style="margin:0 0 14px">Flight number with its airline code, and the airports, so a flight number that flies several legs picks the right one. Leave the date blank for the next departure.</p>
        <form id="addform" class="form">
          <div class="seg" role="radiogroup">
            <label><input type="radio" name="f-kind" value="manual" checked><span>Mine</span></label>
            <label><input type="radio" name="f-kind" value="friend"><span>A friend's</span></label>
          </div>
          <p class="small faint" style="margin:-4px 0 4px" id="f-kind-help">Tracked like your calendar flights and kept in History.</p>
          <label>Flight number<input id="f-ident" placeholder="UA1234" autocapitalize="characters" autocomplete="off" required></label>
          <div class="form-2">
            <label>From<input id="f-dep" placeholder="EWR" maxlength="4" autocapitalize="characters" autocomplete="off" required></label>
            <label>To<input id="f-arr" placeholder="SFO" maxlength="4" autocapitalize="characters" autocomplete="off" required></label>
          </div>
          <label>Date (optional)<input id="f-date" type="date" min="${earliest}"></label>
          <p class="small faint" style="margin:-4px 0 0">Past flights work up to 10 days back (that's as far as AeroAPI and FlightAware's free pages go). They go straight into History.</p>
          <button class="btn primary" type="submit">Add</button>
          <div id="f-msg" class="small"></div>
        </form>
      </div>
      ${manual.length ? `<h2 class="section">Added in the app</h2>` + manual.map((l) => `<div class="row">
          <div class="when">${esc(l.display || l.ident)} ${kindTag(l)}<small>${l.date ? esc(new Date(l.date + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric" })) : "next"}</small></div>
          <div class="what"><div class="r">${esc(l.dep)} → ${esc(l.arr)}</div><div class="s">${esc(l.flight ? l.status : (l.note || "Looking up…"))}</div></div>
          <button class="btn" data-remove="${esc(l.id)}">Remove</button></div>`).join("") : ""}
      <p class="small faint" style="margin:16px 4px">Which pushes each kind gets is set on the bell page (Calendar, Manual, Friends). Your own flights are saved to History when they finish; friends' flights aren't. App-added flights leave this list a day and a half after departure.</p>`;
    $app.querySelectorAll('[name="f-kind"]').forEach((r) => r.onchange = () => {
      document.getElementById("f-kind-help").textContent = r.value === "manual" && r.checked ? "Tracked like your calendar flights and kept in History." : "Live tracking and pushes only; not saved to History.";
    });
    document.getElementById("addform").onsubmit = async (e) => {
      e.preventDefault();
      const msg = document.getElementById("f-msg");
      msg.className = "small muted"; msg.textContent = "Adding…";
      try {
        const r = await fetch("api/flights", { method: "POST", body: JSON.stringify({
          ident: document.getElementById("f-ident").value, dep: document.getElementById("f-dep").value,
          arr: document.getElementById("f-arr").value, date: document.getElementById("f-date").value,
          kind: (document.querySelector('[name="f-kind"]:checked') || {}).value || "manual" }) });
        changed("api/flights");
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || r.status);
        msg.textContent = "Added. Looking it up on FlightAware…";
        await load();
        setTimeout(() => go(`#/leg/${d.id}`), 600);
      } catch (err) { msg.className = "small err"; msg.textContent = err.message; }
    };
    $app.querySelectorAll("[data-remove]").forEach((b) => b.onclick = async () => {
      await api("api/flights/remove", { method: "POST", body: JSON.stringify({ id: b.dataset.remove }) });
      await load(); addView();
    });
  }

  // ---------- notification settings ----------
  async function notifyView() {
    destroyMaps();
    const d = await api("api/rules");
    const groups = [];
    for (const r of d.rules) {
      let g = groups.find((x) => x.name === r.group);
      if (!g) groups.push(g = { name: r.group, rows: [] });
      g.rows.push(r);
    }
    const kinds = [["calendar", "Calendar"], ["manual", "Manual"], ["friends", "Friends"]];
    const rowHtml = (r) => {
      const v = r.rule;
      return `<div class="nrow ${v.on ? "" : "off"}" data-key="${esc(r.key)}">
        <div class="nrow-head">
          <label class="switch"><input type="checkbox" data-f="on" ${v.on ? "checked" : ""}><span></span></label>
          <div class="nrow-txt"><div class="nrow-label">${esc(r.label)}</div><div class="small faint">${esc(r.desc)}</div></div>
        </div>
        <div class="nrow-opts">
          ${kinds.filter(([k]) => v[k] !== null && v[k] !== undefined).map(([k, lbl]) =>
            `<button type="button" class="kchip ${v[k] ? "on" : ""}" data-f="${k}">${lbl}</button>`).join("")}
          ${r.group === "System" && r.key !== "watch_start" ? "" : `<label class="hrs">from <input type="number" min="0" max="48" step="0.5" inputmode="decimal" data-f="hours" value="${v.hours ?? ""}" placeholder="any"> h out</label>`}
          <button type="button" class="kchip ts ${v.ts ? "on" : ""}" data-f="ts" title="Time-sensitive: breaks through Focus">⚡ Urgent</button>
          ${r.params.map((p) => `<label class="hrs">${esc(p.label)} <input type="number" min="0" inputmode="numeric" data-p="${esc(p.name)}" value="${v.params[p.name] ?? ""}"> ${esc(p.unit)}</label>`).join("")}
        </div>
      </div>`;
    };
    $app.innerHTML = `
      <a class="back" href="#/">‹ Flights</a>
      <div class="card">
        <h3 style="margin:0 0 4px">Notifications</h3>
        <p class="small muted" style="margin:0">Every change the tracker can see. For each: on or off, which flights it applies to,
          how close to departure it starts pushing (blank = any time), and whether it's urgent (time-sensitive on iPhone).
          Changes save as you make them. Tracking itself always runs; this only decides what pushes.</p>
        <div id="n-msg" class="small" style="margin-top:8px"></div>
      </div>
      ${groups.map((g) => fold(`notify-${g.name}`, g.name, `<div class="card nlist">${g.rows.map(rowHtml).join("")}</div>`, false,
        `<span class="fold-n">${g.rows.filter((r) => r.rule.on).length}/${g.rows.length} on</span>`)).join("")}
      <p class="small faint" style="margin:16px 4px">Gates assigned while a rule's window is closed are pushed once when it opens, with the gate at that moment. Ground stops push whatever their scope and say whether they include your departure or your inbound. Weather only applies to Calendar and Manual flights.</p>`;
    wireFolds();
    const msg = document.getElementById("n-msg");
    let timer = null;
    const pending = {};
    const save = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const body = { rules: { ...pending } };
        for (const k in pending) delete pending[k];
        try {
          const r = await fetch("api/rules", { method: "POST", body: JSON.stringify(body) });
        changed("api/rules");
          if (!r.ok) throw new Error(r.status);
          msg.className = "small muted"; msg.textContent = "Saved";
          setTimeout(() => { if (msg.textContent === "Saved") msg.textContent = ""; }, 1500);
        } catch (e) { msg.className = "small err"; msg.textContent = `Couldn't save: ${e.message}`; }
      }, 400);
    };
    $app.querySelectorAll(".nrow").forEach((row) => {
      const key = row.dataset.key;
      const upd = (patch) => { pending[key] = { ...(pending[key] || {}), ...patch, params: { ...((pending[key] || {}).params || {}), ...(patch.params || {}) } }; save(); };
      row.querySelector('[data-f="on"]').onchange = (e) => { row.classList.toggle("off", !e.target.checked); upd({ on: e.target.checked }); };
      row.querySelectorAll("button.kchip").forEach((b) => b.onclick = () => { b.classList.toggle("on"); upd({ [b.dataset.f]: b.classList.contains("on") }); });
      const h = row.querySelector('[data-f="hours"]');
      if (h) h.onchange = () => upd({ hours: h.value === "" ? null : h.value });
      row.querySelectorAll("[data-p]").forEach((i) => i.onchange = () => upd({ params: { [i.dataset.p]: i.value } }));
    });
  }

  // ---------- tools: test push + debug ----------
  function backupCard(b) {
    const when = b.last_backup ? `last backup ${ago(b.last_backup)}` : "no backup yet";
    return `<div class="card">
      ${!b.configured ? `<p class="small muted" style="margin:0 0 10px">Google Drive backup needs your own Google OAuth client: add <span class="mono">google_client_id</span> and <span class="mono">google_client_secret</span> in the add-on's Configuration tab and restart it. The client from the OOOI Flight Logger works (TVs and Limited Input devices type, Drive API on).</p>`
        : b.connected ? `<p class="small" style="margin:0 0 10px">Backing up to <b>${esc(b.folder)}</b>${b.email ? ` in ${esc(b.email)}'s Drive` : ""} · ${when}. It uploads about 15 s after any change (flights, History, settings) and once a day.${b.last_error ? `<br><span class="err">${esc(b.last_error)}</span>` : ""}</p>`
        : `<p class="small muted" style="margin:0 0 10px">Not connected.${b.last_error ? ` <span class="err">${esc(b.last_error)}</span>` : ""}</p>`}
      <div id="bk-code" class="small" style="margin-bottom:8px">${b.pending && !b.pending.error ? `Open <a href="${esc(b.pending.verification_url)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(b.pending.verification_url)}</a> and enter <b class="mono" style="font-size:18px">${esc(b.pending.user_code)}</b>` : b.pending && b.pending.error ? `<span class="err">${esc(b.pending.error)}</span>` : ""}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        ${b.configured && !b.connected ? '<button class="btn primary" id="bk-connect">Connect Google</button>' : ""}
        ${b.connected ? '<button class="btn primary" id="bk-now">Back up now</button><button class="btn" id="bk-restore">Restore from Drive</button>' : ""}
        ${b.connected && b.folder_url ? `<a class="btn" href="${esc(b.folder_url)}" target="_blank" rel="noopener">Open folder ↗</a>` : ""}
        <a class="btn" href="api/backup/download" download>Download backup</a>
        <label class="btn filebtn">Restore from file<input type="file" id="bk-file" accept=".zip" hidden></label>
        ${b.connected ? '<button class="btn" id="bk-disconnect">Disconnect</button>' : ""}
      </div>
      <div id="bk-msg" class="small" style="margin-top:8px"></div>
      <p class="small faint" style="margin:10px 0 0">The backup is one zip with all of History (flights and flown tracks) plus your alert and tracking settings, and a CSV of your flights. Restoring adds what's missing and keeps what's here.</p></div>`;
  }
  function wireBackup() {
    const msg = document.getElementById("bk-msg");
    const say = (t, err) => { if (msg) { msg.className = err ? "small err" : "small"; msg.textContent = t; } };
    const post = async (path, body, extra) => {
      const r = await fetch(path, { method: "POST", body, ...(extra || {}) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || r.status);
      return d;
    };
    const c = document.getElementById("bk-connect");
    if (c) c.onclick = async () => {
      try {
        const d = await post("api/backup/connect");
        document.getElementById("bk-code").innerHTML = `Open <a href="${esc(d.verification_url)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(d.verification_url)}</a> and enter <b class="mono" style="font-size:18px">${esc(d.user_code)}</b>. This page updates when you're done.`;
        const t = setInterval(async () => {
          const b = await api("api/backup").catch(() => null);
          if (b && (b.connected || (b.pending && b.pending.error))) { clearInterval(t); toolsView(); }
        }, 4000);
      } catch (e) { say(e.message, true); }
    };
    const n = document.getElementById("bk-now");
    if (n) n.onclick = async () => { say("Backing up…"); try { await post("api/backup/now"); say("Backed up."); setTimeout(toolsView, 800); } catch (e) { say(e.message, true); } };
    const rs = document.getElementById("bk-restore");
    if (rs) rs.onclick = async () => {
      if (!confirm("Restore from the Drive backup? Flights missing here are added; nothing here is removed.")) return;
      say("Restoring…"); try { const d = await post("api/backup/restore"); say(`Restored: ${d.added} flights added (${d.flights} in the backup).`); } catch (e) { say(e.message, true); }
    };
    const f = document.getElementById("bk-file");
    if (f) f.onchange = async () => {
      const file = f.files[0]; if (!file) return;
      say(`Restoring ${file.name}…`);
      try { const d = await post("api/backup/upload", await file.arrayBuffer()); say(`Restored: ${d.added} flights added (${d.flights} in the backup).`); } catch (e) { say(e.message, true); }
    };
    const x = document.getElementById("bk-disconnect");
    if (x) x.onclick = async () => { if (confirm("Disconnect Google Drive? The files already in Drive stay.")) { await post("api/backup/disconnect"); toolsView(); } };
  }

  async function toolsView() {
    destroyMaps();
    const [d, st, bk] = await Promise.all([api("api/debug"), api("api/settings").catch(() => ({ settings: [] })),
      api("api/backup").catch(() => ({ configured: false }))]);
    const sch = d.schedule || {};
    const c = d.config;
    const setRow = (x) => x.kind === "bool"
      ? `<div class="nrow"><div class="nrow-head"><label class="switch"><input type="checkbox" data-s="${esc(x.key)}" ${x.value ? "checked" : ""}><span></span></label>
          <div class="nrow-txt"><div class="nrow-label">${esc(x.label)}</div><div class="small faint">${esc(x.help)}</div></div></div></div>`
      : `<div class="nrow"><div class="set-num"><div class="nrow-txt"><div class="nrow-label">${esc(x.label)}</div><div class="small faint">${esc(x.help)}</div></div>
          <label class="hrs"><input type="number" min="${x.min}" max="${x.max}" inputmode="numeric" data-s="${esc(x.key)}" value="${x.value ?? ""}"> ${x.key === "watch_hours" ? "h" : x.key === "schedule_days" ? "days" : "min"}</label></div></div>`;
    const fetchRow = (x) => `<div class="fetch ${x.error ? "bad" : x.page_data === false ? "warn" : ""}">
      ${x.has_shot ? `<a href="api/shot/${esc(x.shot)}.png?t=${Math.round(x.at)}" target="_blank" rel="noopener"><img loading="lazy" src="api/shot/${esc(x.shot)}.png?t=${Math.round(x.at)}" alt="" onerror="this.parentNode.remove()"></a>` : ""}
      <div class="fx">
        <div><b>${esc(x.leg || "")}</b> · ${esc(x.what)} <span class="faint">${fmt(x.at, undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })}${x.ms ? ` · ${(x.ms / 1000).toFixed(1)} s` : ""}</span></div>
        <div class="small">${x.error ? `<span class="err">${esc(x.error)}</span>`
          : x.page_data === false ? `<span style="color:var(--amber)">Page loaded, but no flight data in it (CAPTCHA, sign-in or layout change?)</span>`
          : x.page_data ? `Page data ✓ · ${x.flights} flight${x.flights === 1 ? "" : "s"}${x.found ? ` · ${esc(x.found)}` : ""}` : esc(x.found || "")}</div>
        ${x.url ? `<div class="small faint mono" style="word-break:break-all">${esc(x.url)}</div>` : ""}
      </div></div>`;
    $app.innerHTML = `
      <a class="back" href="#/">‹ Flights</a>
      <h2 class="section"><span>Tracking</span><span id="s-msg" class="small"></span></h2>
      ${[...new Set(st.settings.map((x) => x.group || "Tracking"))].map((g, i) =>
        fold(`set-${g}`, g, `<div class="card nlist">${st.settings.filter((x) => (x.group || "Tracking") === g).map(setRow).join("")}</div>`, i === 0)).join("")}
      ${fold("set-backup", "Backup", backupCard(bk), !bk.connected)}
      ${STATE.swim && STATE.swim.status !== "off" ? fold("set-swim", "FAA SWIM", `<div class="card small">
        <b>${esc({ connected: "Connected", connecting: "Connecting…", error: "Not connected" }[STATE.swim.status] || STATE.swim.status)}</b>
        ${STATE.swim.status === "connected" ? ` · ${nm(STATE.swim.rate || 0)} messages a minute · ${STATE.swim.matched || 0} for your flights` : ""}
        ${STATE.swim.error ? `<p class="err" style="margin:6px 0 0">${esc(STATE.swim.error)}</p>` : ""}
        ${[["tfdm", "TFDM", "for your flights"], ["stdds", "STDDS", "for your airports"]].map(([k, name, what]) => STATE[k] && STATE[k].status !== "off" ? `<p style="margin:8px 0 0"><b>${name}: ${esc({ connected: "Connected", connecting: "Connecting…", error: "Not connected" }[STATE[k].status] || STATE[k].status)}</b>${STATE[k].status === "connected" ? ` · ${nm(STATE[k].rate || 0)} a minute · ${STATE[k].matched || 0} ${what}` : ""}</p>
        ${STATE[k].error ? `<p class="err" style="margin:6px 0 0">${esc(STATE[k].error)}</p>` : ""}` : "").join("")}
        <p class="faint" style="margin:8px 0 0">TFMS: the FAA's flight plans, amendments, takeoffs and landings, and the times United itself files. TFDM: tower data (runways, predicted delays, landing estimates) at airports that have it. STDDS: live surface positions, shown on a flight's map while it's on the ground.</p>
        <p style="margin:10px 0 0"><a class="btn" href="api/swim/samples.zip" download>Download sample messages</a></p>
        <p class="faint" style="margin:6px 0 0">A zip with a few messages of each type the feed has sent, and the last ones for your flights, to check they're read right.</p></div>`) : ""}
      <p class="small faint" style="margin:-4px 4px 14px">Alerts are on the bell page. Schedule file, notify service and API keys are in the add-on's Configuration tab in Home Assistant.</p>
      <div class="card">
        <h3 style="margin:0 0 4px">Notifications</h3>
        <p class="small muted" style="margin:0 0 12px">Sends to ${esc(c.notify)}.</p>
        <button class="btn primary" id="t-notify">Send test notification</button>
        <div id="t-msg" class="small" style="margin-top:8px"></div>
      </div>
      ${fold("tools-setup", "Setup check", `<div class="card"><div class="kv">
        <div class="k">Schedule file</div><div>${sch.exists ? "✓ " : '<span class="err">missing </span>'}<span class="mono small">${esc(sch.path)}</span></div>
        <div class="k">Read</div><div>${sch.checked ? ago(sch.checked) : "not yet"}${sch.events != null ? ` · ${sch.events} events, ${sch.count} flights` : ""}</div>
        ${sch.error ? `<div class="k">Error</div><div class="err">${esc(sch.error)}</div>` : ""}
        <div class="k">Airline</div><div>${esc(c.airline_icao)}</div>
        <div class="k">AeroAPI</div><div>${d.aeroapi.enabled
          ? `<b>$${d.aeroapi.spent.toFixed(3)}</b> of $${d.aeroapi.budget.toFixed(2)} this month · ${d.aeroapi.queries} quer${d.aeroapi.queries === 1 ? "y" : "ies"}`
          : "off (no key)"}</div>
        <div class="k">Gemini</div><div>${d.gemini.enabled
          ? `${esc(d.gemini.model)}, only when page data can't be read · <b>${d.gemini.today}</b> today, <b>${d.gemini.month}</b> this month${d.gemini.last ? ` · last ${ago(d.gemini.last)}` : " · never used yet"}`
          : "off (no API key), never used"}</div>
        <div class="k">Screenshots</div><div>${d.screenshots.count} files, ${(d.screenshots.bytes / 1048576).toFixed(1)} MB · deleted ${d.screenshots.keep_hours} h after a flight is done</div>
        <div class="k">Worker</div><div>${d.worker.busy ? "checking a flight now" : "idle"}${d.worker.last_cycle ? ` · last cycle ${ago(d.worker.last_cycle)}` : ""}${d.worker.error ? ` · <span class="err">${esc(d.worker.error)}</span>` : ""}</div>
        <div class="k">Tapping a push opens</div><div class="mono small">${esc(c.ingress)}</div>
        <div class="k">Browser viewer</div><div>http://&lt;your-ha-ip&gt;:${c.viewer_port}/vnc.html <span class="faint">(needs viewer_password)</span></div>
        <div class="k">Version</div><div>${esc(d.version)}</div>
      </div></div>`, true)}
      ${fold("tools-sched", "Flights found in your schedule", `<div class="card">${(sch.legs || []).length ? sch.legs.map((l) => `<div class="sched-row">
          <span class="mono small">${fmt(l.sched_out, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}</span>
          <span><b>${esc(l.ident || "—")}</b> ${esc(l.dep)}→${esc(l.arr)}${l.dh ? " DH" : ""} <span class="faint">${l.finished ? "done" : l.watching ? "watching" : ""}</span><br><span class="small faint">${esc(l.summary)}</span></span></div>`).join("")
        : '<div class="small muted">None coming up.</div>'}
        ${(sch.unmatched || []).length ? `<div class="small muted" style="margin-top:12px">Events not recognised as flights (fine if they're hotels, reserve, etc.):</div><div class="small faint mono">${sch.unmatched.map(esc).join("<br>")}</div>` : ""}
      </div>`)}
      ${d.aeroapi.enabled ? fold("tools-aero", "AeroAPI calls", `<div class="card">${d.aeroapi.calls.length ? d.aeroapi.calls.map((x) => `<div class="fetch ${x.ok === false ? "bad" : ""}"><div class="fx">
          <div><b>${esc(x.leg || "")}</b> · ${esc(x.reason)} <span class="faint">${fmt(x.at, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}</span></div>
          <div class="small">${x.ok === false ? `<span class="err">${esc(x.error)}</span>` : x.found ? `tail ${esc(x.tail || "not assigned yet")}${x.inbound ? " · inbound linked" : ""}` : "flight not found in AeroAPI"}${x.billed ? "" : ' <span class="faint">(not billed)</span>'}</div>
        </div></div>`).join("") : '<div class="small muted">No calls yet. The first one happens when a flight enters the watch window.</div>'}</div>`) : ""}
      ${fold("tools-pages", "Page loads & screenshots", `<div style="text-align:right;margin:-4px 0 8px"><button class="btn small" id="t-reload">Reload</button></div>
      <div class="card">${d.fetches.length ? d.fetches.map(fetchRow).join("") : '<div class="small muted">No pages loaded yet. Flights are checked from ' + STATE.settings.watch_hours + ' h before departure; add a flight to test now.</div>'}</div>`)}`;
    wireFolds();
    wireBackup();
    document.getElementById("t-notify").onclick = async (e) => {
      const msg = document.getElementById("t-msg");
      e.target.disabled = true; msg.className = "small muted"; msg.textContent = "Sending…";
      try {
        const r = await fetch("api/test-notify", { method: "POST" });
        const j = await r.json();
        msg.className = j.ok ? "small" : "small err";
        msg.textContent = j.ok ? `Sent via ${j.detail}. Check your phone.` : `Failed: ${j.detail}`;
      } catch (err) { msg.className = "small err"; msg.textContent = err.message; }
      e.target.disabled = false;
    };
    document.getElementById("t-reload").onclick = () => toolsView();
    const sMsg = document.getElementById("s-msg");
    $app.querySelectorAll("[data-s]").forEach((inp) => inp.onchange = async () => {
      const body = {};
      $app.querySelectorAll("[data-s]").forEach((x) => { body[x.dataset.s] = x.type === "checkbox" ? x.checked : x.value; });
      sMsg.className = "small muted"; sMsg.textContent = "Saving…";
      try {
        const r = await fetch("api/settings", { method: "POST", body: JSON.stringify({ settings: body }) });
        changed("api/settings");
        if (!r.ok) throw new Error(r.status);
        sMsg.textContent = "Saved"; setTimeout(() => { if (sMsg.textContent === "Saved") sMsg.textContent = ""; }, 1500);
        const fresh = await api("api/settings");
        for (const x of fresh.settings) { const el = $app.querySelector(`[data-s="${x.key}"]`); if (el && el.type !== "checkbox") el.value = x.value; }
      } catch (e) { sMsg.className = "small err"; sMsg.textContent = `Couldn't save: ${e.message}`; }
    });
  }

  // ---------- loop ----------
  // The last state is kept on the phone, so the app draws at once on open (even on a weak signal)
  // and then refreshes when the add-on answers.
  const SNAP_KEY = "ft-state-v1";
  function loadSnapshot() {
    try { const s = JSON.parse(localStorage.getItem(SNAP_KEY) || "null"); return s && s.legs ? s : null; } catch { return null; }
  }
  let snapT = null;
  function saveSnapshot(st) {
    clearTimeout(snapT);
    snapT = setTimeout(() => { try { localStorage.setItem(SNAP_KEY, JSON.stringify(st)); } catch {} }, 500);
  }
  async function load() {
    STATE = await api("api/state");
    saveSnapshot(STATE);
    $updated.textContent = STATE.worker && STATE.worker.busy ? "updating…" : "";
    setDrive(STATE.backup);
  }
  // The pill updates on every page (Settings and forms don't redraw on live events).
  let driveT = null;
  function driveSoon() {
    if (driveT || document.hidden) return;
    driveT = setTimeout(async () => { driveT = null; try { setDrive(await api("api/backup/brief")); } catch {} }, 300);
  }
  // Drive backup pill next to Live: green synced, amber changes waiting, blue syncing, red problem.
  const $drive = document.getElementById("drive");
  let LAST_DRIVE = null;
  function setDrive(b) {
    if (!$drive) return;
    LAST_DRIVE = b;
    const st = (b && b.state) || "off";
    $drive.hidden = st === "off";
    $drive.className = "live drive " + st;
    const when = b && b.last ? `last backup ${ago(b.last)}` : "no backup yet";
    $drive.title = { synced: `Drive: backed up · ${when}`, waiting: `Drive: changes waiting to upload (within 10 min) · ${when}`,
                     syncing: "Drive: uploading…", disconnected: "Drive: not connected. Tap to connect in Settings → Backup",
                     error: `Drive: ${(b && b.error) || "backup failed"} · ${when}` }[st] || "";
    $drive.querySelector("b").textContent = { syncing: "Syncing", error: "Drive !", disconnected: "Drive off" }[st] || "Drive";
  }

  async function render(force) {
    const route = ROUTE;
    try {
      if (force || !STATE) await load();
      morphing = route === lastRoute && $root.childElementCount > 0;
      const m = route.match(/^#\/leg\/([\w-]+)/);
      const ma = route.match(/^#\/aircraft\/([\w-]+)\/(\d+)/);
      const mh = route.match(/^#\/history(?:\/(stats|alerts))?(?:\/(\d{4}))?$/);
      const mf = route.match(/^#\/hist\/(.+)$/);
      const mn = route.match(/^#\/nas\/(.+)$/);
      if (route.startsWith("#/history/find")) await findView(route);
      else if (mh) await (mh[1] === "stats" ? statsView(mh[2] || "") : mh[1] === "alerts" ? alertsHistoryView() : historyView(mh[2] || ""));
      else if (mf) await histFlightView(decodeURIComponent(mf[1]));
      else if (mn) await nasView(decodeURIComponent(mn[1]));
      else if (ma) await aircraftView(ma[1], parseInt(ma[2], 10));
      else if (m) await legView(m[1]);
      else if (route.startsWith("#/add")) addView();
      else if (route.startsWith("#/tools")) await toolsView();
      else if (route.startsWith("#/notify")) await notifyView();
      else homeView();
      // hero map is drawn after its own fetch; give it a moment before clearing unused maps
      setTimeout(sweepMaps, 1500);
      const tab = /^#\/hist/.test(route) ? "history" : /^#\/notify/.test(route) ? "notify" : /^#\/tools/.test(route) ? "tools" : "flights";
      document.querySelectorAll("#tabbar a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
      if (restoreY != null) { const y = restoreY; restoreY = null; window.scrollTo(0, y); requestAnimationFrame(() => window.scrollTo(0, y)); }
      else if (route !== lastRoute) window.scrollTo(0, 0);
      lastRoute = route;
    } catch (e) {
      $app.innerHTML = `<div class="card empty"><b>Can't reach the add-on</b>${esc(e.message)}</div>`;
    } finally {
      morphing = false;
    }
  }

  $refresh.onclick = async () => {
    $refresh.classList.add("spin");
    try { await api("api/refresh", { method: "POST" }); } catch {}
    setTimeout(async () => { await render(true); $refresh.classList.remove("spin"); }, 20000);
  };
  // In-page navigation never touches the URL. Changing the address (even just the #hash)
  // makes Home Assistant reload the panel, which showed as a blank "Loading…" flash.
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest('a[href^="#"]');
    if (!a || e.defaultPrevented || e.metaKey || e.ctrlKey) return;
    e.preventDefault();
    const href = a.getAttribute("href") || "#/";
    if (a.classList.contains("back")) goBack(href);
    else go(href, { reset: !!a.closest("#tabbar") });
  });
  window.addEventListener("hashchange", () => { if (location.hash && location.hash !== ROUTE) go(location.hash); });

  // ---------- live updates ----------
  // The add-on pushes an event whenever anything changes; the page re-renders in place
  // (maps keep their zoom and pan). Views with forms aren't redrawn under your fingers.
  const LIVE_VIEW = () => !/^#\/(add|notify|tools|hist)/.test(ROUTE);
  let pending = null;
  // SWIM can change a flight every few seconds; one redraw every 2.5 s at most keeps the phone
  // responsive (and scrolling smooth) without missing anything
  let lastLive = 0;
  function liveRefresh() {
    if (pending || document.hidden || !LIVE_VIEW()) return;
    const wait = Math.max(400, 2500 - (Date.now() - lastLive));
    pending = setTimeout(async () => { pending = null; lastLive = Date.now(); await render(true); }, wait);
  }
  let es = null;
  const $live = document.getElementById("live");
  let lastEvent = null;
  function setLive(on, v) {
    if (v) lastEvent = Date.now();
    if (!$live) return;
    $live.classList.toggle("on", !!on);
    $live.title = on ? `Live: connected${lastEvent ? ` · last update ${Math.round((Date.now() - lastEvent) / 1000)} s ago` : ""}`
                     : "Not live: checking every 30 s";
    if (v) { $live.classList.remove("flash"); void $live.offsetWidth; $live.classList.add("flash"); }
  }
  // The server sends a ping every 15 s. iOS freezes a backgrounded page and can leave the
  // stream looking open but dead, so: reconnect at once when the page comes back, and whenever
  // nothing (not even a ping) has arrived for 35 s.
  let lastSeen = 0, retry = null;
  function drop() { if (es) { try { es.close(); } catch {} } es = null; }
  function connect(force) {
    if (!window.EventSource) return;
    if (es && !force) return;
    drop();
    clearTimeout(retry);
    try {
      es = new EventSource("api/events");
      es.onopen = () => { lastSeen = Date.now(); setLive(true); };
      es.onmessage = (e) => { lastSeen = Date.now(); setLive(true, e.data); liveRefresh(); driveSoon(); };
      es.addEventListener("ping", () => { lastSeen = Date.now(); setLive(true); });
      es.onerror = () => { setLive(false); drop(); retry = setTimeout(() => connect(), 2000); };
    } catch { es = null; }
  }
  let lastWake = 0;
  function wake() {
    if (document.hidden || Date.now() - lastWake < 1500) return;
    lastWake = Date.now();
    if (!es || Date.now() - lastSeen > 20000) { setLive(false); connect(true); }
    render(true);  // fresh data right away, not after the stream reconnects
  }
  connect();
  // Countdowns tick, and a fallback in case the stream is blocked somewhere.
  setInterval(liveRefresh, 30000);
  setInterval(driveSoon, 20000);
  setInterval(() => { if (!document.hidden && es && Date.now() - lastSeen > 35000) { setLive(false); connect(true); } }, 5000);
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("pageshow", (e) => { if (e.persisted) wake(); });
  window.addEventListener("focus", wake);
  window.addEventListener("online", () => connect(true));
  // Draw the last known state right away, then fetch the current one.
  const snap = loadSnapshot();
  if (snap) {
    STATE = snap;
    $updated.textContent = "updating…";
    render(false).then(() => render(true));
  } else render(true);
})();
