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
  function go(r) {
    r = r || "#/";
    ROUTE = r;
    render(false);
  }

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

  async function api(path, opts) {
    const r = await fetch(path, { cache: "no-store", ...opts });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
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
    const map = L.map(el, { zoomControl: interactive, attributionControl: true, dragging: interactive,
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

  function timeBlock(label, t, sched, tz, right) {
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
    drawMap(document.getElementById("ac-map"), { dep_ap: o, arr_ap: d, flight: h, phase: h.phase, inbound: null, live: null, display: h.display_ident }, { key: `ac-${id}-${n}` });
  }

  function problemBanners(leg) {
    return (leg.problems || []).filter((p) => !p.nas).map((p) => `<div class="banner ${p.level}"><span class="dot ${p.level}"></span><div>${esc(p.text)}</div></div>`).join("");
  }

  function hero(leg) {
    const f = leg.flight || { times: {} }, tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz;
    const block = (leg.sched_in - leg.sched_out);
    return `<a class="card hero" href="#/leg/${leg.id}">
      <div class="map" id="hero-map"></div>
      <div class="hero-body">
        <div class="hero-top">
          <div class="flightno">${esc(leg.display || leg.ident || "Flight")}${leg.dh ? '<span class="tag">DH</span>' : ""}${leg.extra ? '<span class="tag">FOLLOWING</span>' : ""}</div>
          ${statusPill(leg)}
        </div>
        <div class="route">
          <div class="ap"><div class="code">${esc(leg.dep)}</div><div class="city">${esc(leg.dep_ap.city || leg.dep_ap.name || "")}</div></div>
          <div class="mid"><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg><span class="dur">${dur(block)}</span></div>
          <div class="ap right"><div class="code">${esc(leg.arr)}</div><div class="city">${esc(leg.arr_ap.city || leg.arr_ap.name || "")}</div></div>
        </div>
        ${progress(leg)}
        <div class="times">
          ${timeBlock(f.times.out && f.times.out.actual ? "Departed" : "Departs", f.times.out, leg.sched_out, tzA)}
          ${timeBlock(f.times.in && f.times.in.actual ? "Arrived" : f.times.on && f.times.on.actual ? "Landed" : "Arrives", f.times.in, leg.sched_in, tzB, true)}
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
    const tags = (leg.dh ? '<span class="tag">DH</span>' : "") + (leg.extra ? '<span class="tag">FOLLOWING</span>' : "")
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
    return `<div class="card">${sorted.map((n) => `<div class="nas-item mine">
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
      if (i.center) hits.add(i.center);
      hits.add(i.from);
      const who = i.who === "you" ? `${esc(u.leg.display || u.leg.ident || "Your flight")} from ${esc(i.from)}` : `${esc(u.leg.display || "Your flight")}'s inbound from ${esc(i.from)}`;
      const where = i.center ? ` (${esc(i.center)})` : "";
      lines.push(i.hit === true ? `<div class="na-hit yes">Includes ${who}${where}</div>`
        : i.hit === false ? `<div class="na-hit no">Doesn't include ${who}${where} — yet</div>`
        : `<div class="na-hit">Can't tell if it includes ${who}</div>`);
    }
    if (!lines.length) lines.push(...g.uses.map((u) => `<div class="na-hit">${esc(u.leg.display || u.leg.ident || "")} ${esc(u.leg.dep)}→${esc(u.leg.arr)}</div>`));
    const when = n.end ? `${n.start && n.start > Date.now() / 1000 ? hm(n.start, tz) + "–" : "until "}${hm(n.end, tz)} ${tzAbbr(n.end, tz)}` : "";
    const sub = [n.reason && n.reason[0].toUpperCase() + n.reason.slice(1), n.detail, n.prob && `extension probability ${n.prob.toLowerCase()}`].filter(Boolean).join(" · ");
    const scope = (n.scope || []).length ? `<div class="na-scope"><span class="faint">Departures from</span> ${n.scope.map((c) => `<span class="cc ${hits.has(c) ? "on" : ""}">${esc(c)}</span>`).join("")}</div>`
      : n.flights ? `<div class="na-scope faint">${esc(n.flights)}</div>` : "";
    return `<div class="nas-alert k-${esc(n.kind)}">
      <div class="na-top"><span class="na-apt">${esc(n.airport)}</span><span class="na-kind">${esc(n.label)}</span><span class="na-when">${esc(when)}</span></div>
      ${sub ? `<div class="na-sub">${esc(sub)}</div>` : ""}
      ${scope}${lines.join("")}
      ${n.advisory ? `<a class="na-adv" href="${esc(n.advisory)}" target="_blank" rel="noopener">FAA advisory ↗</a>` : ""}
    </div>`;
  }
  const nasTop = (legs) => nasGroups(legs).filter((g) => MAJOR.includes(g.n.kind)).map(nasAlert).join("");
  const nasMinor = (legs) => nasGroups(legs).filter((g) => !MAJOR.includes(g.n.kind)).map((g) => g.n);

  function alertsCard(list) {
    if (!list.length) return "";
    return `<div class="card">${list.slice(0, 15).map((a) => `<div class="alert">
      <span class="dot ${a.level}"></span>
      <div><div class="ttl">${esc(a.title)}</div><div class="msg">${esc(a.message)}</div></div>
      <div class="at">${fmt(a.at, undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}<br>${fmt(a.at, undefined, { day: "numeric", month: "short" })}</div>
    </div>`).join("")}</div>`;
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
      h += `<div class="card empty"><b>No upcoming flights</b>Nothing in the next three weeks${STATE.schedule && STATE.schedule.count === 0 ? " (no flights found in your schedule)" : ""}. Tap + to follow a flight.</div>`;
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
      const minor = nasMinor(watching), major = nasGroups(watching).length - minor.length;
      h += `<h2 class="section"><span>NAS · ${esc([...myAirports].join(" · "))}</span><span class="small">${STATE.nas_at ? ago(STATE.nas_at) : ""}</span></h2>`;
      h += minor.length ? nasCard(minor) : `<div class="card small muted">${major ? "Programs shown at the top. No other delays at your airports." : "No ground stops, delay programs or closures at your airports."}${STATE.nas_error ? `<div class="err">${esc(STATE.nas_error)}</div>` : ""}</div>`;
    }
    const done = all.filter((l) => l.finished).sort((a, b) => sortKey(b) - sortKey(a)).slice(0, 6);
    if (done.length) h += `<h2 class="section">Recent</h2>` + done.map(row).join("");
    if (STATE.alerts.length) h += `<h2 class="section">Recent alerts</h2>` + alertsCard(STATE.alerts);
    h += `<p class="small faint" style="margin:22px 4px">v${esc(STATE.version)} · alerts go to ${esc(STATE.notify_target)} · watching ${STATE.settings.watch_hours} h before departure${STATE.schedule && STATE.schedule.checked ? ` · schedule read ${ago(STATE.schedule.checked)}` : ""}</p>`;
    destroyMaps();
    $app.innerHTML = h;
    const hm_ = document.getElementById("hero-map");
    if (hm_ && next) {
      const opt = { interactive: false, key: `hero-${next.id}` };
      // draw straight away from the last full copy so the map never shows blank, then update
      if (LEGS[next.id]) drawMap(hm_, LEGS[next.id], opt);
      api(`api/leg/${next.id}`).then((full) => { LEGS[next.id] = full; drawMap(document.getElementById("hero-map"), full, opt); })
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
    const wx = Object.entries(leg.wx || {}).filter(([, w]) => w).map(([c, w]) =>
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
      ["Followed for", leg.extra ? (leg.date || "the next departure") : ""],
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
          <div class="mid"><svg viewBox="0 0 24 24"><path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"/></svg><span class="dur">${dur(leg.sched_in - leg.sched_out)}</span></div>
          <div class="ap right"><div class="code">${esc(leg.arr)}</div><div class="city">${esc(leg.arr_ap.name || "")}</div></div>
        </div>
        ${progress(leg)}
        <div class="times">
          ${timeBlock(f.times.out && f.times.out.actual ? "Departed" : "Departs", f.times.out, leg.sched_out, tzA)}
          ${timeBlock(f.times.in && f.times.in.actual ? "Arrived" : "Arrives", f.times.in, leg.sched_in, tzB, true)}
        </div>
        <div class="times sm">
          ${timeBlock(f.times.off && f.times.off.actual ? "Took off" : "Takeoff", f.times.off, null, tzA)}
          ${timeBlock(f.times.on && f.times.on.actual ? "Landed" : "Landing", f.times.on, null, tzB, true)}
        </div>
        ${chips(leg)}
        ${countdown(leg)}
      </div>
      ${inboundCard(leg)}
      ${wx ? `<h2 class="section">Weather</h2><div class="card">${wx}</div>` : ""}
      <h2 class="section">Details</h2>
      <div class="card"><div class="kv">${kv.map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>
        ${leg.note ? `<p class="small muted">${esc(leg.note)}</p>` : ""}${leg.error ? `<p class="err">${esc(leg.error)}</p>` : ""}
        <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
          ${leg.finished ? "" : '<button class="btn" id="leg-refresh">Refresh this flight</button>'}
          ${leg.manual ? '<button class="btn" id="leg-remove">Stop following</button>' : ""}
          ${leg.fa_id ? `<a class="btn" href="https://www.flightaware.com/live/flight/id/${encodeURIComponent(leg.fa_id)}" target="_blank" rel="noopener">FlightAware ↗</a>` : leg.ident ? `<a class="btn" href="https://www.flightaware.com/live/flight/${encodeURIComponent(leg.ident)}" target="_blank" rel="noopener">FlightAware ↗</a>` : ""}
        </div></div>
      ${nasMinor([leg]).length ? `<h2 class="section">Delays at these airports</h2>${nasCard(nasMinor([leg]))}` : ""}
      ${(leg.alerts || []).length ? `<h2 class="section">Alerts for this flight</h2>${alertsCard(leg.alerts)}` : ""}
      ${(leg.history || []).length ? `<h2 class="section">Lookup log</h2><div class="card">${leg.history.map((h) => `<div class="hist">
          <div class="hist-top"><span class="mono small">${fmt(h.at, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}${h.quiet ? `–${fmt(h.last, undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}` : ""}</span>
            ${h.sources.map((x) => `<span class="src ${/AeroAPI/.test(x) ? "aero" : /Gemini/.test(x) ? "gem" : /ADS-B/.test(x) ? "adsb" : ""}">${esc(x)}</span>`).join("")}</div>
          ${h.changes.length ? `<div class="hist-ch">${h.changes.map((c) => `<div><span class="faint">${esc(c.field)}</span> ${c.from ? `<s>${esc(c.from)}</s> → ` : ""}<b>${esc(c.to)}</b>${c.via ? ` <span class="faint small">via ${esc(c.via)}</span>` : ""}</div>`).join("")}</div>`
            : `<div class="small faint">No changes${h.quiet ? ` · ${h.quiet} checks` : ""}</div>`}
          ${h.error ? `<div class="small err">${esc(h.error)}</div>` : h.note && !h.changes.length ? `<div class="small faint">${esc(h.note)}</div>` : ""}
        </div>`).join("")}</div>` : ""}
      ${leg.shot ? `<h2 class="section">Latest screenshot</h2><div class="card shot"><img loading="lazy" alt="FlightAware page" onerror="this.closest('.card').previousElementSibling.remove();this.closest('.card').remove()" src="api/shot/${leg.id}.png?t=${Math.round(leg.updated || 0)}"></div>` : ""}
    `;
    drawMap(document.getElementById("detail-map"), leg, { key: `leg-${leg.id}` });
    const rb = document.getElementById("leg-refresh");
    if (rb) rb.onclick = async (e) => {
      e.target.textContent = "Refreshing…";
      await api(`api/refresh/${leg.id}`, { method: "POST" });
      setTimeout(() => render(true), 25000);
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
    $app.innerHTML = `
      <a class="back" href="#/">‹ Flights</a>
      <div class="card">
        <h3 style="margin:0 0 4px">Follow a flight</h3>
        <p class="small muted" style="margin:0 0 14px">Flight number with its airline code, and the airports, so a flight number that flies several legs picks the right one. Leave the date blank for the next departure.</p>
        <form id="addform" class="form">
          <label>Flight number<input id="f-ident" placeholder="UA1234" autocapitalize="characters" autocomplete="off" required></label>
          <div class="form-2">
            <label>From<input id="f-dep" placeholder="EWR" maxlength="4" autocapitalize="characters" autocomplete="off" required></label>
            <label>To<input id="f-arr" placeholder="SFO" maxlength="4" autocapitalize="characters" autocomplete="off" required></label>
          </div>
          <label>Date (optional)<input id="f-date" type="date" min="${today}"></label>
          <button class="btn primary" type="submit">Follow</button>
          <div id="f-msg" class="small"></div>
        </form>
      </div>
      ${manual.length ? `<h2 class="section">Following from the app</h2>` + manual.map((l) => `<div class="row">
          <div class="when">${esc(l.display || l.ident)}<small>${l.date ? esc(new Date(l.date + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric" })) : "next"}</small></div>
          <div class="what"><div class="r">${esc(l.dep)} → ${esc(l.arr)}</div><div class="s">${esc(l.flight ? l.status : (l.note || "Looking up…"))}</div></div>
          <button class="btn" data-remove="${esc(l.id)}">Remove</button></div>`).join("") : ""}
      <p class="small faint" style="margin:16px 4px">Followed flights get pushes for gate changes, delays, pushback and takeoff, then tracking stops. App-added flights clear themselves a day and a half after departure.</p>`;
    document.getElementById("addform").onsubmit = async (e) => {
      e.preventDefault();
      const msg = document.getElementById("f-msg");
      msg.className = "small muted"; msg.textContent = "Adding…";
      try {
        const r = await fetch("api/flights", { method: "POST", body: JSON.stringify({
          ident: document.getElementById("f-ident").value, dep: document.getElementById("f-dep").value,
          arr: document.getElementById("f-arr").value, date: document.getElementById("f-date").value }) });
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
    const kinds = [["mine", "My legs"], ["dh", "Deadheads"], ["followed", "Followed"]];
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
      ${groups.map((g) => `<h2 class="section">${esc(g.name)}</h2><div class="card nlist">${g.rows.map(rowHtml).join("")}</div>`).join("")}
      <p class="small faint" style="margin:16px 4px">Gates assigned while a rule's window is closed are pushed once when it opens, with the gate at that moment. Ground stops push whatever their scope and say whether they include your departure or your inbound. Weather only applies to flights you're working or deadheading.</p>`;
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
  async function toolsView() {
    destroyMaps();
    const d = await api("api/debug");
    const sch = d.schedule || {};
    const c = d.config;
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
      <div class="card">
        <h3 style="margin:0 0 4px">Notifications</h3>
        <p class="small muted" style="margin:0 0 12px">Sends to ${esc(c.notify)}.</p>
        <button class="btn primary" id="t-notify">Send test notification</button>
        <div id="t-msg" class="small" style="margin-top:8px"></div>
      </div>
      <h2 class="section">Setup check</h2>
      <div class="card"><div class="kv">
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
      </div></div>
      <h2 class="section">Flights found in your schedule</h2>
      <div class="card">${(sch.legs || []).length ? sch.legs.map((l) => `<div class="sched-row">
          <span class="mono small">${fmt(l.sched_out, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}</span>
          <span><b>${esc(l.ident || "—")}</b> ${esc(l.dep)}→${esc(l.arr)}${l.dh ? " DH" : ""} <span class="faint">${l.finished ? "done" : l.watching ? "watching" : ""}</span><br><span class="small faint">${esc(l.summary)}</span></span></div>`).join("")
        : '<div class="small muted">None in the next three weeks.</div>'}
        ${(sch.unmatched || []).length ? `<div class="small muted" style="margin-top:12px">Events not recognised as flights (fine if they're hotels, reserve, etc.):</div><div class="small faint mono">${sch.unmatched.map(esc).join("<br>")}</div>` : ""}
      </div>
      ${d.aeroapi.enabled ? `<h2 class="section">AeroAPI calls</h2><div class="card">${d.aeroapi.calls.length ? d.aeroapi.calls.map((x) => `<div class="fetch ${x.ok === false ? "bad" : ""}"><div class="fx">
          <div><b>${esc(x.leg || "")}</b> · ${esc(x.reason)} <span class="faint">${fmt(x.at, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}</span></div>
          <div class="small">${x.ok === false ? `<span class="err">${esc(x.error)}</span>` : x.found ? `tail ${esc(x.tail || "not assigned yet")}${x.inbound ? " · inbound linked" : ""}` : "flight not found in AeroAPI"}${x.billed ? "" : ' <span class="faint">(not billed)</span>'}</div>
        </div></div>`).join("") : '<div class="small muted">No calls yet. The first one happens when a flight enters the watch window.</div>'}</div>` : ""}
      <h2 class="section"><span>Page loads &amp; screenshots</span><button class="btn small" id="t-reload">Reload</button></h2>
      <div class="card">${d.fetches.length ? d.fetches.map(fetchRow).join("") : '<div class="small muted">No pages loaded yet. Flights are checked from ' + STATE.settings.watch_hours + ' h before departure; add a flight to test now.</div>'}</div>`;
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
  }

  // ---------- loop ----------
  async function load() {
    STATE = await api("api/state");
    $updated.textContent = STATE.worker && STATE.worker.busy ? "updating…" : "";
  }

  async function render(force) {
    const route = ROUTE;
    try {
      if (force || !STATE) await load();
      morphing = route === lastRoute && $root.childElementCount > 0;
      const m = route.match(/^#\/leg\/([\w-]+)/);
      const ma = route.match(/^#\/aircraft\/([\w-]+)\/(\d+)/);
      if (ma) await aircraftView(ma[1], parseInt(ma[2], 10));
      else if (m) await legView(m[1]);
      else if (route.startsWith("#/add")) addView();
      else if (route.startsWith("#/tools")) await toolsView();
      else if (route.startsWith("#/notify")) await notifyView();
      else homeView();
      // hero map is drawn after its own fetch; give it a moment before clearing unused maps
      setTimeout(sweepMaps, 1500);
      if (route !== lastRoute) window.scrollTo(0, 0);
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
    go(a.getAttribute("href") || "#/");
  });
  window.addEventListener("hashchange", () => { if (location.hash && location.hash !== ROUTE) go(location.hash); });

  // ---------- live updates ----------
  // The add-on pushes an event whenever anything changes; the page re-renders in place
  // (maps keep their zoom and pan). Views with forms aren't redrawn under your fingers.
  const LIVE_VIEW = () => !/^#\/(add|notify|tools)/.test(ROUTE);
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
  function connect() {
    if (es || !window.EventSource) return;
    try {
      es = new EventSource("api/events");
      es.onopen = () => setLive(true);
      es.onmessage = (e) => { setLive(true, e.data); liveRefresh(); };
      es.onerror = () => { setLive(false); try { es.close(); } catch {} es = null; setTimeout(connect, 8000); };
    } catch { es = null; }
  }
  connect();
  // Countdowns tick, and a fallback in case the stream is blocked somewhere.
  setInterval(liveRefresh, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { connect(); liveRefresh(); } });
  render(true);
})();
