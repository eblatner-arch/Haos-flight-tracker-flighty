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
    if (!prev) { ROUTE = fallback || "#/"; render(false); return; }
    ROUTE = prev.route;
    restoreY = prev.y;
    render(false);
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
    $root.style.transition = "none";
    $root.style.transform = `translateX(${sw.dx * 0.6}px)`;
    $root.style.opacity = String(1 - Math.min(0.4, sw.dx / 600));
  }, { capture: true, passive: false });
  document.addEventListener("touchend", () => {
    if (!sw) return;
    const go_ = sw.horiz && sw.dx > Math.min(110, window.innerWidth * 0.28), href = sw.back.getAttribute("href");
    sw = null;
    $root.style.transition = "transform .18s ease, opacity .18s ease";
    $root.style.transform = ""; $root.style.opacity = "";
    if (go_) goBack(href);
  }, { capture: true, passive: true });
  document.addEventListener("touchcancel", () => { sw = null; $root.style.transform = ""; $root.style.opacity = ""; }, { capture: true, passive: true });

  async function api(path, opts) {
    const r = await fetch(path, { cache: "no-store", ...opts });
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

  // Free basemaps, no API key: Esri's gray canvas, falling back to OpenStreetMap if it won't load.
  function addTiles(map, dark) {
    const esri = (name) => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${name}/MapServer/tile/{z}/{y}/{x}`;
    const base = L.tileLayer(esri(dark ? "World_Dark_Gray_Base" : "World_Light_Gray_Base"),
      { maxZoom: 16, attribution: "Tiles &copy; Esri" }).addTo(map);
    if (!map.getPane("labels")) { map.createPane("labels"); map.getPane("labels").style.zIndex = 350; map.getPane("labels").style.pointerEvents = "none"; }
    const labels = L.tileLayer(esri(dark ? "World_Dark_Gray_Reference" : "World_Light_Gray_Reference"),
      { maxZoom: 16, pane: "labels", opacity: 0.85 }).addTo(map);
    let errors = 0, switched = false;
    base.on("tileerror", () => {
      if (switched || ++errors < 4) return;
      switched = true;
      map.removeLayer(base); map.removeLayer(labels);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        { maxZoom: 16, className: dark ? "osm-dark" : "", attribution: "&copy; OpenStreetMap contributors" }).addTo(map);
    });
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
    }
    paintMap(entry, leg, true);
    setTimeout(() => map.invalidateSize(), 50);
  }

  function paintMap(entry, leg, fit) {
    const { layer, interactive } = entry;
    layer.clearLayers();
    const add = (x) => x.addTo(layer);
    const route = css("--route"), violet = css("--violet"), muted = css("--faint");
    const bounds = [];
    const A = ll(leg.dep_ap), B = ll(leg.arr_ap);
    const f = leg.flight || {};
    const wps = (f.waypoints || []).filter((p) => p[0] != null);
    if (wps.length > 1) {
      add(L.polyline(wps, { color: route, weight: 2, opacity: 0.55, dashArray: "6 6" }));
      wps.forEach((p) => bounds.push(p));
    } else if (A && B) {
      const g = gcPoints(A, B);
      add(L.polyline(g, { color: muted, weight: 2, opacity: 0.8, dashArray: "4 7" }));
      g.forEach((p) => bounds.push(p));
    }
    const track = (f.track || []).filter((p) => p[0] != null);
    if (track.length > 1) add(L.polyline(track.map((p) => [p[0], p[1]]), { color: route, weight: 3.5, opacity: 0.95 }));
    const inb = leg.inbound;
    if (inb) {
      const ia = ll(inb.origin), itr = (inb.track || []).filter((p) => p[0] != null);
      if (itr.length > 1) add(L.polyline(itr.map((p) => [p[0], p[1]]), { color: violet, weight: 2.5, opacity: 0.9 }));
      const iw = (inb.waypoints || []).filter((p) => p[0] != null);
      if (iw.length > 1) add(L.polyline(iw, { color: violet, weight: 1.5, opacity: 0.45, dashArray: "5 6" }));
      else if (ia && A) add(L.polyline(gcPoints(ia, A), { color: violet, weight: 1.5, opacity: 0.45, dashArray: "5 6" }));
      let ip = null, ih = 0;
      if (leg.inbound_live) { ip = [leg.inbound_live.lat, leg.inbound_live.lon]; ih = leg.inbound_live.heading; }
      else if (inb.phase === "airborne" && itr.length) { ip = itr[itr.length - 1]; ih = itr.length > 1 ? bearing(itr[itr.length - 2], ip) : 0; }
      if (ip) { add(planeMarker([ip[0], ip[1]], ih, violet, "Inbound")); bounds.push([ip[0], ip[1]]); }
      if (ia && interactive) { bounds.push(ia); add(L.marker(ia, { icon: L.divIcon({ className: "", html: `<span class="apt-label" style="border-color:${violet}">${esc(inb.origin.iata || "")}</span>`, iconSize: null }) })); }
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
        bounds.push([p[0], p[1]]);
      }
      if (ca) { bounds.push(ca); add(L.marker(ca, { icon: L.divIcon({ className: "", html: `<span class="apt-label faint" style="border-color:${violet}">${esc(c.origin.iata || "")}</span>`, iconSize: null }) })); }
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
    if (pos) add(planeMarker([pos[0], pos[1]], hdg, route, leg.display));
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
  function timeBlock(label, t, sched, tz, right) {
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
    const pct = turn == null ? 0 : Math.max(4, Math.min(100, (turn / Math.max(minTurn * 2, 60)) * 100));
    const from = (i.origin || {}).iata || "";
    return `<a class="card inbound" href="#/aircraft/${leg.id}/0">
      <div class="head"><span class="title">Inbound aircraft ›</span><span class="pill ${slug(i.status)}">${esc(i.status)}</span></div>
      <div class="line">${esc(i.display_ident || "")} ${from ? `from ${esc(from)}` : ""}</div>
      <div class="small muted">${due ? `Due at ${esc(leg.dep)} ${hm(due, tz)} ${tzAbbr(due, tz)}` : "Arrival time not known yet"}${i.tail ? ` · ${esc(i.tail)}` : ""}${(i.destination || {}).gate ? ` · gate ${esc(i.destination.gate)}` : ""}</div>
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
    try { leg = await api(`api/leg/${id}`); } catch { go("#/"); return; }
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
      lines.push(i.airborne ? `<div class="na-hit air">${who} is already airborne — not held</div>`
        : i.hit === true ? `<div class="na-hit yes">Includes ${who}${where}</div>`
        : i.hit === false ? `<div class="na-hit no">Doesn't include ${who}${where} — yet</div>`
        : `<div class="na-hit">Can't tell if it includes ${who}</div>`);
    }
    // A ground stop / delay program holds flights *to* the airport; leaving it isn't held.
    for (const u of g.uses) if (["gs", "gdp"].includes(n.kind) && u.leg.dep === n.airport)
      lines.push(`<div class="na-hit air">Doesn't hold ${esc(u.leg.display || u.leg.ident || "your flight")} leaving ${esc(n.airport)}${(u.impact || []).length ? "" : " — only flights headed here"}</div>`);
    if (!lines.length) lines.push(...g.uses.map((u) => `<div class="na-hit">${esc(u.leg.display || u.leg.ident || "")} ${esc(u.leg.dep)}→${esc(u.leg.arr)}</div>`));
    const when = n.end ? `${n.start && n.start > Date.now() / 1000 ? hm(n.start, tz) + "–" : "until "}${hm(n.end, tz)} ${tzAbbr(n.end, tz)}` : "";
    const sub = [n.reason && n.reason[0].toUpperCase() + n.reason.slice(1), n.detail, n.prob && `extension probability ${n.prob.toLowerCase()}`].filter(Boolean).join(" · ");
    const scope = (n.scope || []).length ? `<div class="na-scope"><span class="faint">Departures from</span> ${n.scope.map((c) => `<span class="cc ${hits.has(c) ? "on" : ""}">${esc(c)}</span>`).join("")}</div>`
      : n.flights ? `<div class="na-scope faint">${esc(n.flights)}</div>` : "";
    const holds = g.uses.some((u) => u.impact.some((i) => i.hit !== false));
    return `<div class="nas-alert k-${esc(n.kind)} ${holds ? "" : "clear"}">
      <div class="na-top"><span class="na-apt">${esc(n.airport)}</span><span class="na-kind">${esc(n.label)}</span><span class="na-when">${esc(when)}</span></div>
      ${sub ? `<div class="na-sub">${esc(sub)}</div>` : ""}
      ${scope}${lines.join("")}
      ${n.advisory ? `<a class="na-adv" href="${esc(n.advisory)}" target="_blank" rel="noopener">FAA advisory ↗</a>` : ""}
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
  function wireFolds() {
    $app.querySelectorAll("details[data-fold]").forEach((d) => d.addEventListener("toggle", () => {
      try { localStorage.setItem(openKey(d.dataset.fold), d.open ? "1" : "0"); } catch {}
    }));
  }

  // ---------- history ----------
  const nm = (x) => x == null ? "—" : Math.round(x).toLocaleString();
  const mins = (m) => m == null ? "—" : `${m > 0 ? "+" : ""}${Math.round(m)}`;
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
        msg.className = "small"; msg.textContent = d.tracks != null ? `${d.added} tracks added, ${d.kept} flights already had one${d.unknown ? `, ${d.unknown} for flights not in History (import those flights first)` : ""}.${d.partial ? " The file was cut off in the download, so the tracks at its end are missing: download it again and import that copy to get the rest." : ""}` : `${d.added} added, ${d.already} already here${d.problems.length ? `, ${d.problems.length} rows skipped` : ""}.`;
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
      ${tile("Avg arrival", st.avg_arr_delay == null ? "—" : mins(st.avg_arr_delay) + " min", `departure ${mins(st.avg_dep_delay)} min`, { sort: "late" })}
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
    const [rd, ra] = (P.get("route") || "").toUpperCase().split("-");
    const g = (k) => (P.get(k) || "").trim();
    const minD = g("min") === "" ? null : parseFloat(g("min")), maxD = g("max") === "" ? null : parseFloat(g("max"));
    const out = all.filter((f) => {
      if (g("year") && !(f.date || "").startsWith(g("year"))) return false;
      if (g("mo") && !(f.date || "").startsWith(g("mo"))) return false;
      if (rd && (f.dep !== rd || (ra && f.arr !== ra))) return false;
      if (g("dep") && f.dep !== g("dep").toUpperCase()) return false;
      if (g("arr") && f.arr !== g("arr").toUpperCase() && f.diverted_to !== g("arr").toUpperCase()) return false;
      if (g("ap") && ![f.dep, f.arr, f.diverted_to].includes(g("ap").toUpperCase())) return false;
      if (g("tail") && (f.tail || "").toUpperCase() !== g("tail").toUpperCase()) return false;
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
    const sts = (P.get("st") || "").split(",").filter(Boolean);
    const nFilters = [...P.keys()].filter((k) => !["q", "st", "sort"].includes(k)).length;
    let h = histTabs("find", P.get("year") || "") + `
      <input class="search" id="f-q" type="search" placeholder="Flight, airport, tail, aircraft, month, >30…" value="${esc(P.get("q") || "")}" autocomplete="off" autocapitalize="characters">
      <div class="chips-row wrap">${ST_CHIPS.map(([k, l]) => `<button class="ychip ${sts.includes(k) ? "on" : ""}" data-st="${k}">${l}</button>`).join("")}</div>
      ${fold("find-more", `More filters${nFilters ? ` · ${nFilters}` : ""}`, `<div class="card ffgrid">
        ${opt("year", uniq((f) => (f.date || "").slice(0, 4)).reverse(), "Year")}
        ${opt("mo", uniq((f) => (f.date || "").slice(0, 7)).reverse(), "Month", (v) => fmt(Date.parse(v + "-15") / 1000, undefined, { month: "short", year: "numeric" }))}
        ${opt("dep", uniq((f) => f.dep), "From")}
        ${opt("arr", uniq((f) => f.diverted_to || f.arr), "To")}
        ${opt("ap", uniq((f) => f.dep).concat(uniq((f) => f.arr)).filter((v, i, a) => a.indexOf(v) === i).sort(), "Either airport")}
        ${opt("route", uniq((f) => `${f.dep}-${f.arr}`), "Route")}
        ${opt("ac", uniq((f) => f.aircraft || f.type), "Aircraft")}
        ${opt("tail", uniq((f) => f.tail), "Tail")}
        ${opt("hr", HOURS, "Departs (local)")}
        ${opt("dow", [0, 1, 2, 3, 4, 5, 6], "Day", (v) => DOW[v])}
        <label class="ff"><span>Late at least</span><input data-k="min" type="number" inputmode="numeric" placeholder="min" value="${esc(P.get("min") || "")}"></label>
        <label class="ff"><span>Late at most</span><input data-k="max" type="number" inputmode="numeric" placeholder="min (−5 = early)" value="${esc(P.get("max") || "")}"></label>
        <label class="ff"><span>Sort</span><select data-k="sort">${[["new", "Newest"], ["old", "Oldest"], ["late", "Latest arrival"], ["early", "Earliest arrival"], ["long", "Longest distance"], ["block", "Longest gate to gate"]]
          .map(([v, l]) => `<option value="${v}" ${(P.get("sort") || "new") === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        ${[...P.keys()].length ? `<a class="btn" href="#/history/find" id="f-clear">Clear all</a>` : ""}
      </div>`, nFilters > 0)}
      <div id="f-out"></div>`;
    $app.innerHTML = h;
    wireFolds();
    const draw = () => {
      const res = applyFind(all, P);
      const dl = res.map((f) => f.arr_delay).filter((v) => v != null);
      const ot = dl.length ? Math.round(100 * dl.filter((v) => v < 15).length / dl.length) : null;
      const avg = dl.length ? dl.reduce((a, b) => a + b, 0) / dl.length : null;
      const dist = res.reduce((a, f) => a + (f.cancelled ? 0 : f.distance_nm || 0), 0);
      let o = `<div class="find-sum"><b>${res.length}</b> flight${res.length === 1 ? "" : "s"}${ot != null ? ` · ${ot}% on time · avg ${mins(avg)} min` : ""}${dist ? ` · ${nm(dist)} nm` : ""}</div>`;
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
    $app.querySelectorAll(".ffgrid [data-k]").forEach((el) => el.onchange = () => {
      P.set(el.dataset.k, el.value);
      commit();
      const n = [...P.keys()].filter((k) => !["q", "st", "sort"].includes(k)).length;
      const t = $app.querySelector('[data-fold="find-more"] summary > span');
      if (t) t.textContent = `More filters${n ? ` · ${n}` : ""}`;
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
        <div class="k">This route</div><div>${rs.flights} flight${rs.flights === 1 ? "" : "s"}${rs.on_time_pct != null ? ` · ${rs.on_time_pct}% on time · avg ${mins(rs.avg_arr_delay)} min` : ""}</div>
        ${f.route ? `<div class="k">Filed route</div><div class="mono small">${esc(f.route)}</div>` : ""}
        <div class="k">Source</div><div>${f.source === "flighty" ? "Flighty import" : "Tracked here"}${f.has_track ? " · flown track saved" : ""}</div>
      </div>
      <div style="margin-top:12px"><button class="btn" id="h-del">Delete from History</button></div></div>`;
    drawMap(document.getElementById("hist-map"), { dep_ap: f.dep_ap || {}, arr_ap: f.arr_ap || {}, flight: { track: f.track || [], waypoints: [], times: {} }, phase: "arrived", inbound: null, chain: [], live: null, display: f.display },
      { key: `hist-${f.id}` });
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

  async function legView(id) {
    let leg;
    try { leg = await api(`api/leg/${id}`); } catch { $app.innerHTML = `<a class="back" href="#/">‹ Flights</a><div class="card empty"><b>Flight not found</b>It may have dropped off your schedule.</div>`; return; }
    LEGS[id] = leg;
    const f = leg.flight || { times: { out: {}, off: {}, on: {}, in: {} } };
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
      ["Filed route", f.route],
      ["Filed altitude", f.filed_altitude ? `FL${String(Math.round(f.filed_altitude / (f.filed_altitude > 1000 ? 100 : 1))).padStart(3, "0")}` : ""],
      ["Filed speed", f.filed_speed ? `${Math.round(f.filed_speed)} kt` : ""],
      ["Distance", f.distance_nm ? `${Math.round(f.distance_nm)} nm` : ""],
      ["Live", leg.live ? `${leg.live.alt_ft ?? "?"} ft · ${leg.live.gs_kt ?? "?"} kt · ${ago(leg.live.at)}` : ""],
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
          ${timeBlock(f.times.out && f.times.out.actual ? "Departed" : "Departs", f.times.out, notFound(leg) ? "nf" : leg.sched_out, tzA)}
          ${timeBlock(f.times.in && f.times.in.actual ? "Arrived" : "Arrives", f.times.in, notFound(leg) ? "nf" : leg.sched_in, tzB, true)}
        </div>
        <div class="times sm" ${notFound(leg) ? 'hidden' : ""}>
          ${timeBlock(f.times.off && f.times.off.actual ? "Took off" : "Takeoff", f.times.off, null, tzA)}
          ${timeBlock(f.times.on && f.times.on.actual ? "Landed" : "Landing", f.times.on, null, tzB, true)}
        </div>
        ${chips(leg)}
        ${countdown(leg)}
      </div>
      ${inboundCard(leg)}
      ${leg.route_stats ? `<div class="card small muted">You've flown ${esc(leg.dep)}–${esc(leg.arr)} ${leg.route_stats.flights} time${leg.route_stats.flights === 1 ? "" : "s"}${leg.route_stats.on_time_pct != null ? ` · ${leg.route_stats.on_time_pct}% on time · avg ${mins(leg.route_stats.avg_arr_delay)} min` : ""}${leg.tail_count ? ` · ${esc((leg.flight || {}).tail || "")}: ${leg.tail_count} time${leg.tail_count === 1 ? "" : "s"} before` : ""}</div>` : ""}
      ${wx ? `<h2 class="section">Weather</h2><div class="card">${wx}</div>` : ""}
      <h2 class="section">Details</h2>
      <div class="card"><div class="kv">${kv.map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>
        ${leg.note ? `<p class="small muted">${esc(leg.note)}</p>` : ""}${leg.error ? `<p class="err">${esc(leg.error)}</p>` : ""}
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
          ${leg.finished ? "" : '<button class="btn" id="leg-refresh">Refresh this flight</button>'}
          ${leg.manual ? `<button class="btn" id="leg-kind">${leg.kind === "manual" ? "Make it a friend's flight" : "It's my flight"}</button><button class="btn" id="leg-remove">Remove</button>` : ""}
          ${leg.fa_id ? `<a class="btn" href="https://www.flightaware.com/live/flight/id/${encodeURIComponent(leg.fa_id)}" target="_blank" rel="noopener">FlightAware ↗</a>` : leg.ident ? `<a class="btn" href="https://www.flightaware.com/live/flight/${encodeURIComponent(leg.ident)}" target="_blank" rel="noopener">FlightAware ↗</a>` : ""}
        </div></div>
      ${nasMinor([leg]).length ? `<h2 class="section">Other NAS items</h2>${nasCard(nasMinor([leg]))}` : ""}
      ${(leg.alerts || []).length ? `<h2 class="section">Alerts for this flight</h2>${alertsCard(leg.alerts)}` : ""}
      ${(leg.history || []).length ? `<h2 class="section">Lookup log</h2><div class="card">${leg.history.map((h) => `<div class="hist">
          <div class="hist-top"><span class="mono small">${fmt(h.at, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}${h.quiet ? `–${fmt(h.last, undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}` : ""}</span>
            ${h.sources.map((x) => `<span class="src ${/AeroAPI/.test(x) ? "aero" : /Gemini/.test(x) ? "gem" : /ADS-B|adsb/.test(x) ? "adsb" : /Flightradar24/.test(x) ? "fr24" : ""}">${esc(x)}</span>`).join("")}</div>
          ${(h.seen || []).length ? `<div class="hist-seen">${h.seen.map((x) => `<div><span class="faint">${esc(x.src)}</span> ${x.tail ? `<b>${esc(x.tail)}</b>` : `<span class="faint">${esc(x.note || "no tail")}</span>`}</div>`).join("")}</div>` : ""}
          ${h.changes.length ? `<div class="hist-ch">${h.changes.map((c) => `<div><span class="faint">${esc(c.field)}</span> ${c.from ? `<s>${esc(c.from)}</s> → ` : ""}<b>${esc(c.to)}</b>${c.via ? ` <span class="faint small">via ${esc(c.via)}</span>` : ""}</div>`).join("")}</div>`
            : `<div class="small faint">No changes${h.quiet ? ` · ${h.quiet} checks` : ""}</div>`}
          ${h.error ? `<div class="small err">${esc(h.error)}</div>` : h.note && !h.changes.length ? `<div class="small faint">${esc(h.note)}</div>` : ""}
        </div>`).join("")}</div>` : ""}
      ${leg.shot ? `<h2 class="section">Latest screenshot</h2><div class="card shot"><img loading="lazy" alt="FlightAware page" onerror="this.closest('.card').previousElementSibling.remove();this.closest('.card').remove()" src="api/shot/${leg.id}.png?t=${Math.round(leg.updated || 0)}"></div>` : ""}
    `;
    drawMap(document.getElementById("detail-map"), withChain(leg), { key: `leg-${leg.id}` });
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
  async function load() {
    STATE = await api("api/state");
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
      if (route.startsWith("#/history/find")) await findView(route);
      else if (mh) await (mh[1] === "stats" ? statsView(mh[2] || "") : mh[1] === "alerts" ? alertsHistoryView() : historyView(mh[2] || ""));
      else if (mf) await histFlightView(decodeURIComponent(mf[1]));
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
  function liveRefresh() {
    if (pending || document.hidden || !LIVE_VIEW()) return;
    pending = setTimeout(async () => { pending = null; await render(true); }, 400);
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
  render(true);
})();
