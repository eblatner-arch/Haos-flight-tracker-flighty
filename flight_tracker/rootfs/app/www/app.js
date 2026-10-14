/* Flight Tracker web app. Served through Home Assistant ingress, so every URL is relative. */
(() => {
  "use strict";
  const $app = document.getElementById("app");
  const $updated = document.getElementById("updated");
  const $refresh = document.getElementById("refresh");
  let STATE = null;
  let maps = [];
  let lastRoute = null;

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
  const destroyMaps = () => { maps.forEach((m) => m.remove()); maps = []; };

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
    const labels = L.tileLayer(esri(dark ? "World_Dark_Gray_Reference" : "World_Light_Gray_Reference"),
      { maxZoom: 16, pane: "overlayPane", opacity: 0.85 }).addTo(map);
    let errors = 0, switched = false;
    base.on("tileerror", () => {
      if (switched || ++errors < 4) return;
      switched = true;
      map.removeLayer(base); map.removeLayer(labels);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        { maxZoom: 16, className: dark ? "osm-dark" : "", attribution: "&copy; OpenStreetMap contributors" }).addTo(map);
    });
  }

  function drawMap(el, leg, { interactive = true } = {}) {
    if (!window.L) { el.innerHTML = '<div class="empty small">Map library didn\'t load.</div>'; return; }
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const map = L.map(el, { zoomControl: interactive, attributionControl: true, dragging: interactive,
      scrollWheelZoom: interactive, touchZoom: interactive, doubleClickZoom: interactive, boxZoom: false, keyboard: false });
    maps.push(map);
    addTiles(map, dark);
    const route = css("--route"), violet = css("--violet"), muted = css("--faint");
    const bounds = [];
    const A = ll(leg.dep_ap), B = ll(leg.arr_ap);
    const f = leg.flight || {};
    const wps = (f.waypoints || []).filter((p) => p[0] != null);
    if (wps.length > 1) {
      L.polyline(wps, { color: route, weight: 2, opacity: 0.55, dashArray: "6 6" }).addTo(map);
      wps.forEach((p) => bounds.push(p));
    } else if (A && B) {
      const g = gcPoints(A, B);
      L.polyline(g, { color: muted, weight: 2, opacity: 0.8, dashArray: "4 7" }).addTo(map);
      g.forEach((p) => bounds.push(p));
    }
    const track = (f.track || []).filter((p) => p[0] != null);
    if (track.length > 1) {
      L.polyline(track.map((p) => [p[0], p[1]]), { color: route, weight: 3.5, opacity: 0.95 }).addTo(map);
    }
    // inbound aircraft
    const inb = leg.inbound;
    if (inb) {
      const ia = ll(inb.origin), itr = (inb.track || []).filter((p) => p[0] != null);
      if (itr.length > 1) L.polyline(itr.map((p) => [p[0], p[1]]), { color: violet, weight: 2.5, opacity: 0.9 }).addTo(map);
      const iw = (inb.waypoints || []).filter((p) => p[0] != null);
      if (iw.length > 1) L.polyline(iw, { color: violet, weight: 1.5, opacity: 0.45, dashArray: "5 6" }).addTo(map);
      else if (ia && A) L.polyline(gcPoints(ia, A), { color: violet, weight: 1.5, opacity: 0.45, dashArray: "5 6" }).addTo(map);
      let ip = null, ih = 0;
      if (leg.inbound_live) { ip = [leg.inbound_live.lat, leg.inbound_live.lon]; ih = leg.inbound_live.heading; }
      else if (inb.phase === "airborne" && itr.length) { ip = itr[itr.length - 1]; ih = itr.length > 1 ? bearing(itr[itr.length - 2], ip) : 0; }
      if (ip) { planeMarker([ip[0], ip[1]], ih, violet, "Inbound").addTo(map); bounds.push([ip[0], ip[1]]); }
      if (ia && interactive) { bounds.push(ia); L.marker(ia, { icon: L.divIcon({ className: "", html: `<span class="apt-label" style="border-color:${violet}">${esc(inb.origin.iata || "")}</span>`, iconSize: null }) }).addTo(map); }
    }
    for (const [ap, pos] of [[leg.dep_ap, A], [leg.arr_ap, B]]) {
      if (!pos) continue;
      bounds.push(pos);
      L.circleMarker(pos, { radius: 4, color: route, fillColor: route, fillOpacity: 1, weight: 0 }).addTo(map);
      L.marker(pos, { icon: L.divIcon({ className: "", html: `<span class="apt-label">${esc(ap.iata || "")}</span>`, iconSize: null, iconAnchor: [-6, 8] }) }).addTo(map);
    }
    // our aircraft
    let pos = null, hdg = 0;
    if (leg.live) { pos = [leg.live.lat, leg.live.lon]; hdg = leg.live.heading; }
    else if (["airborne", "taxi_out", "landed"].includes(leg.phase) && track.length) {
      pos = track[track.length - 1]; hdg = track.length > 1 ? bearing(track[track.length - 2], pos) : 0;
    } else if (f.last_position && ["airborne", "taxi_out"].includes(leg.phase)) { pos = f.last_position; }
    if (pos) planeMarker([pos[0], pos[1]], hdg, route, leg.display).addTo(map);
    const center = () => bounds.length ? map.fitBounds(bounds, { padding: [28, 28], maxZoom: 9 }) : map.setView([39.5, -98.35], 3);
    center();
    if (interactive && L.Control) {
      const Btn = L.Control.extend({
        options: { position: "topright" },
        onAdd() {
          const b = L.DomUtil.create("button", "map-center");
          b.type = "button"; b.title = "Center on route"; b.setAttribute("aria-label", "Center on route");
          b.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm8.94 3A8.99 8.99 0 0 0 13 3.06V1h-2v2.06A8.99 8.99 0 0 0 3.06 11H1v2h2.06A8.99 8.99 0 0 0 11 20.94V23h2v-2.06A8.99 8.99 0 0 0 20.94 13H23v-2h-2.06zM12 19a7 7 0 1 1 0-14 7 7 0 0 1 0 14z"/></svg>';
          L.DomEvent.disableClickPropagation(b);
          L.DomEvent.on(b, "click", (e) => { L.DomEvent.preventDefault(e); center(); });
          return b;
        },
      });
      map.addControl(new Btn());
    }
    setTimeout(() => map.invalidateSize(), 50);
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
      <div class="sub">${kind} · ${tzAbbr(shown || s, tz)} · ${dayLabel(shown || s, tz)}</div>
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
    return `<div class="card inbound">
      <div class="head"><span class="title">Inbound aircraft</span><span class="pill ${slug(i.status)}">${esc(i.status)}</span></div>
      <div class="line">${esc(i.display_ident || "")} ${from ? `from ${esc(from)}` : ""}</div>
      <div class="small muted">${due ? `Due at ${esc(leg.dep)} ${hm(due, tz)} ${tzAbbr(due, tz)}` : "Arrival time not known yet"}${i.tail ? ` · ${esc(i.tail)}` : ""}${(i.destination || {}).gate ? ` · gate ${esc(i.destination.gate)}` : ""}</div>
      ${turn != null ? `<div class="turnbar ${cls}"><div class="bar"><span style="width:${pct}%"></span></div><div class="num">${dur(turn * 60)} turn</div></div>` : ""}
    </div>`;
  }

  function problemBanners(leg) {
    return (leg.problems || []).map((p) => `<div class="banner ${p.level}"><span class="dot ${p.level}"></span><div>${esc(p.text)}</div></div>`).join("");
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
          ${timeBlock("Departs", f.times.out, leg.sched_out, tzA)}
          ${timeBlock("Arrives", f.times.in, leg.sched_in, tzB, true)}
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
    // NAS only for airports on your own flights in the watch window.
    const myAirports = new Set(all.filter((l) => !l.extra && l.watching).flatMap((l) => [l.dep, l.arr]).filter(Boolean));
    let h = "";
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
      const mine = (STATE.nas || []).filter((n) => myAirports.has(n.airport));
      h += `<h2 class="section"><span>NAS · ${esc([...myAirports].join(" · "))}</span><span class="small">${STATE.nas_at ? ago(STATE.nas_at) : ""}</span></h2>`;
      h += mine.length ? nasCard(mine) : `<div class="card small muted">No ground stops, delay programs or closures at your airports.</div>`;
    }
    const done = all.filter((l) => l.finished).sort((a, b) => sortKey(b) - sortKey(a)).slice(0, 6);
    if (done.length) h += `<h2 class="section">Recent</h2>` + done.map(row).join("");
    if (STATE.alerts.length) h += `<h2 class="section">Recent alerts</h2>` + alertsCard(STATE.alerts);
    h += `<p class="small faint" style="margin:22px 4px">v${esc(STATE.version)} · alerts go to ${esc(STATE.notify_target)} · watching ${STATE.settings.watch_hours} h before departure${STATE.schedule && STATE.schedule.checked ? ` · schedule read ${ago(STATE.schedule.checked)}` : ""}</p>`;
    destroyMaps();
    $app.innerHTML = h;
    const hm_ = document.getElementById("hero-map");
    if (hm_ && next) {
      api(`api/leg/${next.id}`).then((full) => drawMap(hm_, full, { interactive: false })).catch(() => drawMap(hm_, next, { interactive: false }));
    }
  }

  async function legView(id) {
    let leg;
    try { leg = await api(`api/leg/${id}`); } catch { $app.innerHTML = `<a class="back" href="#/">‹ Flights</a><div class="card empty"><b>Flight not found</b>It may have dropped off your schedule.</div>`; return; }
    const f = leg.flight || { times: { out: {}, off: {}, on: {}, in: {} } };
    const tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz;
    const cell = (lbl, t, tz) => {
      const v = t ? (t.actual || t.estimated || t.scheduled) : null;
      const k = t && t.actual ? "actual" : t && t.estimated ? "estimated" : t && t.scheduled ? "scheduled" : "";
      return `<div class="tcell"><div class="lbl">${lbl}</div><div class="v">${hm(v, tz)}</div><div class="s">${k}${t && t.scheduled && v && Math.abs(v - t.scheduled) >= 300 ? ` · sched ${hm(t.scheduled, tz)}` : ""}</div></div>`;
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
      ${problemBanners(leg)}
      <div class="detail-map" id="detail-map"></div>
      <div class="card">
        <div class="hero-top"><div class="flightno">${esc(leg.display || leg.ident || "")}${leg.dh ? '<span class="tag">DH</span>' : ""}</div>${statusPill(leg)}</div>
        <div class="route">
          <div class="ap"><div class="code">${esc(leg.dep)}</div><div class="city">${esc(leg.dep_ap.name || "")}</div></div>
          <div class="mid"><span class="dur">${dur(leg.sched_in - leg.sched_out)}</span></div>
          <div class="ap right"><div class="code">${esc(leg.arr)}</div><div class="city">${esc(leg.arr_ap.name || "")}</div></div>
        </div>
        ${progress(leg)}
        <div class="grid4" style="margin-top:14px">
          ${cell(`Gate out · ${tzAbbr(best(f.times, "out") || leg.sched_out, tzA)}`, f.times.out.scheduled ? f.times.out : { scheduled: leg.sched_out }, tzA)}
          ${cell(`Gate in · ${tzAbbr(best(f.times, "in") || leg.sched_in, tzB)}`, f.times.in.scheduled ? f.times.in : { scheduled: leg.sched_in }, tzB)}
          ${cell("Takeoff", f.times.off, tzA)}
          ${cell("Landing", f.times.on, tzB)}
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
      ${(leg.nas || []).length ? `<h2 class="section">NAS at your airports</h2><div class="card">${leg.nas.map((n) => `<div class="nas-item mine"><div class="apt">${esc(n.airport)}</div><div><div class="lbl">${esc(n.label)}</div><div class="det">${esc([n.detail, n.reason].filter(Boolean).join(" · "))}</div></div></div>`).join("")}</div>` : ""}
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
    drawMap(document.getElementById("detail-map"), leg);
    const rb = document.getElementById("leg-refresh");
    if (rb) rb.onclick = async (e) => {
      e.target.textContent = "Refreshing…";
      await api(`api/refresh/${leg.id}`, { method: "POST" });
      setTimeout(() => render(true), 25000);
    };
    const xb = document.getElementById("leg-remove");
    if (xb) xb.onclick = async () => {
      await api("api/flights/remove", { method: "POST", body: JSON.stringify({ id: leg.id }) });
      location.hash = "#/";
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
        setTimeout(() => { location.hash = `#/leg/${d.id}`; }, 600);
      } catch (err) { msg.className = "small err"; msg.textContent = err.message; }
    };
    $app.querySelectorAll("[data-remove]").forEach((b) => b.onclick = async () => {
      await api("api/flights/remove", { method: "POST", body: JSON.stringify({ id: b.dataset.remove }) });
      await load(); addView();
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
    const route = location.hash || "#/";
    try {
      if (force || !STATE) await load();
      const m = route.match(/^#\/leg\/([\w-]+)/);
      if (m) await legView(m[1]);
      else if (route.startsWith("#/add")) addView();
      else if (route.startsWith("#/tools")) await toolsView();
      else homeView();
      if (route !== lastRoute) window.scrollTo(0, 0);
      lastRoute = route;
    } catch (e) {
      $app.innerHTML = `<div class="card empty"><b>Can't reach the add-on</b>${esc(e.message)}</div>`;
    }
  }

  $refresh.onclick = async () => {
    $refresh.classList.add("spin");
    try { await api("api/refresh", { method: "POST" }); } catch {}
    setTimeout(async () => { await render(true); $refresh.classList.remove("spin"); }, 20000);
  };
  window.addEventListener("hashchange", () => render(false));
  // Re-render on a timer, but not while the user is panning a detail map.
  setInterval(() => { if (!document.hidden && ["", "#/", "#"].includes(location.hash)) render(true); }, 30000);
  setInterval(() => { if (!document.hidden && (location.hash || "").startsWith("#/leg/")) render(true); }, 120000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) render(true); });
  render(true);
})();
