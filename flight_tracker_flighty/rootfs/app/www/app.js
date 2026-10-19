/* Flight Tracker Flighty web app. Served through Home Assistant ingress, so every URL is relative. */
(() => {
  "use strict";
  const $root = document.getElementById("app");
  try { if (window.top !== window) document.documentElement.classList.add("in-frame"); } catch { document.documentElement.classList.add("in-frame"); }
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
  const keepNode = () => false;
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
  const LEGS = {};  // last full copy of each flight, for instant redraws
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
  const hm = (ts, tz) => fmt(ts, tz, CLOCK24 ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } : { hour: "numeric", minute: "2-digit", hour12: true });
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
  const destroyMaps = () => {};

  const EDGE = 24;  // px from the left edge where a swipe means "back"

  // Swipe in from the left edge to go back (to wherever the page's "‹" or ✕ link points), like iOS.
  // On map pages the sheet slides away and the map stays put.
  let sw = null;
  const swipeEl = () => document.body.classList.contains("sheetmode") ? ($root.querySelector(".sheet") || $root) : $root;
  const unstyle = (el) => { el.style.transition = ""; el.style.transform = ""; el.style.opacity = ""; el.style.willChange = ""; };
  document.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    const back = document.querySelector("#app a.back");
    sw = e.touches.length === 1 && back && t.clientX <= EDGE ? { x: t.clientX, y: t.clientY, dx: 0, back, horiz: null, el: swipeEl() } : null;
  }, { capture: true, passive: true });
  document.addEventListener("touchmove", (e) => {
    if (!sw) return;
    const t = e.touches[0], dx = t.clientX - sw.x, dy = t.clientY - sw.y;
    if (sw.horiz === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) sw.horiz = Math.abs(dx) > Math.abs(dy);
    if (!sw.horiz) { if (sw.horiz === false) { unstyle(sw.el); sw = null; } return; }
    if (e.cancelable) e.preventDefault();  // keep the page from scrolling sideways while swiping
    sw.dx = Math.max(0, dx);
    if (sw.raf) return;
    sw.raf = requestAnimationFrame(() => {  // one paint per frame, following the finger 1:1
      if (!sw) return;
      sw.raf = 0;
      sw.el.style.transition = "none";
      sw.el.style.willChange = "transform";
      sw.el.style.transform = `translate3d(${sw.dx}px,0,0)`;
      sw.el.style.opacity = String(1 - Math.min(0.35, sw.dx / 900));
    });
  }, { capture: true, passive: false });
  document.addEventListener("touchend", () => {
    if (!sw) return;
    const go_ = sw.horiz && sw.dx > Math.min(110, window.innerWidth * 0.28), href = sw.back.getAttribute("href"), el = sw.el;
    if (sw.raf) cancelAnimationFrame(sw.raf);
    sw = null;
    if (!go_) {
      el.style.transition = "transform .2s cubic-bezier(.2,.8,.2,1), opacity .2s";
      el.style.transform = ""; el.style.opacity = "";
      return;
    }
    // slide the page off, then show the one underneath sliding in from a little to the left
    el.style.transition = "transform .16s ease-out, opacity .16s ease-out";
    el.style.transform = `translate3d(${window.innerWidth}px,0,0)`; el.style.opacity = "0.6";
    setTimeout(async () => {
      unstyle(el);
      $root.style.transition = "none"; $root.style.opacity = "0";
      await Promise.race([goBack(href), new Promise((r) => setTimeout(r, 1500))]);
      const inEl = swipeEl();
      inEl.style.transition = "none"; inEl.style.transform = "translate3d(-24px,0,0)";
      requestAnimationFrame(() => {
        $root.style.transition = "opacity .2s"; $root.style.opacity = "";
        inEl.style.transition = "transform .2s cubic-bezier(.2,.8,.2,1)"; inEl.style.transform = "";
        setTimeout(() => { unstyle(inEl); unstyle($root); }, 260);
      });
    }, 150);
  }, { capture: true, passive: true });
  document.addEventListener("touchcancel", () => { if (sw) unstyle(sw.el); sw = null; $root.style.transform = ""; $root.style.opacity = ""; }, { capture: true, passive: true });

  // ---------- notifications straight to this phone (Web Push) ----------
  // A Home Screen web app subscribes with the add-on's key; the add-on then sends alerts through
  // Apple's (or Google's) push service, as "Flighty", and a tap opens this app on that flight.
  const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const b64u8 = (s) => { const b = atob((s + "=".repeat((4 - s.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from(b, (c) => c.charCodeAt(0)); };
  async function pushSub() {
    if (!("serviceWorker" in navigator) || !window.PushManager) return null;
    const reg = await navigator.serviceWorker.getRegistration();
    return reg ? reg.pushManager.getSubscription() : null;
  }
  async function wirePush() {
    const st = document.getElementById("wp-state"), msg = document.getElementById("wp-msg");
    if (!st) return;
    const on = document.getElementById("wp-on"), off = document.getElementById("wp-off"), test = document.getElementById("wp-test");
    const show = async () => {
      on.hidden = off.hidden = test.hidden = true;
      if (!window.isSecureContext) { st.textContent = "Needs the app's https address (e.g. https://yourname.duckdns.org:port)."; return; }
      if (!("serviceWorker" in navigator) || !window.PushManager || !window.Notification) {
        st.textContent = /iPhone|iPad/.test(navigator.userAgent) && !isStandalone()
          ? "Add this page to your Home Screen (Share → Add to Home Screen) and open it from there: iPhone allows notifications only for Home Screen apps."
          : "This browser can't receive web notifications."; return;
      }
      let key = null; try { key = (await api("api/push/key")).key; } catch {}
      if (!key) { st.textContent = "The add-on can't send phone notifications yet (update the add-on)."; return; }
      const sub = await pushSub();
      if (Notification.permission === "denied") { st.textContent = "Notifications are blocked for this app (on iPhone: Settings → Notifications → Flighty)."; return; }
      if (sub) { st.textContent = "On. Alerts come straight to this phone as Flighty; tapping one opens the flight."; off.hidden = test.hidden = false; }
      else { st.textContent = "Off. Turn on to get alerts here directly, as Flighty, opening right on the flight."; on.hidden = false; }
    };
    on.onclick = async () => {
      msg.className = "small muted"; msg.textContent = "Turning on…";
      try {
        if ((await Notification.requestPermission()) !== "granted") throw new Error("permission not given");
        const { key } = await api("api/push/key");
        const reg = await navigator.serviceWorker.ready;
        const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u8(key) });
        const label = /iPhone/.test(navigator.userAgent) ? "iPhone" : /iPad/.test(navigator.userAgent) ? "iPad" : /Android/.test(navigator.userAgent) ? "Android" : "browser";
        await api("api/push/subscribe", { method: "POST", body: JSON.stringify({ sub: sub.toJSON(), label }) });
        msg.className = "small"; msg.textContent = "On. Send a test to check.";
      } catch (e) { msg.className = "small err"; msg.textContent = `Couldn't turn on: ${e.message}`; }
      show();
    };
    off.onclick = async () => {
      const sub = await pushSub();
      if (sub) { try { await api("api/push/unsubscribe", { method: "POST", body: JSON.stringify({ endpoint: sub.endpoint }) }); } catch {} await sub.unsubscribe().catch(() => {}); }
      msg.textContent = ""; show();
    };
    test.onclick = async () => {
      const sub = await pushSub();
      msg.className = "small muted"; msg.textContent = "Sending…";
      try { await api("api/push/test", { method: "POST", body: JSON.stringify({ endpoint: sub && sub.endpoint }) }); msg.className = "small"; msg.textContent = "Sent: it should appear in a few seconds."; }
      catch (e) { msg.className = "small err"; msg.textContent = `Not delivered (${e.message}). Turn it off and on again.`; }
    };
    show();
  }
  async function api(path, opts) {
    // GETs revalidate (the add-on answers "not changed" with a tiny 304); changes always go through.
    // A GET gives up after 15 s on a weak signal instead of hanging the page.
    const get = !(opts && opts.method && opts.method !== "GET");
    let ctl = null, to = null;
    if (get && !(opts && opts.signal) && window.AbortController) { ctl = new AbortController(); to = setTimeout(() => ctl.abort(), 15000); }
    let r;
    try { r = await fetch(path, { cache: get ? "no-cache" : "no-store", ...(ctl ? { signal: ctl.signal } : {}), ...opts }); }
    catch (e) {
      // can't reach the add-on: the Live dot goes gray now, not after the stream's 35 s silence check
      try { setLive(false); } catch {}
      throw new Error(e && e.name === "AbortError" ? "timed out (weak signal?)" : "offline or unreachable");
    }
    finally { if (to) clearTimeout(to); }
    if (!r.ok) throw new Error(`${r.status}`);
    if (r.headers.get("X-From-Phone")) { try { setLive(false); } catch {} }   // the phone's copy: no signal
    if (opts && opts.method === "POST") changed(path);
    return r.json();
  }
  // Stale-while-revalidate: draw the copy kept on the phone at once, then fetch and redraw in place
  // (only if it changed). On a weak signal the page is there immediately, and stays if the fetch fails.
  // ---------- the phone's copy of every page ----------
  // Each page's data is kept on the phone. A page draws from that copy at once, then fetches and
  // redraws in place only if something changed; with no signal it simply stays as it was.
  const CKEY = (p) => "ft-c:" + p;
  function cacheGet(p) { try { return JSON.parse(localStorage.getItem(CKEY(p)) || "null"); } catch { return null; } }
  function cachePut(p, d) {
    let txt;
    try { txt = JSON.stringify(d); } catch { return; }
    if (txt.length > 1500000) return;
    try { localStorage.setItem(CKEY(p), txt); }
    catch {
      // phone storage full: let go of the saved flights (the next visit re-saves the ones still open) and try once more
      try { Object.keys(localStorage).filter((k) => k.startsWith("ft-c:api/leg/")).forEach((k) => localStorage.removeItem(k)); localStorage.setItem(CKEY(p), txt); } catch {}
    }
  }
  // a flight's copy without the long lookup log (it can be big; the page fetches it fresh anyway)
  const slimLeg = (d) => d && d.history && d.history.length > 8 ? { ...d, history: d.history.slice(0, 8), history_more: (d.history_more || 0) + d.history.length - 8 } : d;
  // paths: one path, or several fetched together (draw gets them in order)
  async function swr(paths, draw) {
    const many = Array.isArray(paths), list = many ? paths : [paths], route0 = ROUTE;
    const c = list.map(cacheGet), have = c.every(Boolean);
    if (have && !morphing) draw(...c);
    let d;
    try { d = await Promise.all(list.map((p) => api(p))); }
    catch (e) { if (!have) throw e; return; }
    if (ROUTE !== route0) return;
    list.forEach((p, k) => cachePut(p, d[k]));
    if (have && !morphing && JSON.stringify(c) === JSON.stringify(d)) return;
    const was = morphing;
    morphing = morphing || have;
    try { draw(...d); } finally { morphing = was; }
  }
  // As soon as the app opens (and when it comes back, at most every 5 minutes): every page's data,
  // one request at a time behind whatever you're looking at, so each page opens instantly after.
  let lastPrefetch = 0, prefetching = false;
  async function prefetchAll(force) {
    if (prefetching || document.hidden || !STATE || (!force && Date.now() - lastPrefetch < 300000)) return;
    prefetching = true; lastPrefetch = Date.now();
    const low = lowData();
    const key = (l) => (l.flight && best(l.flight.times, "out")) || l.sched_out;
    const legs = (STATE.legs || []).filter((l) => !l.finished).sort((a, b) => key(a) - key(b)).slice(0, low ? 3 : 15);
    const jobs = [...legs.slice(0, 3).map((l) => `api/leg/${l.id}`), "api/history", "api/airports/status", "api/alerts",
      ...legs.slice(3).map((l) => `api/leg/${l.id}`), "api/settings", "api/backup", "api/rules", "api/debug"];
    try {
      for (const p of jobs) {
        if (document.hidden) break;
        try {
          const d = await api(p);
          if (p.startsWith("api/leg/")) { LEGS[d.id] = d; cachePut(p, slimLeg(d)); }
          else { cachePut(p, d); if (p === "api/airports/status") APTS = d; }
        } catch {}
      }
    } finally { prefetching = false; }
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
  // Which way the airplane points: its own reported heading, else the direction from the last track
  // point at least ~2 km back (FAA center radar sends no heading).
  function headingOf(live, track) {
    if (live && live.heading != null) return live.heading;
    // newest known position (the live fix or the track's last point, whichever is later), and the
    // direction from a point at least ~15 km before it in time, never from a point that came after
    // it. The track mixes sources (FlightAware's ADS-B, FAA center radar a minute behind): over a
    // couple of km their disagreement can point the airplane backwards; over 15 km it can't.
    const pts = (track || []).filter((p) => p && p[0] != null);
    let pos = live ? [live.lat, live.lon] : null, t = live ? live.at || 0 : -Infinity;
    const last = pts[pts.length - 1];
    if (last && (!pos || (last[4] || 0) > t)) { pos = last; t = last[4] || Infinity; }
    if (!pos) return 0;
    for (let i = pts.length - 1; i >= 0; i--) {
      const p = pts[i];
      if (p === pos || (p[4] && t !== Infinity && p[4] >= t)) continue;
      const dy = p[0] - pos[0], dx = (p[1] - pos[1]) * Math.cos(pos[0] * Math.PI / 180);
      if (Math.hypot(dx, dy) > 0.135) return bearing(p, pos);   // ~15 km
    }
    // a short track: the farthest point back, if it's at least ~2 km away
    const far = pts.filter((p) => p !== pos && !(p[4] && t !== Infinity && p[4] >= t))[0];
    if (far && Math.abs(far[0] - pos[0]) + Math.abs(far[1] - pos[1]) > 0.02) return bearing(far, pos);
    return 0;
  }
  // the newest of the live fix and the track's last point
  function newestPos(live, track) {
    const last = (track || []).filter((p) => p && p[0] != null).pop();
    if (live && !(last && (last[4] || 0) > (live.at || 0) + 5)) return [live.lat, live.lon];
    return last ? [last[0], last[1]] : live ? [live.lat, live.lon] : null;
  }
  // Where the airplane is now, carried forward from its last fix at its speed and heading (at most
  // 2 min): moves smoothly between radar reports that only come about once a minute en route.
  function aheadOf(live, hdg, now) {
    const age = now / 1000 - (live.at || 0);
    if (!(live.gs_kt > 60) || age < 3 || age > 120) return null;
    const R = Math.PI / 180, d = (live.gs_kt * age / 3600) / 3440.065, b = hdg * R, la = live.lat * R, lo = live.lon * R;
    const la2 = Math.asin(Math.sin(la) * Math.cos(d) + Math.cos(la) * Math.sin(d) * Math.cos(b));
    const lo2 = lo + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la), Math.cos(d) - Math.sin(la) * Math.sin(la2));
    return [la2 / R, lo2 / R];
  }
  // Rounds the corners between track points (two passes of corner cutting, ends kept), so the
  // drawn track reads as the curved path an airplane flies rather than straight jumps between fixes.
  function smooth(pts) {
    let a = pts;
    if (a.length < 3) return a;
    for (let k = 0; k < 2; k++) {
      const out = [a[0]];
      for (let i = 0; i < a.length - 1; i++) {
        const p = a[i], q = a[i + 1];
        out.push([0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]], [0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]]);
      }
      out.push(a[a.length - 1]);
      a = out;
    }
    return a;
  }
  const ll = (ap) => ap && ap.lat != null && ap.lon != null ? [ap.lat, ap.lon] : null;

  function kmBetween(a, b) {
    const R = Math.PI / 180, x = (b[1] - a[1]) * R * Math.cos((a[0] + b[0]) / 2 * R), y = (b[0] - a[0]) * R;
    return Math.hypot(x, y) * 6371;
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
  function timeBlock(label, t, sched, tz, right, src, extra) {
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
      ${extra || ""}
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

  // An inbound leg's page is your flight's page: the same layout, cards and map, for that leg
  // (its own inbound below it), plus the strip of the airplane's day and the FAA data log.
  async function aircraftView(id, n) {
    let leg;
    try { leg = await api(`api/leg/${id}?rotation=1`); } catch { go("#/"); return; }
    const rot = (leg.rotation || []).length ? leg.rotation : (leg.inbound ? [leg.inbound] : []);
    n = Math.max(0, Math.min(n, rot.length - 1));
    const h = rot[n];
    destroyMaps();
    if (!h) { $app.innerHTML = `<div class="sheet-gap"></div><section class="sheet">${sheetHead("No inbound linked yet", esc(flno(leg)), `#/leg/${id}`)}<div class="card empty"><b>No inbound linked yet</b>FlightAware hasn't linked the flight bringing your aircraft.</div></section>`; return; }
    const t = h.times || { out: {}, off: {}, on: {}, in: {} }, o = h.origin || {}, d = h.destination || {};
    const nxt = rot[n + 1] || null;
    // the turn into this leg: its inbound's gate arrival to this leg's departure
    const outB = best(t, "out"), inB = nxt ? best(nxt.times || {}, "in") : null;
    const vl = {
      id: `${id}~${n}`, ident: h.ident, display: h.display_ident || h.ident, airline: leg.airline,
      dep: o.iata || o.icao, arr: d.iata || d.icao, dep_ap: o, arr_ap: d, flight: h, phase: h.phase, status: h.status,
      sched_out: (t.out || {}).scheduled || (t.off || {}).scheduled, sched_in: (t.in || {}).scheduled || (t.on || {}).scheduled,
      rotation: rot.slice(n + 1), inbound: nxt, turn: outB && inB ? Math.round((outB - inB) / 60) : null,
      problems: [], // FAA programs that touch this leg: at its arrival, or departure-side ones at its departure
      nas: (leg.nas || []).filter((x) => x.airport === d.iata || (x.airport === o.iata && (x.kind === "closure" || x.dir === "dep"))).map((x) => ({ ...x, impact: [] })),
      wx: leg.wx || {}, fa_id: h.fa_id, source: "FlightAware", updated: h.fetched, watching: true,
      finished: ["arrived", "cancelled", "diverted"].includes(h.phase), history: [], alerts: [], chain: [],
      live: n === 0 ? leg.inbound_live : null, tail_count: h.tail_count,
      _ac: { parent: leg, id, n, rot },
    };
    drawLeg(vl.id, vl);
  }

  // the airplane's whole day as a strip, newest (your flight) on the left
  function rotStrip(ac) {
    const { parent, id, n, rot } = ac;
    return `<div class="rot-strip" style="margin:10px 0 12px">${[`<a class="rot-step mine" href="#/leg/${id}"><b>${esc(parent.display || "You")}</b><span>${esc(parent.dep)}→${esc(parent.arr)}</span></a>`]
      .concat(rot.map((r, i) => `<a class="rot-step ${i === n ? "on" : ""} ph-${slug(r.phase)}" href="#/aircraft/${id}/${i}"><b>${esc(r.display_ident || r.ident || "?")}</b><span>${esc((r.origin || {}).iata || "?")}→${esc((r.destination || {}).iata || "?")}</span><i>${esc(r.status)}</i></a>`))
      .join('<span class="rot-arrow">‹</span>')}</div>`;
  }
  // every lookup and what it changed; on an inbound leg's page, the lookups of that airplane
  function lookupLog(hist, more) {
    if (!hist.length) return "";
    return fold("leg-log", "Lookup Log", `<div class="card">${hist.map((h) => `<div class="hist">
          <div class="hist-top"><span class="mono small">${fmt(h.at, undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}${h.quiet ? `–${fmt(h.last, undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}` : ""}</span>
            ${h.sources.map((x) => `<span class="src ${/AeroAPI/.test(x) ? "aero" : /Gemini/.test(x) ? "gem" : /ADS-B|adsb/.test(x) ? "adsb" : ""}">${esc(x)}</span>`).join("")}</div>
          ${(h.seen || []).length ? `<div class="hist-seen">${h.seen.map((x) => `<div><span class="faint">${esc(x.src)}</span> ${x.tail ? `<b>${esc(x.tail)}</b>${x.note ? ` <span class="faint">· ${esc(x.note)}</span>` : ""}` : `<span class="faint">${esc(x.note || "no tail")}</span>`}</div>`).join("")}</div>` : ""}
          ${h.changes.length ? `<div class="hist-ch">${h.changes.map((c) => `<div><span class="faint">${esc(c.field)}</span> ${c.from ? `<s>${esc(c.from)}</s> → ` : ""}<b>${esc(c.to)}</b>${c.via ? ` <span class="faint small">via ${esc(c.via)}</span>` : ""}</div>`).join("")}</div>`
            : `<div class="small faint">No changes${h.quiet ? ` · ${h.quiet} checks` : ""}</div>`}
          ${h.error ? `<div class="small err">${esc(h.error)}</div>` : h.note && !h.changes.length ? `<div class="small faint">${esc(h.note)}</div>` : ""}
        </div>`).join("")}${more ? `<button class="btn" id="log-more" style="margin-top:10px">Show ${more} older</button>` : ""}</div>`, false);
  }
  // an inbound leg's Lookup Log, in exactly your flight's format: what the FAA, towers and the
  // airline said about it (one entry each, as on your flight), plus, for your inbound, its lookups
  // from your flight's log (its FlightAware page, its transponder) and what they changed
  function inboundHist(ac, f) {
    const swimH = (f.log || []).map((x) => ({ at: x.at, sources: [x.src || "FAA SWIM"], changes: [], seen: [{ src: x.src || "FAA SWIM", tail: null, note: x.text }] }));
    if (ac.n !== 0) return swimH;
    const inb = (x) => /\(inbound\)/.test(x), field = (c) => /^Inbound|^Tail$/.test(c.field);
    const look = (ac.parent.history || []).map((h) => {
      const seen = (h.seen || []).filter((x) => / · inbound /.test(x.src || ""));
      const changes = (h.changes || []).filter(field);
      if (!seen.length && !changes.length && !h.sources.some(inb)) return null;
      return { ...h, sources: h.sources.filter((x) => inb(x) || (seen.length && /adsb|ADS-B|radar|Transponder/i.test(x))), seen, changes, error: null, note: null };
    }).filter(Boolean);
    return swimH.concat(look).sort((a, b) => b.at - a.at);
  }
  function problemBanners(leg) {
    const nf = notFound(leg) && leg.note && /^Not found/.test(leg.note) ? `<div class="banner warn"><span class="dot warn"></span><div>${esc(leg.note)}</div></div>` : "";
    return nf + (leg.problems || []).filter((p) => !p.nas && !p.delay).map((p) => `<div class="banner ${p.level}"><span class="dot ${p.level}"></span><div>${esc(p.text)}</div></div>`).join("");
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
    // Collapsed: one line plus whether it catches your flight. Tap for the scope, the map and the advisory.
    const fid = `nas-${n.key}`;
    return `<details class="nas-alert k-${esc(n.kind)} ${holds ? "" : "clear"}" data-fold="${esc(fid)}" ${isOpen(fid, false) ? "open" : ""}>
      <summary><div class="na-top"><span class="na-apt">${esc(n.airport)}</span><span class="na-kind">${esc(n.kind === "gdp" && /^ground delay program$/i.test(n.label || "") ? "GDP" : n.label)}</span><span class="na-when">${esc(when)}</span><i class="na-chev"></i></div>
      ${lines[0] || ""}</summary>
      <div class="na-body">
      ${sub ? `<div class="na-sub">${esc(sub)}</div>` : ""}
      ${scope}${lines.slice(1).join("")}
      <div class="na-links">${tappable ? `<a class="na-adv" href="#/nas/${encodeURIComponent(n.key)}">Map ›</a>` : ""}${n.advisory ? `<a class="na-adv" href="${esc(n.advisory)}" target="_blank" rel="noopener">FAA advisory ↗</a>` : ""}</div>
      ${n.source ? `<div class="na-src faint small">from ${esc(n.source)}</div>` : ""}
      </div>
    </details>`;
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
  // The airport-status button on the map: red for a ground stop / closure that catches one of your
  // flights (or its inbound), amber for a delay program, white when nothing touches you.
  function towerLevel(legs) {
    let lvl = "";
    for (const g of nasGroups(legs)) {
      if (!MAJOR.includes(g.n.kind)) continue;
      if (!g.uses.some((u) => u.impact.some((i) => !i.airborne && i.hit !== false))) continue;
      if (g.n.kind === "gs" || g.n.kind === "closure") return "red";
      lvl = "amber";
    }
    return lvl;
  }
  function paintTower() {
    const b = GL.ctl && GL.ctl.querySelector('[data-a="apts"]');
    if (b) b.dataset.lvl = GL.tower || "";
  }
  const nasMinor = (legs) => nasGroups(legs).filter((g) => !MAJOR.includes(g.n.kind) && !TREND.includes(g.n.kind)).map((g) => g.n);

  function alertsCard(list) {
    if (!list.length) return "";
    return `<div class="card">${list.slice(0, 15).map((a) => `<div class="alert">
      <span class="dot ${a.level}"></span>
      <div><div class="ttl">${esc(a.title)}</div><div class="msg">${esc(a.message)}</div></div>
      <div class="at">${hm(a.at)}<br>${fmt(a.at, undefined, { day: "numeric", month: "short" })}</div>
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
  // Program cards on any page remember whether you opened them.
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (d && d.classList && d.classList.contains("nas-alert") && d.dataset.fold)
      try { localStorage.setItem(openKey(d.dataset.fold), d.open ? "1" : "0"); } catch {}
  }, true);
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
    if (cur === "stats") return `<a class="back plain" href="#/history${y}">‹ Passport</a><h1 class="ptitle">Flight Stats</h1>`;
    if (cur === "find") return `<h1 class="ptitle">Search</h1>`;
    if (cur === "alerts") return `<div class="ptitle-row"><h1 class="ptitle">Alerts</h1><a class="round" href="#/notify" aria-label="Notification settings">${IC.gear}</a></div>`;
    return "";
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

  function historyView(year) {
    return swr(`api/history${year ? `?year=${year}` : ""}`, (d) => drawPassport(d, year));
  }
  function drawPassport(d, year) {
    let h = `<div class="sheet-gap pass"></div><section class="sheet">
      <div class="sheet-head"><h1>Passport</h1><div class="sh-btns">${avatar()}</div></div>
      <div class="ypills">${["", ...d.years].map((y) => `<a class="yp ${y === (year || "") ? "on" : ""}" href="#/history${y ? "/" + y : ""}">${y || "All-Time"}</a>`).join("")}</div>`;
    if (!d.flights.length) h += `<div class="card empty"><b>No flights yet</b>Your Calendar and Manual flights are saved here when they finish.</div>` + importCard(true);
    else h += passCards(d, year) + `<div id="pass-list">${passList(d)}</div><div style="height:18px"></div>` + importCard(false, { flighty: d.flighty_count });
    h += `</section>`;
    $app.innerHTML = h;
    wireImport();
    const pl = document.getElementById("pass-list");
    if (pl) pl.onclick = (e) => {
      const b = e.target.closest("[data-psort]"), all = e.target.closest("[data-pall]");
      if (b) {
        const k = b.dataset.psort;
        PASS_SORT = PASS_SORT.k === k ? { k, dir: -(PASS_SORT.dir || -1) } : { k, dir: -1 };
        setPref("ft-pass-sort", JSON.stringify(PASS_SORT));
        pl.innerHTML = passList(d);
      } else if (all) { PASS_ALL = true; pl.innerHTML = passList(d); }
    };
    showMap(`pass-${year || "all"}`, paintPass, globeData(d));
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
  function statsView(year) {
    return swr(`api/history${year ? `?year=${year}` : ""}`, (d) => drawStats(d, year));
  }
  function drawStats(d, year) {
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
    if (!FIND_CACHE || Date.now() - FIND_CACHE.at > 60000 || !morphing) {
      let d = null;
      try { d = await api("api/history"); } catch (e) { try { d = JSON.parse(localStorage.getItem("ft-c:api/history") || "null"); } catch {} if (!d) throw e; }
      FIND_CACHE = { at: Date.now(), d };
    }
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

  function alertsHistoryView() { return swr("api/alerts", drawAlertsPage); }
  function drawAlertsPage(d) {
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
          <div class="at">${hm(a.at)}</div></div>`;
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
    const t = f.times || {};
    const cont = !!f.continuation_of;  // its "schedule" is the original flight's: show actual times only
    const conv = (x) => x ? { scheduled: cont ? null : x.s, actual: x.a, estimated: null } : {};
    // where each actual time came from (saved since 4.6; older flights and Flighty imports don't say)
    const tsrc = {};
    for (const k of ["out", "off", "on", "in"]) if (t[k] && t[k].src) tsrc[k] = { actual: t[k].src };
    const P = { id: f.id, dep: f.dep, arr: f.diverted_to || f.arr, dep_ap: f.dep_ap || {}, arr_ap: f.arr_ap || {}, phase: f.cancelled ? "cancelled" : "arrived",
      finished: true, source: f.source === "flighty" ? "Flighty import" : "", sched_out: (t.out || {}).s, sched_in: (t.in || {}).s, display: f.display, ident: f.ident, airline: f.airline, nas: [], problems: [],
      flight: { times: { out: conv(t.out), off: conv(t.off), on: conv(t.on), in: conv(t.in) }, distance_nm: f.distance_nm, aircraft_name: f.aircraft, aircraft_type: f.type, tail: f.tail,
        origin: { gate: f.dep_gate, terminal: f.dep_terminal }, destination: { gate: f.arr_gate, terminal: f.arr_terminal }, time_src: tsrc } };
    if (!P.dep_ap.tz) P.dep_ap.tz = f.dep_tz;
    if (!P.arr_ap.tz) P.arr_ap.tz = f.arr_tz;
    const rs = f.route_stats || {};
    const am = f.arr_delay;
    const head = f.cancelled ? "Cancelled" : f.diverted_to ? `Diverted to ${f.diverted_to}` : am == null ? "Completed" : `Arrived ${lateLower(am)}`;
    const cls = f.cancelled || f.diverted_to ? "red" : am == null ? "" : tone(am);
    const sub = [f.diverted_to ? `Planned to ${f.arr}.` : "", f.continued_by ? `Reached ${f.final_ap || f.arr} at ${hm(f.final_arrival, f.arr_tz)}${am != null ? `, ${fdur(am * 60)} after the original schedule` : ""}.` : "",
      cont ? "Continuation of a diverted flight; its lateness is counted on that flight." : ""].filter(Boolean).join(" ");
    $app.innerHTML = `
      <div class="sheet-gap d"></div>
      <section class="sheet detail">
        ${sheetHead(`${esc(cityOf(P.dep_ap, f.dep))} to ${esc(cityOf(P.arr_ap, P.arr))}`,
          `${esc(flno(f))} • ${esc(fmt((t.out && (t.out.a || t.out.s)), f.dep_tz, { weekday: "short", day: "numeric", month: "short", year: "numeric" }).toUpperCase())}`, "#/history", alBadge(airlineCode(f), "lg"))}
        <div class="sbanner ${cls}"><div class="sb-h">${esc(head)}</div>${sub ? `<div class="sb-s">${esc(sub)}</div>` : ""}</div>
        ${f.continued_by ? `<p class="small" style="margin:10px 0 0"><a href="#/hist/${encodeURIComponent(f.continued_by)}" style="color:var(--accent)">${esc(f.continued_by_label || "Continuation")} ›</a></p>` : ""}
        ${cont ? `<p class="small" style="margin:10px 0 0"><a href="#/hist/${encodeURIComponent(f.continuation_of)}" style="color:var(--accent)">${esc(f.continuation_of_label || "Original flight")} ›</a></p>` : ""}
        ${endpoint(P, "dep", { past: true })}
        ${midLine(P)}
        ${endpoint(P, "arr", { past: true })}
        ${timesCard(P)}
        ${aircraftF({ ...P, tail_count: f.tail_count })}
        <div class="gk-card"><div class="kv">
          <div class="k">Distance</div><div>${nm(f.distance_nm)} nm · ${nm((f.distance_nm || 0) * 1.15078)} mi</div>
          <div class="k">In the air</div><div>${f.air_min ? fdur(f.air_min * 60) : "—"}</div>
          <div class="k">This route</div><div>${rs.flights} flight${rs.flights === 1 ? "" : "s"}${rs.on_time_pct != null ? ` · ${rs.on_time_pct}% on time · ${avgArr(rs.avg_arr_delay)}` : ""}</div>
          ${f.route ? `<div class="k">Filed route</div><div class="mono small">${esc(f.route)}</div>` : ""}
          <div class="k">Source</div><div>${f.source === "flighty" ? "Flighty import" : "Tracked here"}${f.has_track ? " · flown track saved" : ""}</div>
        </div></div>
        <div class="actions">${!f.has_track && bfToken(f) ? `<button class="btn" id="h-bf">Copy for track backfill</button>` : ""}<button class="btn danger" id="h-del">Delete from History</button></div>
        ${!f.has_track && bfToken(f) ? `<p class="small faint" style="margin:8px 0 0">No flown track yet. Copy, then in GitHub run Backfill tracks with this pasted in the flights box, and import its tracks download here.</p>` : ""}
      </section>`;
    showMap(`hist-${f.id}`, paintLeg, { dep_ap: P.dep_ap, arr_ap: P.arr_ap, flight: { track: f.track || [], waypoints: [], times: {} }, phase: "arrived", inbound: null, chain: [], live: null, display: f.display },
      { kind: "leg" });
    const hb = document.getElementById("h-bf");
    if (hb) hb.onclick = async () => { hb.textContent = (await copyText(bfToken(f))) ? "Copied" : "Couldn't copy"; };
    document.getElementById("h-del").onclick = async () => {
      if (!confirm("Delete this flight from History?")) return;
      await api("api/history/delete", { method: "POST", body: JSON.stringify({ id: f.id }) });
      go("#/history");
    };
  }

  // ================================================================================================
  // Flighty-style UI: full-screen map with a sheet over it, flight rows, the flight page's sheet,
  // and the Passport globe.
  // ================================================================================================
  const IC = {
    dep: '<svg viewBox="0 0 24 24"><path d="M7 17 17 7M9 7h8v8" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    arr: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 9v8H9" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7.5V12l3 2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    bag: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M9 2.5h6a1 1 0 0 1 1 1V6h1.5A2.5 2.5 0 0 1 20 8.5v9a2.5 2.5 0 0 1-2.5 2.5H17v1.5h-2V20H9v1.5H7V20h-.5A2.5 2.5 0 0 1 4 17.5v-9A2.5 2.5 0 0 1 6.5 6H8V3.5a1 1 0 0 1 1-1zm1 3.5h4V4.5h-4z"/></svg>',
    x: '<svg viewBox="0 0 24 24"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
    plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>',
    refresh: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M17.65 6.35A7.96 7.96 0 0 0 12 4a8 8 0 1 0 7.73 10h-2.08A6 6 0 1 1 12 6c1.66 0 3.14.69 4.22 1.78L13 11h7V4z"/></svg>',
    takeoff: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M2.5 19h19v2h-19zm19.57-9.36c-.21-.8-1.04-1.28-1.84-1.06L14.92 10l-6.9-6.43-1.93.51 4.14 7.17-4.97 1.33-1.97-1.54-1.45.39 1.82 3.16.77 1.33 1.6-.43 5.31-1.42 4.35-1.16L21 11.49c.81-.23 1.28-1.05 1.07-1.85z"/></svg>',
    land: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M2.5 19h19v2h-19zm7.18-5.73 4.35 1.16 5.31 1.42c.8.21 1.62-.26 1.84-1.06.21-.8-.26-1.62-1.06-1.84l-5.31-1.42-2.76-9.02L10.12 2v8.28L5.15 8.95l-.93-2.32-1.45-.39v5.17l1.6.43 5.31 1.43z"/></svg>',
    check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    chev: '<svg viewBox="0 0 24 24"><path d="M9 5.5l6.5 6.5L9 18.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    map: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 6.2 8.6 4l6.8 2.4L21 4.2v13.6L15.4 20l-6.8-2.4L3 19.8zm6.2.1v9.9l5.6 2v-9.9z"/></svg>',
    cloud: '<svg viewBox="0 0 24 24"><path d="M7.2 18.5h9.6a4 4 0 0 0 .6-7.95 5.8 5.8 0 0 0-11.1 1.5A3.25 3.25 0 0 0 7.2 18.5z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>',
    locate: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M20.9 3.1 3.6 10.3c-.8.3-.7 1.5.1 1.7l6.7 1.6 1.6 6.7c.2.8 1.4.9 1.7.1l7.2-17.3z"/></svg>',
    gear: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M19.14 12.94a7.07 7.07 0 0 0 0-1.88l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.03 7.03 0 0 0-1.62-.94l-.36-2.54A.5.5 0 0 0 13.9 2h-3.84a.5.5 0 0 0-.49.42l-.36 2.54c-.59.24-1.13.56-1.62.94l-2.39-.96a.5.5 0 0 0-.61.22L2.67 8.48a.5.5 0 0 0 .12.64l2.03 1.58a7.07 7.07 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.3.61.22l2.39-.96c.49.38 1.03.7 1.62.94l.36 2.54c.05.24.25.42.49.42h3.84c.24 0 .45-.18.49-.42l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.48 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58zM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7z"/></svg>',
    speed: '<svg viewBox="0 0 24 24"><path d="M4.5 16a8 8 0 1 1 15 0" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><path d="M12 14l4-5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>',
    alt: '<svg viewBox="0 0 24 24"><path d="M5 4h14M8 8h8M10 12h4M5 20h14M12 12v8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    warn: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 3 1.8 20.5h20.4zm-1 6h2v6h-2zm0 7.5h2v2h-2z"/></svg>',
  };
  const WX_IC = {
    sun: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.2" fill="#ffc53d"/><g stroke="#ffc53d" stroke-width="2" stroke-linecap="round"><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></g></svg>',
    moon: '<svg viewBox="0 0 24 24"><path fill="#ffd36b" d="M20 14.6A8.5 8.5 0 0 1 9.4 4a8.5 8.5 0 1 0 10.6 10.6z"/></svg>',
    cloud: '<svg viewBox="0 0 24 24"><path fill="#cfd6e4" d="M7 19h10.5a4.5 4.5 0 0 0 .7-8.95A6.5 6.5 0 0 0 5.8 11.7 3.7 3.7 0 0 0 7 19z"/></svg>',
    part: '<svg viewBox="0 0 24 24"><circle cx="9" cy="9" r="3.6" fill="#ffc53d"/><path fill="#d9dfeb" d="M9.5 20h8.3a3.6 3.6 0 0 0 .5-7.15 5.2 5.2 0 0 0-9.9 1.4A3 3 0 0 0 9.5 20z"/></svg>',
    partn: '<svg viewBox="0 0 24 24"><path fill="#ffd36b" d="M13 8.2A5 5 0 0 1 6.6 2.4a5 5 0 1 0 6.4 5.8z"/><path fill="#d9dfeb" d="M9.5 20h8.3a3.6 3.6 0 0 0 .5-7.15 5.2 5.2 0 0 0-9.9 1.4A3 3 0 0 0 9.5 20z"/></svg>',
    rain: '<svg viewBox="0 0 24 24"><path fill="#cfd6e4" d="M7 15h10.5a4.2 4.2 0 0 0 .7-8.35A6 6 0 0 0 5.8 8 3.5 3.5 0 0 0 7 15z"/><path d="M8 18l-1 2.5M12 18l-1 2.5M16 18l-1 2.5" stroke="#5aa9ff" stroke-width="2" stroke-linecap="round"/></svg>',
    snow: '<svg viewBox="0 0 24 24"><path fill="#cfd6e4" d="M7 15h10.5a4.2 4.2 0 0 0 .7-8.35A6 6 0 0 0 5.8 8 3.5 3.5 0 0 0 7 15z"/><g fill="#fff"><circle cx="8" cy="19" r="1.2"/><circle cx="12" cy="20.5" r="1.2"/><circle cx="16" cy="19" r="1.2"/></g></svg>',
    storm: '<svg viewBox="0 0 24 24"><path fill="#aeb6c6" d="M7 14h10.5a4.2 4.2 0 0 0 .7-8.35A6 6 0 0 0 5.8 7 3.5 3.5 0 0 0 7 14z"/><path fill="#ffc53d" d="M12.5 13 9 18.5h3l-1 4.5 4.5-6.5h-3l1.5-3.5z"/></svg>',
    fog: '<svg viewBox="0 0 24 24"><path d="M4 9h16M3 13h18M5 17h14" stroke="#cfd6e4" stroke-width="2.2" stroke-linecap="round"/></svg>',
  };

  let CLOCK24 = false;
  try { CLOCK24 = localStorage.getItem("ft-24h") === "1"; } catch {}
  const pref = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } };
  const setPref = (k, v) => { try { localStorage.setItem(k, v); } catch {} };

  // "1h 16m", "43m", "3d 4h" (Flighty's way, no padding)
  function fdur(sec) {
    if (sec == null || !isFinite(sec)) return "";
    let m = Math.round(Math.abs(sec) / 60);
    const d = Math.floor(m / 1440); m -= d * 1440;
    const h = Math.floor(m / 60); m -= h * 60;
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  }
  const ALC = { UAL: "UA", AAL: "AA", DAL: "DL", SWA: "WN", JBU: "B6", ASA: "AS", NKS: "NK", FFT: "F9", SKW: "OO", RPA: "YX",
    ENY: "MQ", GJS: "G7", ASH: "YV", AWI: "ZW", PDT: "PT", EDV: "9E", JIA: "OH", ACA: "AC", WJA: "WS", BAW: "BA", DLH: "LH",
    AFR: "AF", KLM: "KL", UPS: "5X", FDX: "FX", AAY: "G4", SCX: "SY", HAL: "HA", VXP: "XP", MXY: "MX" };
  function airlineCode(x) {
    const a = (x.airline || "").toUpperCase();
    if (a.length === 2) return a;
    if (ALC[a]) return ALC[a];
    const d = String(x.display || x.ident || "").replace(/\s/g, "").match(/^([A-Z0-9]{2})(?=\d)/i);
    if (d) return d[1].toUpperCase();
    const i = String(x.ident || "").match(/^([A-Z]{3})\d/i);
    return i && ALC[i[1].toUpperCase()] ? ALC[i[1].toUpperCase()] : (a.slice(0, 2) || "✈");
  }
  const AL_COLOR = { UA: "#0b2a6f", AA: "#3b4d5f", DL: "#8a1a2c", WN: "#2a49a6", B6: "#0b2f6b", AS: "#0b3a5c", NK: "#8a7b00", F9: "#1d5c3a" };
  function alColor(code) {
    if (AL_COLOR[code]) return AL_COLOR[code];
    let h = 0; for (const c of String(code)) h = (h * 31 + c.charCodeAt(0)) % 360;
    return `hsl(${h} 42% 30%)`;
  }
  const alBadge = (code, cls) => `<span class="al-badge ${cls || ""}" style="background:${alColor(code)}">${esc(code)}</span>`;
  const flno = (x) => {
    const s = String(x.display || x.ident || "").trim();
    const m = s.match(/^([A-Z0-9]{2,3}?)\s*(\d+[A-Z]?)$/i);
    return m ? `${m[1].toUpperCase()} ${m[2]}` : s;
  };
  function cityOf(ap, code) {
    ap = ap || {};
    if (ap.city) return String(ap.city).replace(/,\s*[A-Z]{2}$/, "");
    const n = String(ap.name || "").replace(/\b(International|Intl\.?|Regional|Municipal|Airport|Field|Int'l)\b/gi, "").replace(/\s+/g, " ").trim();
    return n || code || ap.iata || "";
  }
  const apShort = (ap, code) => {
    const n = (ap && ap.name) || "";
    return n ? n.replace(/International/i, "Intl.").replace(/\s*Airport$/i, "").trim() : cityOf(ap, code);
  };
  function dayNum(ts, tz) {
    let s;
    try { s = new Intl.DateTimeFormat("en-CA", { timeZone: tz || undefined, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(ts * 1000)); }
    catch { s = new Date(ts * 1000).toISOString().slice(0, 10); }
    const [y, m, d] = s.split("-").map(Number);
    return Math.round(Date.UTC(y, m - 1, d) / 86400000);
  }
  function tzOff(ts, tz) {
    try {
      const v = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" }).formatToParts(new Date(ts * 1000))
        .find((x) => x.type === "timeZoneName").value;
      const m = v.match(/GMT([+-])(\d+)(?::(\d+))?/);
      return m ? (m[1] === "-" ? -1 : 1) * (parseInt(m[2], 10) * 60 + parseInt(m[3] || "0", 10)) : 0;
    } catch { return null; }
  }
  // The flight's times, best known: actual, else estimated, else scheduled.
  function tms(leg) {
    const f = leg.flight || null, t = (f && f.times) || {};
    const g = (k) => t[k] || {};
    const v = (k) => g(k).actual || g(k).estimated || g(k).scheduled || null;
    return {
      out: v("out") || leg.sched_out, outS: g("out").scheduled || leg.sched_out, outA: g("out").actual, outKnown: !!(g("out").actual || g("out").estimated),
      inn: v("in") || leg.sched_in, inS: g("in").scheduled || leg.sched_in, inA: g("in").actual, inKnown: !!(g("in").actual || g("in").estimated),
      off: v("off"), offA: g("off").actual, on: v("on"), onA: g("on").actual,
    };
  }
  const minsLate = (a, s) => a && s ? Math.round((a - s) / 60) : 0;
  const tone = (m, known = true) => !known ? "" : m >= 15 ? "red" : m >= 5 ? "amber" : "green";
  const lateWord = (m, known = true) => !known ? "Scheduled" : Math.abs(m) < 5 ? "On Time" : m > 0 ? `${fdur(m * 60)} Late` : `${fdur(-m * 60)} Early`;
  const lateLower = (m) => Math.abs(m) < 5 ? "on time" : m > 0 ? `${fdur(m * 60)} late` : `${fdur(-m * 60)} early`;
  const lateTone = (t) => t === "red" || t === "amber" ? t : "";

  // ---------- the flight list ----------
  function whenCol(leg) {
    const T = tms(leg), now = Date.now() / 1000, ph = leg.phase;
    const ic = (svg, lbl, cls) => `<div class="fw ${cls || ""}"><span class="fw-ic">${svg}</span><span class="fw-l">${lbl}</span></div>`;
    if (ph === "airborne") return ic(IC.land, "In Air", "air");
    if (ph === "taxi_out") return ic(IC.takeoff, "Taxiing", "air");
    if (ph === "landed") return ic(IC.land, "Landed", "air");
    if (ph === "cancelled") return ic(IC.x, "Cancelled", "bad");
    if (ph === "diverted") return ic(IC.warn, "Diverted", "bad");
    if (leg.finished || ph === "arrived") return ic(IC.check, "Arrived", "done");
    const s = T.out - now;
    const num = (n, unit) => `<div class="fw"><b>${n}</b><span class="fw-l">${unit}${n === 1 ? "" : "s"}</span></div>`;
    if (s <= 0) return ic(IC.takeoff, "Boarding");
    if (s < 3600) return num(Math.max(1, Math.round(s / 60)), "Min");
    if (s < 18 * 3600) return num(Math.round(s / 3600), "Hour");
    return num(Math.max(1, dayNum(T.out, leg.dep_ap.tz) - dayNum(now, leg.dep_ap.tz)), "Day");
  }
  function rowStatus(leg) {
    const T = tms(leg), now = Date.now() / 1000, ph = leg.phase;
    const am = minsLate(T.inn, T.inS), dm = minsLate(T.out, T.outS);
    if (ph === "airborne") { const at = T.on || T.inn; return [at > now ? `Landing in ${fdur(at - now)}` : "Landing now", lateTone(tone(am, T.inKnown))]; }
    if (ph === "taxi_out") return ["Taxiing out", lateTone(tone(dm))];
    if (ph === "landed") return [T.inn > now ? `At gate in ${fdur(T.inn - now)}` : "Landed", lateTone(tone(am))];
    if (ph === "arrived") return [`Arrived ${lateLower(am)}`, tone(am)];
    if (ph === "cancelled") return ["Cancelled", "red"];
    if (ph === "diverted") return [`Diverted to ${leg.arr}`, "red"];
    if (notFound(leg)) return [leg.status || "Not found", "amber"];
    if (T.out > now && T.out - now < 12 * 3600) return [`Departs in ${fdur(T.out - now)}`, dm >= 5 ? tone(dm) : ""];
    return [fmt(T.out, leg.dep_ap.tz, { weekday: "short", month: "short", day: "numeric" }), dm >= 5 ? tone(dm) : "date"];
  }
  function row(leg) {
    const T = tms(leg), tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz, known = !notFound(leg);
    const [st, stc] = rowStatus(leg);
    const dt = lateTone(tone(minsLate(T.out, T.outS), T.outKnown)), at = lateTone(tone(minsLate(T.inn, T.inS), T.inKnown));
    const tags = (leg.dh ? '<span class="tag">DH</span>' : "") + kindTag(leg) + ((leg.source || "").includes("Gemini") ? '<span class="tag gem">GEMINI</span>' : "");
    const warn = (leg.problems || []).filter((p) => !p.nas && !p.delay)[0];
    return `<a class="frow ${leg.finished ? "done" : ""}" href="#/leg/${leg.id}">
      ${whenCol(leg)}
      <div class="fr-main">
        <div class="fr-top">${alBadge(airlineCode(leg))}<span class="fr-no">${esc(flno(leg))}</span>${tags}<span class="fr-st ${stc}">${esc(st)}</span></div>
        <div class="fr-cities">${esc(cityOf(leg.dep_ap, leg.dep))} to ${esc(cityOf(leg.arr_ap, leg.arr))}</div>
        <div class="fr-times">
          <span class="ft"><i class="ti ${dt}">${IC.dep}</i>${esc(leg.dep || "···")} <b class="${dt}">${known ? hm(T.out, tzA) : "—"}</b></span>
          <span class="ft"><i class="ti ${at}">${IC.arr}</i>${esc(leg.arr || "···")} <b class="${at}">${known ? hm(T.inn, tzB) : "—"}</b></span>
        </div>
        ${warn && !leg.finished ? `<div class="fr-warn ${esc(warn.level)}">${esc(warn.text)}</div>` : ""}
      </div></a>`;
  }
  // "1h 12m at FLL" between two legs that connect
  function layover(a, b) {
    if (!a || !b || !a.arr || a.arr !== b.dep || a.extra || b.extra) return "";
    const gap = tms(b).out - tms(a).inn;
    if (!(gap > 0 && gap < 14 * 3600)) return "";
    const tight = gap < ((STATE.settings || {}).min_turn_minutes || 30) * 60;
    return `<div class="layover ${tight ? "tight" : ""}"><b>${fdur(gap)}</b> at ${esc(b.dep)}</div>`;
  }
  function avatar() {
    const m = String(STATE.notify_target || "").match(/mobile_app_([a-z])/i);
    return `<a class="round avatar" href="#/tools" aria-label="Settings">${m ? esc(m[1].toUpperCase()) : IC.gear}</a>`;
  }

  // ---------- maps: satellite, radar, night ----------
  const ESRI_IMG = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
  const ESRI_LBL = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";
  // Esri satellite or the dark map (Mapbox and Google were removed in 4.23.0: their tiles can't be
  // kept on the phone)
  const mapStyle = () => { const v = pref("ft-mapstyle", "satellite"); return v === "dark" ? "dark" : "satellite"; };
  // Globe (default, like Flighty) or a flat map: lighter to draw, so smoother to drag
  const flatMap = () => pref("ft-proj", "globe") === "flat";
  // Map sharpness: how many pixels the map draws per screen point. Full = 2 (sharp on a Retina
  // screen), Balanced = 1.4 (half the pixels), Fast = 1 (a quarter): the same size on screen,
  // softer, but far less for the phone to redraw every frame.
  const MAPQ = { full: 2, balanced: 1.4, fast: 1 };
  const mapPR = () => Math.min(window.devicePixelRatio || 1, MAPQ[pref("ft-mapq", "full")] || 2);
  const lowData = () => pref("ft-lowdata", "0") === "1";
  const radarOn = () => pref("ft-radar", "0") === "1" && !lowData();
  let RADAR = null;
  // Where the sun is overhead (and the numbers the night-side outline needs).
  function subsolar(date) {
    const rad = Math.PI / 180, n = date.getTime() / 86400000 + 2440587.5 - 2451545.0;
    const Lm = (280.46 + 0.9856474 * n) % 360, g = ((357.528 + 0.9856003 * n) % 360) * rad;
    const lam = (Lm + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * rad, eps = (23.4393 - 0.0000004 * n) * rad;
    const alpha = Math.atan2(Math.cos(eps) * Math.sin(lam), Math.cos(lam)) / rad;
    const delta = Math.asin(Math.sin(eps) * Math.sin(lam)) / rad;
    const gmst = ((18.697374558 + 24.06570982441908 * n) % 24 + 24) % 24;
    const lon = ((alpha - gmst * 15) % 360 + 540) % 360 - 180;
    return { lat: delta, lon, alpha, delta, gmst };
  }
  // The sheet has three stops: peeking (pulled down, a big map), middle (where pages open) and
  // full (scrolled up over the map). midTop: where the sheet's top sits at the middle stop.
  const MID_F = (g) => g.classList.contains("pass") ? 0.58 : g.classList.contains("d") ? 0.52 : 0.42;
  const midTop = () => { const g = document.querySelector(".sheet-gap"); return g ? window.innerHeight * MID_F(g) : window.innerHeight * 0.45; };
  const midY = () => { const g = document.querySelector(".sheet-gap"); return g ? Math.max(0, Math.round(g.offsetHeight - midTop())) : 0; };
  // ---------- the flight page ----------
  function telem(lv) {
    if (!lv || lv.gs_kt == null) return "";
    return `<span class="te">${IC.speed}<b>${Math.round(lv.gs_kt * 1.15078).toLocaleString()}</b><small>MPH</small></span>
      <span class="te">${IC.alt}<b>${lv.alt_ft != null ? Math.round(lv.alt_ft).toLocaleString() : "—"}</b><small>FT</small></span>`;
  }
  function sheetHead(title, sub, back, badge) {
    return `<div class="d-head">${badge || ""}<div class="d-title">${sub ? `<div class="d-sub">${sub}</div>` : ""}<div class="d-cities">${title}</div></div>
      <a class="back xbtn" href="${back}" aria-label="Close">${IC.x}</a></div>`;
  }
  // The colored line under the header: what's happening now, in Flighty's words.
  function statusBanner(leg) {
    const T = tms(leg), now = Date.now() / 1000, ph = leg.phase;
    const am = minsLate(T.inn, T.inS), dm = minsLate(T.out, T.outS);
    let head = "", sub = "", cls = "";
    if (ph === "airborne") {
      const at = T.on || T.inn;
      head = at > now ? `Landing in ${fdur(at - now)}` : "Landing now";
      sub = `Arrives at gate ${lateLower(am)}.`; cls = tone(am, T.inKnown);
    } else if (ph === "taxi_out") {
      head = "Taxiing to the runway";
      sub = `Pushed back ${T.outA ? fdur(now - T.outA) + " ago" : ""}${T.off ? ` · takeoff ~${hm(T.off, leg.dep_ap.tz)}` : ""}. Arrives ${lateLower(am)}.`;
      cls = tone(am, T.inKnown);
    } else if (ph === "landed") {
      head = T.inn > now ? `At the gate in ${fdur(T.inn - now)}` : "Landed";
      sub = `Taxiing to the gate · arrives ${lateLower(am)}.`; cls = tone(am);
    } else if (ph === "arrived") {
      head = `Arrived ${lateLower(am)}`;
      sub = T.inA ? `At the gate ${fdur(now - T.inA)} ago.` : ""; cls = tone(am);
    } else if (ph === "cancelled") { head = "Cancelled"; cls = "red"; }
    else if (ph === "diverted") { head = `Diverted to ${leg.arr}`; sub = leg.planned_arr ? `Planned to ${leg.planned_arr}.` : ""; cls = "red"; }
    else if (notFound(leg)) { head = leg.status || "Not found"; sub = leg.note || ""; cls = "amber"; }
    else {
      head = T.out > now ? `Departs in ${fdur(T.out - now)}` : "Departing";
      sub = !T.outKnown ? "Scheduled." : dm >= 5 ? `Departs ${fdur(dm * 60)} late${am >= 5 ? `, arrives ${lateLower(am)}` : ""}.` : `On time${am >= 5 ? `, arrives ${lateLower(am)}` : ""}.`;
      cls = T.outKnown ? tone(Math.max(dm, am)) : "";
    }
    // the FAA's delay trend at an airport you're headed into
    const trend = (leg.nas || []).find((n) => n.kind === "delay" && (n.airport === leg.arr || (ph === "scheduled" && n.airport === leg.dep)));
    if (trend) sub += ` ${trend.airport}: ${trend.label}${trend.detail ? ` (${trend.detail})` : ""}.`;
    return `<div class="sbanner ${cls}"><div class="sb-h">${esc(head)}</div>${sub.trim() ? `<div class="sb-s">${esc(sub.trim())}</div>` : ""}</div>`;
  }
  // One end of the flight: airport, big time, how late, gate and bags.
  function endpoint(leg, side, opts) {
    opts = opts || {};
    const f = leg.flight || { times: {} }, dep = side === "dep", now = Date.now() / 1000;
    const ap = (dep ? leg.dep_ap : leg.arr_ap) || {}, code = dep ? leg.dep : leg.arr;
    const fa_ = (dep ? f.origin : f.destination) || {};
    const t = (f.times || {})[dep ? "out" : "in"] || {};
    const sched = t.scheduled || (dep ? leg.sched_out : leg.sched_in);
    const shown = t.actual || t.estimated || sched;
    const known = !!(t.actual || t.estimated);
    const m = minsLate(shown, sched), tn = tone(m, known);
    const nf = notFound(leg);
    let rel = "";
    if (opts.past) rel = shown ? fmt(shown, ap.tz, { weekday: "short", month: "short", day: "numeric" }) : "";
    else if (shown) rel = shown > now ? `${dep ? "Departs" : "Arrives"} in ${fdur(shown - now)}` : `${fdur(now - shown)} ago`;
    const tzs = shown ? tzAbbr(shown, ap.tz) : "";
    const gate = fa_.gate, term = fa_.terminal;
    return `<div class="ep">
      <div class="ep-ap"><i class="ep-ic">${dep ? IC.dep : IC.arr}</i><b>${esc(code || "")}</b><span>•</span>${esc(apShort(ap, code))}</div>
      <div class="ep-row">
        <div class="ep-time">
          <div class="ep-tl"><span class="ep-big ${lateTone(tn)}">${nf ? "—" : hm(shown, ap.tz)}</span>${!nf && sched && Math.abs(m) >= 1 ? `<s>${hm(sched, ap.tz)}</s>` : ""}</div>
          <div class="ep-sub">${nf ? "Not found" : `<span class="${tn}">${lateWord(m, known)}</span>${rel ? ` • ${esc(rel)}` : ""}${tzs ? ` <span class="ep-tz">${esc(tzs)}</span>` : ""}`}</div>
          ${dep && !opts.past ? edctLine(leg) : ""}
        </div>
        <div class="ep-gates">
          <div class="ep-badges">${!dep && fa_.baggage ? `<span class="gb">${IC.bag}${esc(fa_.baggage)}</span>` : ""}<span class="gb ${gate ? "" : "none"}">${dep ? IC.dep : IC.arr}${esc(gate || "—")}</span></div>
          <div class="ep-term">${term ? `Terminal <b>${esc(term)}</b>` : gate ? "" : "Gate not set"}</div>
        </div>
      </div></div>`;
  }
  // The EDCT is a takeoff time at the departure airport, so it sits with the departure.
  function edctLine(leg) {
    const f = leg.flight || {}, e = (f.swim || {}).edct, off = (f.times || {}).off;
    if (!e || !e.at || (off && off.actual)) return "";
    const tz = leg.dep_ap.tz;
    return `<div class="edct-line">EDCT <b>${hm(e.at, tz)}</b>${e.for ? ` · ${esc(e.for)}` : ""}</div>`;
  }
  function midLine(leg) {
    const T = tms(leg), f = leg.flight || {}, now = Date.now() / 1000;
    const A = ll(leg.dep_ap), B = ll(leg.arr_ap);
    const nm_ = f.distance_nm || (A && B ? kmBetween(A, B) / 1.852 : null);
    const block = T.inn && T.out ? T.inn - T.out : null;
    let pct = null;
    if (leg.phase === "airborne" && T.offA && (T.on || T.inn)) pct = Math.max(0, Math.min(1, (now - T.offA) / ((T.on || T.inn) - T.offA)));
    else if (["landed", "arrived"].includes(leg.phase)) pct = 1;
    return `<div class="ep-mid"><span>${IC.clock}${[block ? fdur(block) : "", nm_ ? `${Math.round(nm_ * 1.15078).toLocaleString()} mi` : ""].filter(Boolean).join(" • ")}</span>
      <i class="ep-line">${pct != null ? `<b style="width:${pct * 100}%"></b><em style="left:${pct * 100}%">${PLANE}</em>` : ""}</i></div>`;
  }
  // METAR, in words: 61°F and clear
  function wxSummary(w) {
    if (!w || !w.raw) return null;
    const body = (" " + w.raw + " ").split(" RMK ")[0];
    const tm = body.match(/\s(M?\d{2})\/(M?\d{2})?\s/);
    const c = tm ? parseInt(tm[1].replace("M", "-"), 10) : null;
    const toks = body.trim().split(/\s+/).filter((t) => /^(-|\+|VC)?(MI|PR|BC|DR|BL|SH|TS|FZ)?(DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|SQ)*$/.test(t)
      && /(TS|DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|HZ|SQ)/.test(t));
    const has = (re) => toks.some((t) => re.test(t));
    let cond = "clear", kind = "clear";
    if (has(/TS/)) { cond = "thunderstorms"; kind = "storm"; }
    else if (has(/SN|SG|PL|IC/)) { cond = has(/^-/) ? "light snow" : "snow"; kind = "snow"; }
    else if (has(/RA|DZ/)) { cond = has(/DZ/) ? "drizzle" : has(/^-/) ? "light rain" : has(/^\+/) ? "heavy rain" : "rain"; kind = "rain"; }
    else if (has(/FG/)) { cond = "fog"; kind = "fog"; }
    else if (has(/BR|HZ|FU/)) { cond = has(/BR/) ? "mist" : "haze"; kind = "fog"; }
    else if (/\sOVC\d/.test(body)) { cond = "overcast"; kind = "cloud"; }
    else if (/\sBKN\d/.test(body)) { cond = "mostly cloudy"; kind = "cloud"; }
    else if (/\sSCT\d/.test(body)) { cond = "partly cloudy"; kind = "part"; }
    else if (/\sFEW\d/.test(body)) { cond = "few clouds"; kind = "part"; }
    return { f: c == null ? null : Math.round(c * 9 / 5 + 32), cond, kind };
  }
  function wxIcon(kind, tz) {
    let h = 12;
    try { h = parseInt(new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "numeric", hourCycle: "h23" }).format(new Date()), 10); } catch {}
    const night = h < 6 || h >= 19;
    if (kind === "clear") return night ? WX_IC.moon : WX_IC.sun;
    if (kind === "part") return night ? WX_IC.partn : WX_IC.part;
    return WX_IC[kind] || WX_IC.cloud;
  }
  function goodToKnow(leg) {
    const cards = [];
    const wxFor = (code, ap) => {
      const e = Object.entries(leg.wx || {}).find(([c]) => c === code || c === ap.icao || c === ap.iata || c.slice(-3) === code);
      return e ? e[1] : null;
    };
    for (const [code, ap] of [[leg.dep, leg.dep_ap], [leg.arr, leg.arr_ap]]) {
      const w = wxFor(code, ap || {}), s = wxSummary(w);
      if (!s) continue;
      cards.push(`<div class="gk"><span class="gk-ic">${wxIcon(s.kind, (ap || {}).tz)}</span><div class="gk-t"><b>Current ${esc(code)} Weather</b>
        <span>${s.f != null ? `${s.f}°F and ` : ""}${esc(s.cond)}</span></div>${w.category ? `<span class="cat ${esc(w.category)}">${esc(w.category)}</span>` : ""}</div>`);
    }
    // FAA status at each airport
    const nasBy = {};
    for (const n of leg.nas || []) (nasBy[n.airport] = nasBy[n.airport] || []).push(n);
    for (const code of [leg.dep, leg.arr]) {
      const items = nasBy[code];
      if (!items || !items.length) continue;
      const major = items.some((n) => MAJOR.includes(n.kind));
      cards.push(`<div class="gk gk-nas"><div class="gk-row"><b>${esc(code)} ${IC.chev}</b><span class="${major ? "red" : "amber"}">${major ? esc(items.find((n) => MAJOR.includes(n.kind)).label) : "Minor Issues"}</span></div>
        ${items.map((n) => `<div class="gk-item"><span class="gk-ic sm">${n.dir === "dep" ? IC.takeoff : IC.land}</span><div class="gk-t"><b>${esc(n.label)}</b><span>${esc([n.detail, n.reason].filter(Boolean).join(" · "))}</span></div></div>`).join("")}</div>`);
    }
    if (!Object.keys(nasBy).length && leg.watching) cards.push(`<div class="gk"><span class="gk-ic">${WX_IC.sun.replace(/#ffc53d/g, "#30d158")}</span><div class="gk-t"><b>No FAA delays</b><span>No ground stops, delay programs or closures at ${esc(leg.dep)} or ${esc(leg.arr)}.</span></div></div>`);
    // timezone change
    const T = tms(leg);
    if (T.inn && leg.dep_ap.tz && leg.arr_ap.tz) {
      const d = tzOff(T.inn, leg.arr_ap.tz), o = tzOff(T.inn, leg.dep_ap.tz);
      if (d != null && o != null && d !== o) {
        const h = (d - o) / 60, hs = `${h > 0 ? "+" : "−"}${Math.abs(h)} Hour${Math.abs(h) === 1 ? "" : "s"}`;
        cards.push(`<div class="gk"><span class="gk-ic tzc">${esc(hs.split(" ")[0])}</span><div class="gk-t"><b>${hs} Timezone Change</b>
          <span>${hm(T.inn, leg.arr_ap.tz)} arrival is ${hm(T.inn, leg.dep_ap.tz)} ${esc(cityOf(leg.dep_ap, leg.dep))} time</span></div></div>`);
      }
    }
    const rs = leg.route_stats;
    if (rs && rs.flights) cards.push(`<div class="gk"><span class="gk-ic tzc">${rs.flights}×</span><div class="gk-t"><b>You've flown ${esc(leg.dep)}–${esc(leg.arr)} ${rs.flights} time${rs.flights === 1 ? "" : "s"}</b>
      <span>${rs.on_time_pct != null ? `${rs.on_time_pct}% on time · ${esc(avgArr(rs.avg_arr_delay))}` : "No delay record yet"}</span></div></div>`);
    return cards.length ? `<h2 class="gk-h">Good to Know</h2>${cards.join("")}` : "";
  }
  // The airplane bringing you in, and the legs before it, as a timeline.
  function inboundF(leg) {
    const rot = (leg.rotation || []).length ? leg.rotation : leg.inbound ? [leg.inbound] : [];
    if (!rot.length) return "";
    const i0 = rot[0], it = i0.times || {}, tz = leg.dep_ap.tz;
    const ph = i0.phase || (it.in && it.in.actual ? "arrived" : "scheduled");
    const inE = best(it, "in"), inS = (it.in || {}).scheduled, am = minsLate(inE, inS);
    const from = cityOf(i0.origin, (i0.origin || {}).iata);
    let pill, ptn, line;
    if (ph === "arrived") { pill = `Arrived ${lateLower(am)}`; ptn = tone(am); line = `The inbound aircraft arrived ${lateLower(am)}.`; }
    else if (["airborne", "landed", "taxi_out"].includes(ph)) {
      pill = ph === "taxi_out" ? "Taxiing" : ph === "landed" ? "Landed" : `En route · ${lateLower(am)}`; ptn = tone(am);
      line = `Your aircraft is ${ph === "landed" ? "on the ground here" : "flying in from " + esc(from)}${inE ? `, at the gate ~${hm(inE, tz)}` : ""}.`;
      if (ph === "taxi_out") {
        // pushed back but not off the ground yet: say since when (a long taxi is a delay in itself)
        const outA = (it.out || {}).actual, oz = (i0.origin || {}).tz, since = outA ? Math.round((Date.now() / 1000 - outA) / 60) : null;
        if (since != null && since >= 1) pill = `Taxiing · ${fdur(since * 60)}`;
        line = `Your aircraft ${outA ? `pushed back at ${esc((i0.origin || {}).iata || from)} at ${hm(outA, oz)}` : `is taxiing at ${esc(from)}`} and hasn't taken off yet${inE ? `. At the gate here ~${hm(inE, tz)}` : ""}.`;
      }
    } else if (ph === "cancelled" || ph === "diverted") { pill = ph === "cancelled" ? "Cancelled" : "Diverted"; ptn = "red"; line = `The inbound flight was ${ph}.`; }
    else { pill = "Not departed"; ptn = tone(minsLate(best(it, "out"), (it.out || {}).scheduled), !!((it.out || {}).estimated || (it.out || {}).actual)); line = `Your aircraft is due from ${esc(from)}${inE ? ` at ${hm(inE, tz)}` : ""}.`;
      if (i0.edct && i0.edct.at) { const oz = (i0.origin || {}).tz; pill = `EDCT ${hm(i0.edct.at, oz)}`; ptn = "amber"; line += ` It has an EDCT out of ${esc((i0.origin || {}).iata || from)}: ${hm(i0.edct.at, oz)} ${esc(tzAbbr(i0.edct.at, oz))}${i0.edct.for ? ` (${esc(i0.edct.for)} program)` : ""}.`; } }
    const T = tms(leg), dm = minsLate(T.out, T.outS);
    const me = leg.phase === "scheduled" ? `Departs ${lateLower(dm)}` : `Departed ${lateLower(dm)}`;
    const turn = leg.turn, minTurn = (STATE.settings || {}).min_turn_minutes || 30;
    const tcls = turn == null ? "" : turn < 0 ? "bad" : turn < minTurn ? "tight" : "";
    const landed = !!((it.in || {}).actual);
    const tpct = landed || (turn != null && turn < 0) ? 100 : turn == null ? 0 : Math.max(4, Math.min(100, (turn / Math.max(minTurn * 2, 60)) * 100));
    const ac = leg._ac, legHref = ac ? `#/aircraft/${ac.id}/${ac.n}` : `#/leg/${leg.id}`;
    const acHref = (i) => ac ? `#/aircraft/${ac.id}/${ac.n + 1 + i}` : `#/aircraft/${leg.id}/${i}`;
    let tl = `<a class="tl-row me" href="${legHref}"><span class="tl-up">${IC.dep}</span><b>This Flight</b><span class="${lateTone(tone(dm, T.outKnown))}">${esc(me)}</span></a>`;
    rot.slice(0, 3).forEach((r, i) => {
      const o = r.origin || {}, d = r.destination || {}, rt = r.times || {};
      const rin = best(rt, "in"), rm = minsLate(rin, (rt.in || {}).scheduled), rk = !!((rt.in || {}).actual || (rt.in || {}).estimated);
      const sub = r.phase === "arrived" ? `Arrived ${hm(rin, d.tz)}` : ["airborne", "landed"].includes(r.phase) ? `Arrives ${hm(rin, d.tz)}`
        : r.phase === "taxi_out" ? `Taxiing${(rt.out || {}).actual ? ` since ${hm(rt.out.actual, o.tz)}` : ""} · arrives ${hm(rin, d.tz)}`
        : r.phase === "cancelled" ? "Cancelled" : `Departs ${hm(best(rt, "out"), o.tz)}${r.edct && r.edct.at ? ` · EDCT ${hm(r.edct.at, o.tz)}` : ""}`;
      tl += `<div class="tl-node"><i></i>${esc(d.iata || "")}</div>
        <a class="tl-row" href="${acHref(i)}"><div><b>${esc(cityOf(o, o.iata))} to ${esc(cityOf(d, d.iata))}</b><span>${esc(r.display_ident || "")} · ${esc(sub)}</span></div>
          <span class="${tone(rm, rk)}">${r.phase === "cancelled" ? "Cancelled" : lateWord(rm, rk)}</span></a>`;
    });
    const last = rot[Math.min(rot.length, 3) - 1];
    tl += `<div class="tl-node"><i></i>${esc((last.origin || {}).iata || "")}</div>`;
    if (rot.length > 3) tl += `<a class="tl-more" href="${acHref(3)}"><span>${rot.length - 3}</span>Show More Flights</a>`;
    return `<div class="gk-card">
      <div class="ib-head"><b>Inbound Aircraft</b><span>${esc([i0.tail, i0.aircraft_type].filter(Boolean).join(" · "))}</span></div>
      <span class="ib-pill ${ptn}">${esc(pill)}</span>
      <p class="ib-line">${line}</p>
      ${turn != null && leg.phase === "scheduled" ? `<div class="turnbar ${tcls}"><div class="bar"><span style="width:${tpct}%"></span></div><div class="num">${turn < 0 ? `lands ${fdur(-turn * 60)} after you're due out` : `${fdur(turn * 60)} turn`}</div></div>` : ""}
      <div class="tl">${tl}</div></div>`;
  }
  // Each of the four times, what kind it is (actual, estimate, schedule) and who it's from:
  // the airline, the FAA, a tower, surface radar, FlightAware...
  function timesCard(leg) {
    const f = leg.flight;
    if (!f || !f.times) return "";
    const t = f.times, src = f.time_src || {}, tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz;
    const sw = f.swim || {};
    const edct = sw.edct && sw.edct.at && !(t.off && t.off.actual)
      ? `<div class="ts-row"><span class="ts-l">EDCT</span><b class="ts-v amber">${hm(sw.edct.at, tzA)}</b><span class="ts-s">FAA${sw.edct.for ? ` · ${esc(sw.edct.for)} program` : ""}</span></div>` : "";
    const rows = [["Gate out", "out", tzA], ["Takeoff", "off", tzA], ["Landing", "on", tzB], ["Gate in", "in", tzB]].map(([lbl, k, tz]) => {
      const x = t[k] || {}, kind = x.actual ? "Actual" : x.estimated ? "Estimate" : x.scheduled ? "Scheduled" : "";
      const v = x.actual || x.estimated || x.scheduled;
      if (!v) return "";
      const who = kind === "Scheduled" ? "" : (src[k] || {})[kind === "Actual" ? "actual" : "estimated"] || (leg.finished && !leg.flight.time_src ? leg.source : leg.source || "FlightAware");
      const m = x.scheduled && kind !== "Scheduled" ? minsLate(v, x.scheduled) : 0;
      return `<div class="ts-row"><span class="ts-l">${lbl}</span><b class="ts-v ${lateTone(tone(m))}">${hm(v, tz)}</b>
        <span class="ts-s">${kind}${who ? ` · ${esc(who)}` : ""}</span></div>${k === "off" ? edct : ""}`;
    }).join("");
    return rows ? `<div class="gk-card ts"><div class="ts-h">Times</div>${rows}</div>` : "";
  }
  function timetable(leg) {
    const f = leg.flight;
    if (!f || !f.times) return "";
    const t = f.times, tzA = leg.dep_ap.tz, tzB = leg.arr_ap.tz;
    const g = (k) => t[k] || {};
    const trow = (lbl, s, v, k, cls) => `<div class="tt-row"><div class="tt-l">${lbl}</div><div class="tt-c"><span>Scheduled</span><b>${s}</b></div><div class="tt-c"><span>${k || "&nbsp;"}</span><b class="${cls || ""}">${v}</b></div></div>`;
    const tp = (lbl, k, tz) => {
      const x = g(k), v = x.actual || x.estimated;
      return trow(lbl, x.scheduled ? hm(x.scheduled, tz) : "—", v ? hm(v, tz) : "—", x.actual ? "Actual" : x.estimated ? "Estimated" : "",
        v && x.scheduled ? lateTone(tone(minsLate(v, x.scheduled))) : "");
    };
    const td = (lbl, a, b) => {
      const A = g(a), B = g(b);
      const S = A.scheduled && B.scheduled ? B.scheduled - A.scheduled : null;
      const av = A.actual || A.estimated, bv = B.actual || B.estimated;
      const V = av && bv ? bv - av : null;
      const kind = A.actual && B.actual ? "Actual" : V != null ? "Estimated" : "";
      return trow(lbl, S != null && S > 0 ? fdur(S) : "—", V != null && V > 0 ? fdur(V) : "—", kind, S && V ? lateTone(tone(Math.round((V - S) / 60))) : "");
    };
    const src = f.time_src || {};
    return `<div class="gk-card tt">
      <h3 class="tt-h">Detailed Timetable</h3><div class="tt-s">Scheduled, estimated and actual</div>
      ${src.in || src.out ? `<div class="tt-src">${IC.clock}Estimates from ${esc(src.in || src.out)}</div>` : ""}
      <div class="tt-sec">Depart</div>
      ${tp("Gate Departure", "out", tzA)}${td("Taxi", "out", "off")}${tp("Take Off", "off", tzA)}
      <div class="tt-sec">In the Air</div>
      ${td("Flight", "off", "on")}
      <div class="tt-sec">Arrive</div>
      ${tp("Land", "on", tzB)}${td("Taxi", "on", "in")}${tp("Gate Arrival", "in", tzB)}
    </div>`;
  }
  // A generic airliner, side on (no airline's livery), tail in the airline's badge color.
  const planeSide = (tail) => `<svg class="ac-art" viewBox="0 0 400 110" aria-hidden="true">
    <path d="M150 63 250 65 284 88c3 2 0 5-4 4l-84-21z" fill="#9aa3b3"/>
    <path d="M318 47 352 8c4-5 14-4 16 2l4 38z" fill="${tail}"/>
    <path d="M18 64c0-12 16-18 42-18l270-2c20 0 32 2 42 6l16 4c4 2 2 8-4 9L60 72c-26 0-42-2-42-8z" fill="#eef1f5"/>
    <path d="M340 55 392 40c4-1 5 2 2 4l-34 14z" fill="${tail}"/>
    <ellipse cx="205" cy="76" rx="27" ry="9" fill="#c4cad5"/><ellipse cx="180" cy="76" rx="4" ry="8" fill="#6b7383"/>
    <path d="M30 55 44 51l2 5-18 2z" fill="#2a3242"/>
    <g fill="#3a4354">${Array.from({ length: 26 }, (_, i) => `<rect x="${70 + i * 9.4}" y="51" width="4.2" height="5" rx="1.6"/>`).join("")}</g>
    <path d="M24 66c40 3 230 1 340-6" stroke="#cdd3dd" stroke-width="1"/></svg>`;
  // United's 737s, side on, in today's livery: Kyle's AI Works' pictures (downloaded and sorted by the
  // add-on, api/liveries), the exact tail when he painted it, else one of the same type; the pictures
  // shipped with the app until those arrive. Other types and airlines keep the drawing.
  let LIV = null;
  try { LIV = JSON.parse(localStorage.getItem("ft-liv") || "null"); } catch {}
  (async () => { try { const d = await api("api/liveries"); if (d && d.ready) { LIV = d; localStorage.setItem("ft-liv", JSON.stringify(d)); } } catch {} })();
  const acKey = (type, name) => {
    const t = (type || "").toUpperCase(), nm = (name || "").toUpperCase();
    return /^B3[9X]M$/.test(t) || /MAX ?(9|10)/.test(nm) ? "b39m" : t === "B38M" || /MAX ?8/.test(nm) ? "b38m"
      : t === "B739" || /737-9\d\d/.test(nm) ? "b739" : t === "B738" || /737-8\d\d/.test(nm) ? "b738"
      : t === "B737" || /737-7\d\d/.test(nm) ? "b737" : "";
  };
  const BUNDLED = { b738: 1, b38m: 1, b39m: 1 };   // (the shipped -900 is the old livery)
  function acSrc(k, tail) {
    if (!k) return "";
    const T = (tail || "").toUpperCase(), L = LIV || {};
    if (T && (L.tails || {})[T] === k) return `api/livery/${T}.png`;
    const of = (L.types || {})[k] || [];
    if (of.length) return `api/livery/${of[0]}.png`;
    if (k === "b737" && BUNDLED.b738) return "ac-b738.png";
    return BUNDLED[k] ? `ac-${k}.png` : "";
  }
  function acArt(leg) {
    const f = leg.flight || {};
    if (airlineCode(leg) !== "UA") return "";
    const u = acSrc(acKey(f.aircraft_type, f.aircraft_name), f.tail);
    return u ? `<img class="ac-photo" src="${u}" alt="" loading="lazy" decoding="async" onerror="this.remove()">` : "";
  }
  const TYPE_NAME = { B737: "Boeing 737-700", B738: "Boeing 737-800", B739: "Boeing 737-900", B38M: "Boeing 737 MAX 8", B39M: "Boeing 737 MAX 9",
    B3XM: "Boeing 737 MAX 10", B752: "Boeing 757-200", B753: "Boeing 757-300", B763: "Boeing 767-300", B764: "Boeing 767-400",
    B772: "Boeing 777-200", B77W: "Boeing 777-300ER", B788: "Boeing 787-8", B789: "Boeing 787-9", B78X: "Boeing 787-10",
    A319: "Airbus A319", A320: "Airbus A320", A20N: "Airbus A320neo", A21N: "Airbus A321neo", A321: "Airbus A321",
    E75L: "Embraer 175", E175: "Embraer 175", CRJ2: "CRJ-200", CRJ7: "CRJ-700", CRJ9: "CRJ-900" };
  // How old the airplane is, from the FAA registry (api/tail/N…): its airworthiness date (within days
  // of an airliner's first flight), or the year it was built when that's all there is. Asked once per
  // tail and type, kept on the phone; the card fills in when the answer arrives.
  const TAILI = new Map();
  const tailKey = (t, ty) => `ft-tail:${t}:${ty || ""}`;
  function tailInfo(t, ty) {
    const k = tailKey(t, ty);
    if (TAILI.has(k)) return TAILI.get(k);
    try { const c = JSON.parse(localStorage.getItem(k) || "null"); if (c && (c.found || Date.now() - c.at < 86400e3)) { TAILI.set(k, c); return c; } } catch {}
    return undefined;
  }
  function ageText(d) {
    if (!d || !d.found) return "";
    const now = new Date();
    if (d.aw) {
      const [y, m, dd] = d.aw.split("-").map(Number);
      let yrs = now.getFullYear() - y, mos = now.getMonth() + 1 - m;
      if (now.getDate() < dd) mos--;
      if (mos < 0) { yrs--; mos += 12; }
      const when = new Date(y, m - 1, dd).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
      const age = yrs < 0 ? "brand new" : `${yrs ? `${yrs} year${yrs === 1 ? "" : "s"} ` : ""}${mos} month${mos === 1 ? "" : "s"} old`;
      return `First flight · ${when} · ${age}`;
    }
    if (d.year) { const a = now.getFullYear() - d.year; return `Built ${d.year} · about ${a} year${a === 1 ? "" : "s"} old`; }
    return "";
  }
  function fillAges() {
    document.querySelectorAll(".ac-age[data-tail]:not([data-asked])").forEach(async (el) => {
      el.dataset.asked = "1";
      const t = el.dataset.tail, ty = el.dataset.type, k = tailKey(t, ty);
      try {
        const d = await api(`api/tail/${encodeURIComponent(t)}${ty ? `?type=${encodeURIComponent(ty)}` : ""}`);
        const c = { found: !!d.found, aw: d.aw, year: d.year, model: d.model, at: Date.now() };
        TAILI.set(k, c);
        try { localStorage.setItem(k, JSON.stringify(c)); } catch {}
        document.querySelectorAll(`.ac-age[data-tail="${CSS.escape(t)}"]`).forEach((x) => { const s = ageText(c); if (s) { x.textContent = s; x.hidden = false; } });
      } catch {}
    });
  }
  function ageLine(f) {
    const t = (f.tail || "").toUpperCase();
    if (!/^N[0-9A-Z]{1,5}$/.test(t)) return "";
    const ty = (f.aircraft_type || "").toUpperCase(), c = tailInfo(t, ty);
    if (c !== undefined) { const s = ageText(c); return s ? `<div class="ac-sub ac-age">${esc(s)}</div>` : ""; }
    setTimeout(fillAges, 0);
    return `<div class="ac-sub ac-age" data-tail="${esc(t)}" data-type="${esc(ty)}" hidden></div>`;
  }
  function aircraftF(leg) {
    const f = leg.flight || {}, name = f.aircraft_name || TYPE_NAME[(f.aircraft_type || "").toUpperCase()] || f.aircraft_type;
    if (!name && !f.tail) return "";
    const n = leg.tail_count;
    return `<div class="gk-card acard"><div class="ac-k">Aircraft</div><div class="ac-name">${esc(name || "Aircraft")}</div>
      <div class="ac-sub">${esc([f.tail, name !== f.aircraft_type && f.aircraft_type ? f.aircraft_type : "", f.tail_source ? `tail from ${f.tail_source}` : ""].filter(Boolean).join(" · "))}</div>
      ${acArt(leg) || planeSide(alColor(airlineCode(leg)))}
      ${ageLine(f)}
      ${f.tail && n != null ? `<div class="ac-sub ac-count">${n ? `You've flown <b>${esc(f.tail)}</b> ${n} time${n === 1 ? "" : "s"} before.` : `First time on <b>${esc(f.tail)}</b>.`}</div>` : ""}</div>`;
  }

  // ---------- Passport ----------
  function globeData(d) {
    const A = d.airports || {}, routes = new Map(), aps = new Map();
    for (const f of d.flights) {
      if (f.cancelled) continue;
      const a = A[f.dep], b = A[f.diverted_to || f.arr];
      if (!a || !b || a.lat == null || b.lat == null) continue;
      routes.set([f.dep, f.diverted_to || f.arr].sort().join("-"), [a, b]);
      aps.set(f.dep, { ...a, code: f.dep }); aps.set(f.diverted_to || f.arr, { ...b, code: f.diverted_to || f.arr });
    }
    return { routes: [...routes.values()], airports: [...aps.values()] };
  }
  let PASS_SORT = (() => { try { return JSON.parse(localStorage.getItem("ft-pass-sort")) || { k: "date", dir: -1 }; } catch { return { k: "date", dir: -1 }; } })();
  let PASS_ALL = false;
  function passList(d) {
    const A = d.airports || {};
    const city = (c) => cityOf(A[c] || {}, c);
    const keyF = { date: (f) => fdep(f), from: (f) => f.dep || "", to: (f) => f.diverted_to || f.arr || "",
      airline: (f) => airlineCode(f), aircraft: (f) => f.aircraft || f.type || "~" }[PASS_SORT.k] || fdep;
    const dir = PASS_SORT.dir || -1;
    const fl = [...d.flights].sort((a, b) => {
      const x = keyF(a), y = keyF(b);
      return (x < y ? -1 : x > y ? 1 : 0) * (PASS_SORT.k === "date" ? dir : -dir) || fdep(b) - fdep(a);
    });
    const groupOf = (f) => PASS_SORT.k === "date" ? (f.date || "").slice(0, 4) || "Undated" : String(keyF(f) === "~" ? "Unknown" : keyF(f));
    const counts = {};
    for (const f of fl) counts[groupOf(f)] = (counts[groupOf(f)] || 0) + 1;
    const chips = [["date", "Date"], ["from", "From"], ["to", "To"], ["airline", "Airline"], ["aircraft", "Aircraft"]]
      .map(([k, l]) => `<button class="psort ${PASS_SORT.k === k ? "on" : ""}" data-psort="${k}">${l}${PASS_SORT.k === k ? (dir < 0 ? " ↓" : " ↑") : ""}</button>`).join("");
    let out = `<div class="psorts">${chips}</div>`, grp = null;
    const lim = PASS_ALL ? fl.length : Math.min(fl.length, 300);
    for (const f of fl.slice(0, lim)) {
      const g = groupOf(f);
      if (g !== grp) { out += `<div class="pgroup"><b>${esc(g)}</b><span>${counts[g]} FLIGHT${counts[g] === 1 ? "" : "S"}</span></div>`; grp = g; }
      const to = f.diverted_to || f.arr;
      out += `<a class="prow" href="#/hist/${encodeURIComponent(f.id)}">${alBadge(airlineCode(f))}
        <div class="pr-main"><div class="pr-top">${esc(flno(f))} <span>${esc(f.dep)} → ${esc(to)}</span>${f.diverted_to ? ' <em class="red">DIVERTED</em>' : ""}${f.cancelled ? ' <em class="red">CANCELLED</em>' : ""}</div>
        <div class="pr-cities">${esc(city(f.dep))} to ${esc(city(to))}</div></div>
        <div class="pr-date">${esc(fmt(fdep(f), f.dep_tz, { month: "short", day: "numeric", year: "numeric" }))}</div></a>`;
    }
    if (lim < fl.length) out += `<button class="btn wide" data-pall="1">Show all ${fl.length} flights</button>`;
    return out;
  }
  function passCards(d, year) {
    const st = d.stats, fl = d.flights.filter((f) => !f.cancelled);
    const y = year ? `/${year}` : "";
    const mi = Math.round((st.distance_nm || 0) * 1.15078);
    const hrs = st.block_hours || st.air_hours || 0;
    const longHaul = fl.filter((f) => (f.distance_nm || 0) * 1.15078 >= 3000).length;
    const airlines = new Set(fl.map((f) => airlineCode(f)).filter(Boolean)).size;
    const late = fl.filter((f) => f.arr_delay != null && f.arr_delay >= 15);
    const lost = late.reduce((a, f) => a + f.arr_delay, 0);
    const ac = (st.top_aircraft || [])[0];
    return `<a class="pcard pass" href="#/history/stats${y}">
        <div class="pc-t">${year ? esc(year) : "All-Time"} Passport</div>
        <div class="pc-st">PILOT LOGBOOK · ${esc(String(st.flights))} FLIGHTS</div>
        <div class="pc-grid">
          <div><span>Flights</span><b>${st.flights.toLocaleString()}</b><small>${longHaul ? `${longHaul} Long Haul` : `${st.routes} routes`}</small></div>
          <div><span>Distance</span><b>${mi.toLocaleString()} mi</b><small>${((st.distance_nm || 0) / 21639).toFixed(1)}x around the world</small></div>
          <div><span>Flight Time</span><b>${Math.floor(hrs / 24)}d ${Math.round(hrs % 24)}h</b><small>gate to gate</small></div>
          <div class="pc-pair"><div><span>Airports</span><b>${st.airports}</b></div><div><span>Airlines</span><b>${airlines}</b></div></div>
        </div>
        <div class="pc-btn">All Flight Stats ${IC.chev}</div></a>
      ${late.length ? `<a class="pcard delay" href="#/history/find?${new URLSearchParams({ ...(year ? { year } : {}), st: "late15", sort: "late" })}">
        <div class="pd-n">${Math.round(lost / 60).toLocaleString()}</div><div class="pd-t">hours lost from delays</div>
        <div class="pd-s">Delayed flights averaged ${fdur(lost / late.length * 60)} late</div>
        <div class="pc-btn">All Delay Stats ${IC.chev}</div></a>` : ""}
      ${ac ? `<a class="pcard acft" href="#/history/find?${new URLSearchParams({ ...(year ? { year } : {}), ac: ac[0] })}">
        <div class="pa-k">Most flown aircraft</div><div class="pa-n">${esc(ac[0])}</div><div class="pa-c">${ac[1]} flights</div>
        ${(() => { const u = acSrc(acKey("", ac[0])); return u ? `<img class="ac-photo pa-photo" src="${u}" alt="" loading="lazy" decoding="async">` : planeSide("#2b4aa0"); })()}<div class="pc-btn">All Aircraft Stats ${IC.chev}</div></a>` : ""}`;
  }



  // ================================================================================================
  // The map: one globe (MapLibre) behind every page. It's made once and never reloads: each page
  // swaps what's drawn on it and the camera glides to that page's routes. Zoomed in it flattens
  // out into a normal map, with the airport diagram and live surface traffic.
  // ================================================================================================
  const MAP_EL = document.getElementById("map");

  // index.html loads the map library without holding up the app; this runs when it arrives
  window.__mapReady = () => { if (GL.queued && !GL.map) { const q = GL.queued; GL.queued = null; showMap(...q); } };
  const GL = { map: null, ready: false, view: null, paint: null, data: null, opts: {}, markers: [],
    bounds: [], maxZoom: 7, fitPending: false, empty: {}, src: {}, leg: null, ctl: null };
  const CARTO_DARK = ["a", "b", "c", "d"].map((s) => `https://${s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png`);
  // CARTO watermarks tiles fetched without its free key. With your key (add-on Configuration) tiles
  // come from its keyed address; a tile address of your own wins ({style} becomes dark_all, {key}
  // your key, {s} the a-d servers).
  // ---------- tiles you've seen stay on the phone ----------
  // Esri and CARTO tiles go through tc://, which answers from the browser's Cache Storage first and
  // only downloads what isn't there. Kept until there are more than TC_MAX (oldest go first).
  // Needs a secure page (https: the Home Assistant app, Nabu Casa) for Cache Storage; on plain
  // http the tiles load directly, as before. Google tiles are never kept (Google's terms).
  const TC_NAME = "ft-tiles-v1", TC_MAX = 16000;
  let TC_ON = false;
  function tcInit() {
    if (TC_ON || !window.maplibregl || !maplibregl.addProtocol || !window.caches || !window.isSecureContext) return;
    TC_ON = true;
    const box = caches.open(TC_NAME).catch(() => null);
    maplibregl.addProtocol("tc", async (params, ac) => {
      const url = "https://" + params.url.slice(5);
      const c = await box;
      if (c) {
        try { const hit = await c.match(url); if (hit) return { data: await hit.arrayBuffer() }; } catch {}
      }
      const r = await fetch(url, { signal: ac.signal });
      if (!r.ok) throw new Error(`tile ${r.status}`);
      const buf = await r.arrayBuffer();
      if (c && buf.byteLength > 0) c.put(url, new Response(buf.slice(0), { headers: { "content-type": r.headers.get("content-type") || "image/jpeg" } })).catch(() => {});
      return { data: buf };
    });
    // once a session, after things settle: trim to the newest TC_MAX
    setTimeout(async () => {
      try { const c = await box; if (!c) return; const keys = await c.keys(); for (const k of keys.slice(0, Math.max(0, keys.length - TC_MAX))) await c.delete(k); } catch {}
    }, 30000);
  }
  const tc = (u) => TC_ON ? u.replace(/^https:\/\//, "tc://") : u;
  // ---------- the map for your trips, downloaded ahead ----------
  // In the background, a little at a time: the satellite imagery along your upcoming flights' routes
  // (zoom 3-8, the views My Flights and a flight page open on), every zoom above their airports and the
  // inbound aircraft's airports (9 to 16, region down to the gates), those airports' diagrams, and
  // every airport in your Passport (to zoom 12). Kept on the
  // phone with the tiles you've seen, so the map opens complete with no signal. Skips what's already
  // saved, so after the first run it only fetches new trips; runs at most every 6 hours.
  const tileXY = (la, lo, z) => {
    const n = 2 ** z, R = Math.max(-85, Math.min(85, la)) * Math.PI / 180;
    return [Math.floor((lo + 180) / 360 * n) & (n - 1), Math.max(0, Math.min(n - 1, Math.floor((1 - Math.log(Math.tan(R) + 1 / Math.cos(R)) / Math.PI) / 2 * n)))];
  };
  const WARMT = { running: false, done: 0, total: 0 };
  function warmStatus() { try { return JSON.parse(localStorage.getItem("ft-warm-trips") || "null"); } catch { return null; } }
  async function warmTrips(force) {
    if (!TC_ON || lowData() || WARMT.running || !STATE) return;
    const st = warmStatus();
    if (!force && st && Date.now() - st.at < 6 * 3600e3) return;
    WARMT.running = true;
    try {
      const want = new Set(), add = (z, x, y) => want.add(`${z}/${y}/${x}`);
      const box = (la, lo, z, r) => { const [x, y] = tileXY(la, lo, z), n = 2 ** z; for (let dx = -r; dx <= r; dx++) for (let dy = -r; dy <= r; dy++) { const yy = y + dy; if (yy >= 0 && yy < n) add(z, (x + dx + n) % n, yy); } };
      const now = Date.now() / 1000, legs = (STATE.legs || []).filter((l) => !l.finished && (l.sched_out || 0) < now + 14 * 86400);
      const close = new Map();
      for (const l of legs) {
        const A = ll(l.dep_ap), B = ll(l.arr_ap);
        if (!A || !B) continue;
        for (const p of gcPoints(A, B, 48)) for (let z = 3; z <= 8; z++) box(p[0], p[1], z, z <= 5 ? 1 : 0);
        close.set(l.dep, A); close.set(l.arr, B);
        // the inbound aircraft's earlier legs too (their airports and routes)
        for (const r of [l.inbound, ...(l.chain || [])].filter(Boolean)) {
          const a = r.origin, b = r.destination, P = ll(a), Q = ll(b);
          if (P && (a.iata || a.icao)) close.set(a.iata || a.icao, P);
          if (Q && (b.iata || b.icao)) close.set(b.iata || b.icao, Q);
          if (P && Q) for (const p of gcPoints(P, Q, 32)) for (let z = 3; z <= 7; z++) box(p[0], p[1], z, 0);
        }
      }
      // above each airport: every zoom from the region (9) down to the gates (16)
      for (const [, p] of close) for (let z = 9; z <= 16; z++) box(p[0], p[1], z, z <= 11 ? 1 : z <= 14 ? 2 : 3);
      // the airport diagrams themselves (runways, taxiways, gates, names), kept with the app's data
      for (const code of close.keys()) { if (code) { try { await api(`api/airport/${encodeURIComponent(code)}/layout`); } catch {} } }
      // Passport airports
      let pass = null;
      try { pass = JSON.parse(localStorage.getItem("ft-c:api/history") || "null"); } catch {}
      for (const a of Object.values((pass && pass.airports) || {})) if (a && a.lat != null) for (let z = 6; z <= 12; z++) box(a.lat, a.lon, z, z <= 9 ? 0 : 1);
      for (const t of Object.keys((LIV && LIV.tails) || {})) { try { await fetch(`api/livery/${t}.png`); } catch {} }
      const c = await caches.open(TC_NAME), list = [...want];
      WARMT.total = list.length; WARMT.done = 0;
      // On your home network (the add-on sees the phone's address): 8 at a time, flat out. Away: 2 at a
      // time with a breath between, so the map you're looking at always comes first.
      let home = false;
      try { home = !!(await api("api/net")).home; } catch {}
      WARMT.home = home;
      let got = 0, i = 0;
      const one = async () => {
        while (i < list.length) {
          if (document.hidden || lowData()) return;   // only while the app is open and data saver is off
          const [z, y, x] = list[i++].split("/");
          const u = ESRI_IMG.replace("{z}", z).replace("{y}", y).replace("{x}", x);
          WARMT.done++;
          if (await c.match(u)) continue;
          try { const r = await fetch(u); if (r.ok) { await c.put(u, r); got++; } } catch {}
          if (!home) await new Promise((ok) => setTimeout(ok, 40));
        }
      };
      await Promise.all(Array.from({ length: home ? 8 : 2 }, one));
      if (WARMT.done >= WARMT.total) localStorage.setItem("ft-warm-trips", JSON.stringify({ at: Date.now(), tiles: list.length, fetched: got }));
    } catch {} finally { WARMT.running = false; }
  }
  // Once: the whole world's coarsest imagery (zoom 0-3, 85 tiles, about 2 MB) into the phone's tile
  // store, so the backdrop under the map is never missing, wherever and however far you zoom.
  async function warmWorld() {
    if (!TC_ON || lowData()) return;
    try {
      if (localStorage.getItem("ft-world-warm") === "1") return;
      const c = await caches.open(TC_NAME);
      for (let z = 0; z <= 3; z++) for (let x = 0; x < 2 ** z; x++) for (let y = 0; y < 2 ** z; y++) {
        const u = ESRI_IMG.replace("{z}", z).replace("{x}", x).replace("{y}", y);
        if (await c.match(u)) continue;
        try { const r = await fetch(u); if (r.ok) await c.put(u, r); } catch {}
      }
      localStorage.setItem("ft-world-warm", "1");
    } catch {}
  }
  async function tileCacheInfo() {
    try { const c = await caches.open(TC_NAME); const n = (await c.keys()).length; const est = navigator.storage && navigator.storage.estimate ? await navigator.storage.estimate() : null; return { n, bytes: est ? est.usage : null }; } catch { return null; }
  }
  async function tileCacheClear() { try { await caches.delete(TC_NAME); } catch {} }

  // ---------- Google satellite (with your Maps API key) ----------
  // Map Tiles API: one session (good for about two weeks), then 2D tiles. 512 px "high DPI" tiles
  // laid out at 256: sharp on a Retina screen without drawing more tiles. Roads and place names
  // come baked in (satellite + roadmap layer). Each tile is a billable request after Google's free
  // monthly allowance.
  const gKey = () => ((STATE && STATE.map_tiles) || {}).google || "";
  const GSESS = { s: null, exp: 0, key: "", pending: null, err: "" };
  try { Object.assign(GSESS, JSON.parse(localStorage.getItem("ft-gsess") || "{}"), { pending: null, err: "" }); } catch {}
  function gReady() { return GSESS.s && GSESS.key === gKey() && GSESS.exp > Date.now() / 1000 + 3600; }
  function gSession() {
    const key = gKey();
    if (!key || gReady() || GSESS.pending) return GSESS.pending;
    GSESS.pending = fetch(`https://tile.googleapis.com/v1/createSession?key=${encodeURIComponent(key)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mapType: "satellite", language: navigator.language || "en-US", region: "US", scale: "scaleFactor2x", highDpi: true, layerTypes: ["layerRoadmap"] }),
    }).then(async (r) => {
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.session) throw new Error((j.error && j.error.message) || `Google said ${r.status}`);
      Object.assign(GSESS, { s: j.session, exp: +j.expiry || Date.now() / 1000 + 86400, key, err: "" });
      try { localStorage.setItem("ft-gsess", JSON.stringify({ s: GSESS.s, exp: GSESS.exp, key })); } catch {}
      if (mapStyle() === "google") restyleAll();
    }).catch((e) => { GSESS.err = e.message || String(e); console.warn("Google tiles:", GSESS.err); })
      .finally(() => { GSESS.pending = null; });
    return GSESS.pending;
  }
  const gTiles = () => `https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=${encodeURIComponent(GSESS.s)}&key=${encodeURIComponent(gKey())}`;
  // Google's terms: its logo and the imagery copyright for what's on screen.
  let gAttrT = 0;
  // Credits live in one place: the map's own credit line, top left, always spelled out (not a
  // collapsible "i"). Google's imagery copyright depends on what's on screen, so it's refreshed
  // after the map stops moving.
  const G_LOGO = '<b class="attr-mb">Google</b>';
  function setAttr(html) {
    const src = GL.map && GL.ready && GL.map.getSource("base");
    if (!src || src.attribution === html) return;
    src.attribution = html;
    try { GL.attrib && GL.attrib._updateAttributions(); } catch {}
  }
  function gAttr() {
    if (!(mapStyle() === "google" && gReady() && GL.map)) return;
    clearTimeout(gAttrT);
    gAttrT = setTimeout(async () => {
      try {
        const b = GL.map.getBounds(), z = Math.max(0, Math.round(GL.map.getZoom()));
        const q = new URLSearchParams({ session: GSESS.s, key: gKey(), zoom: z, north: Math.min(85, b.getNorth()), south: Math.max(-85, b.getSouth()), east: b.getEast(), west: b.getWest() });
        const j = await (await fetch(`https://tile.googleapis.com/tile/v1/viewport?${q}`)).json();
        setAttr(`${G_LOGO} ${esc(j.copyright || "")}`);
      } catch {}
    }, 800);
  }
  // ---------- Mapbox satellite (with your Mapbox token) ----------
  // Static Tiles API, satellite-streets style: imagery with roads and place names drawn in, 512 px
  // (@2x) pictures laid out at 256, so sharp on a Retina screen. One request per tile; Mapbox's free
  // allowance is 200,000 a month. Not kept on the phone (Mapbox's terms).
  const mbTok = () => ((STATE && STATE.map_tiles) || {}).mapbox || "";
  const mbTiles = () => `https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/256/{z}/{x}/{y}@2x?access_token=${encodeURIComponent(mbTok())}`;
  const STYLES = () => ["satellite", "dark"];

  function darkTiles() {
    const mt = (STATE && STATE.map_tiles) || {};
    if (mt.url) {
      const u = mt.url.replace("{style}", "dark_all").replace("{key}", encodeURIComponent(mt.key || "")).replace("{r}", "@2x");
      return u.includes("{s}") ? ["a", "b", "c", "d"].map((x) => u.replace("{s}", x)) : [u];
    }
    if (mt.key) return [`https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}.png?key=${encodeURIComponent(mt.key)}`];
    return CARTO_DARK;
  }
  function glStyle() {
    let st = mapStyle();
    if (st === "google" && !gReady()) { gSession(); st = "satellite"; }   // Esri until Google's session is ready
    if (st === "google" && !gKey()) st = "satellite";
    if (st === "mapbox" && !mbTok()) st = "satellite";
    const mb = st === "mapbox", goo = st === "google" || mb, sat = st !== "dark", low = lowData();
    // Image tiles are 256 px. The map picks its tile zoom by rounding, so a tile can be drawn up to
    // 1.4x its size, and 2x more on a Retina screen: soft, worst when the globe sits halfway between
    // two tile zooms (dragging north or south crosses one). Laid out at 181 px (256/sqrt 2) the
    // worst case is 2x and the usual ~1.4x, for twice the tiles; 128 px was sharp but 4x the tiles
    // (stutter, black holes). Data saver: 512, a quarter of the downloads. Google's tiles are 512 px
    // pictures already: 256 is sharp.
    // Map sharpness Balanced or Fast: 256, half the tiles (they're drawn at fewer pixels anyway).
    const ts = goo ? 256 : low ? 512 : pref("ft-mapq", "full") === "full" ? 181 : 256;
    // A coarse copy of the imagery underneath (2 zoom levels lower, at most z8: a handful of tiles,
    // cached): while you drag, anything not loaded yet shows a softer earth instead of black.
    // At most zoom 3 (85 tiles for the whole world, kept on the phone, see warmWorld): something is
    // always underneath, anywhere at any zoom, even mid-flight from an airport out to the globe.
    const under = (tiles) => ({ type: "raster", tiles, tileSize: 1024, maxzoom: 3 });
    const img = tc(ESRI_IMG), dark = darkTiles().map(tc);
    setTimeout(gAttr, 0);
    return {
      version: 8,
      projection: { type: flatMap() ? "mercator" : "globe" },
      sources: mb
        ? { under: under([img]), base: { type: "raster", tiles: [mbTiles()], tileSize: ts, maxzoom: 22, attribution: '<b class="attr-mb">mapbox</b> &copy; Mapbox &copy; OpenStreetMap &copy; Maxar' } }
        : goo
        ? { under: under([img]), base: { type: "raster", tiles: [gTiles()], tileSize: ts, maxzoom: 22, attribution: G_LOGO } }
        : sat
        ? { under: under([img]), base: { type: "raster", tiles: [img], tileSize: ts, maxzoom: 19, attribution: "&copy; Esri, Maxar &middot; Natural Earth" } }
        : { under: under(dark), base: { type: "raster", tiles: dark, tileSize: ts, maxzoom: 19, attribution: "&copy; OpenStreetMap &copy; CARTO" } },
      layers: [
        { id: "under", type: "raster", source: "under", paint: sat ? { "raster-brightness-max": 0.84, "raster-saturation": -0.08, "raster-fade-duration": 0 } : { "raster-fade-duration": 0 } },
        { id: "base", type: "raster", source: "base", paint: sat ? { "raster-brightness-max": 0.84, "raster-saturation": -0.08, "raster-fade-duration": 120 } : { "raster-fade-duration": 120 } },
      ],
    };
  }
  // The night side as a picture laid over the earth: dark where the sun is down, a soft twilight edge.
  function nightImage() {
    const W = 360, Hh = 360, c = document.createElement("canvas");
    c.width = W; c.height = Hh;
    const ctx = c.getContext("2d"), img = ctx.createImageData(W, Hh), s = subsolar(new Date()), R = Math.PI / 180;
    const sd = Math.sin(s.lat * R), cd = Math.cos(s.lat * R);
    for (let y = 0; y < Hh; y++) {
      const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * (y + 0.5) / Hh)));   // Mercator rows
      const sl = Math.sin(lat), cl = Math.cos(lat);
      for (let x = 0; x < W; x++) {
        const alt = Math.asin(sl * sd + cl * cd * Math.cos((-180 + (x + 0.5) - s.lon) * R)) / R;
        const t = Math.max(0, Math.min(1, (2 - alt) / 14));
        const i = (y * W + x) * 4;
        img.data[i] = 2; img.data[i + 1] = 6; img.data[i + 2] = 22; img.data[i + 3] = Math.round(t * t * (3 - 2 * t) * 150);
      }
    }
    ctx.putImageData(img, 0, 0);
    return c.toDataURL();
  }
  const MERC = [[-180, 85.0511], [180, 85.0511], [180, -85.0511], [-180, -85.0511]];
  const FC = (features) => ({ type: "FeatureCollection", features: features || [] });
  const lineF = (pts, props) => ({ type: "Feature", properties: props || {}, geometry: { type: "LineString", coordinates: pts.map((p) => [p[1], p[0]]) } });
  const ptF = (p, props) => ({ type: "Feature", properties: props || {}, geometry: { type: "Point", coordinates: [p[1], p[0]] } });
  const ring = (pts) => { const r = pts.map((p) => [p[1], p[0]]); if (r.length && (r[0][0] !== r[r.length - 1][0] || r[0][1] !== r[r.length - 1][1])) r.push(r[0]); return r; };
  const DYN = ["artcc", "plan", "routes", "track", "aps", "pts", "fixes"];
  const LAY = ["lay-term", "lay-twy", "lay-rwy"];
  // runways are drawn about their real width (45 m) at every zoom, taxiways about 23 m
  const widthM = (m) => ["interpolate", ["exponential", 2], ["zoom"], 10, m / 60, 20, m * 17];
  function glLayers() {
    const map = GL.map, route = css("--route");
    const before = map.getLayer("lbl") ? "lbl" : undefined;
    if (!map.getSource("night")) map.addSource("night", { type: "image", url: nightImage(), coordinates: MERC });
    for (const id of [...DYN, ...LAY, "sym", "laysym", "surf"]) if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: FC() });
    const add = (l, b) => { if (!map.getLayer(l.id)) map.addLayer(l, b); };
    add({ id: "night", type: "raster", source: "night", paint: { "raster-fade-duration": 0 } }, before);
    placeLayers(add);
    add({ id: "lay-term", type: "fill", minzoom: 12.5, source: "lay-term", paint: { "fill-color": "#c8cdd8", "fill-opacity": 0.14 } });
    add({ id: "lay-term-line", type: "line", minzoom: 12.5, source: "lay-term", paint: { "line-color": "#c8cdd8", "line-opacity": 0.5, "line-width": 1 } });
    add({ id: "lay-twy", type: "line", minzoom: 12.5, source: "lay-twy", layout: { "line-join": "round" }, paint: { "line-color": "#fff", "line-opacity": 0.12, "line-width": widthM(23) } });
    add({ id: "lay-twycl", type: "line", source: "lay-twy", minzoom: 16, paint: { "line-color": "#f6c445", "line-opacity": 0.45, "line-width": 1 } });
    add({ id: "lay-rwy", type: "line", minzoom: 12.5, source: "lay-rwy", layout: { "line-cap": "butt" }, paint: { "line-color": "#fff", "line-opacity": 0.22, "line-width": widthM(45) } });
    add({ id: "lay-rwycl", type: "line", minzoom: 12.5, source: "lay-rwy", paint: { "line-color": "#fff", "line-opacity": 0.55, "line-width": 1, "line-dasharray": [8, 8] } });
    add({ id: "artcc-fill", type: "fill", source: "artcc", paint: { "fill-color": ["get", "c"], "fill-opacity": ["get", "o"] } });
    add({ id: "artcc-line", type: "line", source: "artcc", paint: { "line-color": ["get", "c"], "line-width": ["get", "w"], "line-opacity": ["get", "lo"] } });
    add({ id: "plan", type: "line", source: "plan", paint: { "line-color": ["coalesce", ["get", "c"], route], "line-width": 2, "line-opacity": ["coalesce", ["get", "o"], 0.6], "line-dasharray": [2.5, 2.5] } });
    add({ id: "routes", type: "line", source: "routes", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["coalesce", ["get", "c"], route], "line-width": ["coalesce", ["get", "w"], 2.4], "line-opacity": 0.92 } });
    add({ id: "track-edge", type: "line", source: "track", filter: ["==", ["get", "own"], 1], layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#04122e", "line-width": 7.5, "line-opacity": 0.45 } });
    add({ id: "track", type: "line", source: "track", layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": ["coalesce", ["get", "c"], route], "line-width": ["coalesce", ["get", "w"], 4], "line-opacity": ["coalesce", ["get", "o"], 1] } });
    add({ id: "fixes", type: "circle", source: "fixes", minzoom: 5, paint: { "circle-radius": 1.6, "circle-color": route } });
    add({ id: "aps", type: "circle", source: "aps", paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, ["coalesce", ["get", "r0"], 4.5], 4, 4.5],
      "circle-color": ["coalesce", ["get", "c"], route], "circle-stroke-color": "#fff", "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 1, ["coalesce", ["get", "s0"], 2], 4, 2] } });
    add({ id: "pts", type: "circle", source: "pts", paint: { "circle-radius": ["coalesce", ["get", "r"], 6.5], "circle-color": ["get", "c"], "circle-stroke-color": "#fff", "circle-stroke-width": ["coalesce", ["get", "s"], 1.5] } });
    symLayers(add);
    GL.empty = {}; GL.src = {};
    if (radarOn()) glRadar(true);
  }
  // Each paint says what it draws; everything else on the map is cleared.
  function glSet(obj) {
    for (const id of DYN) {
      const v = obj[id], src = GL.map.getSource(id);
      if (!src) continue;
      if (!v || (Array.isArray(v) && !v.length)) {
        if (!GL.empty[id]) { src.setData(FC()); GL.empty[id] = true; GL.src[id] = null; }
        continue;
      }
      const fc = Array.isArray(v) ? FC(v) : v;
      if (fc === GL.src[id]) continue;   // the same (big) collection as last time: leave it
      src.setData(fc); GL.src[id] = fc; GL.empty[id] = false;
    }
  }
  // Radar. NOAA (default): the National Weather Service's MRMS mosaic, every NEXRAD radar merged and
  // quality-controlled (ground clutter, birds and false echoes removed), as on radar.weather.gov;
  // US, Canada, Caribbean, Alaska, Hawaii; new every 5 minutes. RainViewer: worldwide, but raw.
  const radarSrc = () => pref("ft-radar-src", "noaa");
  const NOAA_RADAR = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/radar/radar_base_reflectivity/MapServer/export"
    + "?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=512,512&format=png32&transparent=true&layers=show:3&f=image";
  let radarT = null;
  async function glRadar(on) {
    const map = GL.map;
    if (!map) return;
    if (map.getLayer("radar")) map.removeLayer("radar");
    if (map.getSource("radar")) map.removeSource("radar");
    clearInterval(radarT); radarT = null;
    if (!on) return;
    let src;
    try {
      if (radarSrc() === "noaa") {
        // a new picture every 5 minutes: the address changes with it, so the phone doesn't reuse an old one
        src = { type: "raster", tiles: [`${NOAA_RADAR}&_t=${Math.floor(Date.now() / 300000)}`], tileSize: 512, maxzoom: 10,
          attribution: "Radar &copy; NOAA/NWS MRMS" };
      } else {
        if (!RADAR || Date.now() - RADAR.at > 300000) {
          const d = await fetch("https://api.rainviewer.com/public/weather-maps.json").then((r) => r.json());
          const fr = d.radar.past[d.radar.past.length - 1];
          RADAR = { url: `${d.host}${fr.path}/256/{z}/{x}/{y}/2/1_1.png`, at: Date.now() };
        }
        src = { type: "raster", tiles: [RADAR.url], tileSize: 256, maxzoom: 7, attribution: "Radar &copy; RainViewer" };
      }
      if (map.getSource("radar")) return;
      map.addSource("radar", src);
      map.addLayer({ id: "radar", type: "raster", source: "radar", paint: { "raster-opacity": 0.65, "raster-fade-duration": 0 } },
        map.getLayer("artcc-fill") ? "artcc-fill" : undefined);
      radarT = setInterval(() => { if (radarOn() && !document.hidden) glRadar(true); }, 300000);
    } catch {}
  }
  // ---------- labels and airplanes, drawn by the GPU ----------
  // Each label or airplane is a small picture drawn once (on a canvas) and handed to the map, which
  // places, rotates and hides colliding ones itself on the GPU: nothing on the page moves while you
  // zoom or pan. A picture's name is its recipe, so the map asks for it again after a style switch.
  const IMG_R = 2;
  const LBL_FONT = "-apple-system, BlinkMacSystemFont, 'Helvetica Neue', Helvetica, Arial, sans-serif";
  const PLANE_PATH = "M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z";
  const imgId = (spec) => "x:" + JSON.stringify(spec);
  function rrect(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  // spec: [kind, text or color, second text or color/size, left padding (the gap to its point)]
  function drawImg(spec) {
    const [k, a, b, pad] = spec, R = IMG_R;
    let c = document.createElement("canvas"), ctx = c.getContext("2d");
    const size = (w, h) => { c.width = Math.ceil(w * R); c.height = Math.ceil(h * R); ctx.setTransform(R, 0, 0, R, 0, 0); };
    const font = (wgt, px) => `${wgt} ${px}px ${LBL_FONT}`;
    const tw = (f, t) => { ctx.font = f; return ctx.measureText(String(t)).width; };
    if (k === "plane" || k === "badge") {
      const s_ = (k === "badge" ? 28 : b) || 30, W_ = s_ + 8, cx = W_ / 2;
      size(W_, W_);
      if (k === "badge") {
        ctx.beginPath(); ctx.arc(cx, cx, s_ / 2 + 3, 0, 7); ctx.fillStyle = "rgba(47,111,232,.35)"; ctx.fill();
        ctx.beginPath(); ctx.arc(cx, cx, s_ / 2, 0, 7); ctx.fillStyle = "#2f6fe8"; ctx.fill();
      }
      const ps = k === "badge" ? s_ * 0.62 : s_, path = new Path2D(PLANE_PATH);
      ctx.save(); ctx.translate(cx - ps / 2, cx - ps / 2); ctx.scale(ps / 24, ps / 24);
      if (k !== "badge") { ctx.shadowColor = "rgba(0,0,0,.6)"; ctx.shadowBlur = 3; ctx.shadowOffsetY = 1; }
      ctx.fillStyle = k === "badge" ? "#fff" : a || "#fff"; ctx.fill(path);
      if (k !== "badge") { ctx.shadowColor = "transparent"; ctx.lineWidth = 0.6; ctx.strokeStyle = "rgba(0,0,0,.35)"; ctx.stroke(path); }
      ctx.restore();
    } else if (k === "city") {
      const f1 = font(600, 12.5), f2 = font(700, 12.5), w1 = a ? tw(f1, a) + 15 : 0, w2 = tw(f2, b) + 15, h = 22;
      size(w1 + w2 + 2, h + 2);
      ctx.save(); rrect(ctx, 1, 1, w1 + w2, h, 7); ctx.clip();
      ctx.fillStyle = "#2f6fe8"; ctx.fillRect(1, 1, w1, h); ctx.fillStyle = "#1b4fbd"; ctx.fillRect(1 + w1, 1, w2, h); ctx.restore();
      ctx.fillStyle = "#fff"; ctx.textBaseline = "middle";
      if (a) { ctx.font = f1; ctx.fillText(a, 8.5, 1 + h / 2); }
      ctx.font = f2; ctx.fillText(b, 1 + w1 + 7.5, 1 + h / 2);
    } else if (["pill", "stag", "rwy", "twy"].includes(k)) {
      const st = { pill: [700, 11.5, "#fff", b || "#2f6fe8", 7, 7, 21], stag: [600, 10, "#fff", b || "rgba(28,28,30,.85)", 5, 5, 16],
        rwy: [800, 10, "#fff", "#a3191f", 2, 4, 16], twy: [800, 9.5, "#f6c445", "#0b0b0b", 2, 3, 14] }[k];
      const f = font(st[0], st[1]), w = tw(f, a) + st[5] * 2, h = st[6];
      size(w + 2, h + 2); rrect(ctx, 1, 1, w, h, st[4]); ctx.fillStyle = st[3]; ctx.fill();
      if (k === "twy") { ctx.lineWidth = 1; ctx.strokeStyle = "#f6c445"; ctx.stroke(); }
      ctx.font = f; ctx.fillStyle = st[2]; ctx.textBaseline = "middle"; ctx.fillText(a, 1 + st[5], 1 + h / 2 + 0.5);
    } else if (k === "pc" || k === "ps" || k === "pt") {
      // place names: countries in spaced capitals, states smaller and dimmer, cities with a dot
      const [wgt, px, col, sp, up] = { pc: [700, 11, "rgba(255,255,255,.88)", 1.6, true], ps: [600, 9.5, "rgba(255,255,255,.66)", 1.1, true],
        pt: [600, 11.5, "#fff", 0, false] }[k];
      const t = up ? String(a).toUpperCase() : String(a), f = font(wgt, px);
      ctx.font = f;
      const chars = [...t], cw = chars.map((ch) => ctx.measureText(ch).width), tw_ = cw.reduce((x, y) => x + y, 0) + sp * Math.max(0, chars.length - 1);
      const dot = k === "pt" ? 10 : 0, w = tw_ + 6 + dot, h = px + 8;
      size(w, h); ctx.font = f; ctx.textBaseline = "middle"; ctx.lineJoin = "round";
      if (dot) {
        ctx.beginPath(); ctx.arc(4, h / 2, 2.6, 0, 7); ctx.fillStyle = "#fff"; ctx.fill();
        ctx.lineWidth = 1; ctx.strokeStyle = "rgba(0,0,0,.7)"; ctx.stroke();
      }
      const run = (fn) => { let x = 3 + dot; chars.forEach((ch, i) => { fn(ch, x); x += cw[i] + sp; }); };
      ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.72)"; run((ch, x) => ctx.strokeText(ch, x, h / 2));
      ctx.fillStyle = col; run((ch, x) => ctx.fillText(ch, x, h / 2));
    } else {   // text with a dark halo: airport codes, route fixes, terminals, gates, centers
      const st = { code: [700, 10, "#fff"], fix: [600, 9, "#c3c7cf"], term: [700, 11, "rgba(255,255,255,.9)"], gate: [600, 9, "#b8bcc4"],
        ctr: [800, b ? 12 : 10, b || "#7c7c82"] }[k] || [600, 10, "#fff"];
      const f = font(st[0], st[1]), w = tw(f, a) + 6, h = st[1] + 8;
      size(w, h); ctx.font = f; ctx.textBaseline = "middle"; ctx.lineJoin = "round";
      ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,.85)"; ctx.strokeText(a, 3, h / 2);
      ctx.fillStyle = st[2]; ctx.fillText(a, 3, h / 2);
    }
    if (pad) {   // a transparent gap on the left: the label sits beside its point, not on it
      const c2 = document.createElement("canvas"), x2 = c2.getContext("2d");
      c2.width = c.width + Math.round(pad * R); c2.height = c.height; x2.drawImage(c, Math.round(pad * R), 0);
      c = c2; ctx = x2;
    }
    return { width: c.width, height: c.height, data: new Uint8Array(ctx.getImageData(0, 0, c.width, c.height).data.buffer) };
  }
  // A label or airplane: L picks the layer (lbl always shown, code from zoom 3, fix from zoom 7,
  // plane rotated with the map), anc the side of the picture on the point.
  const symF = (p, spec, L, anc, rot, extra) => ({ type: "Feature", properties: { img: imgId(spec), L, anc: anc || "center", rot: rot || 0, ...(extra || {}) },
    geometry: { type: "Point", coordinates: [p[1], p[0]] } });
  const planeMk = (pos, hdg, color, badge, size) => symF(pos, badge ? ["badge"] : ["plane", color || "#fff", size || 0], "plane", "center", hdg);
  let symT = 0;
  const flushSym = () => {
    if (symT) return;
    symT = requestAnimationFrame(() => { symT = 0; const s_ = GL.ready && GL.map.getSource("sym"); if (s_) s_.setData(FC(GL.markers.filter((f) => !f.properties.hide))); });
  };
  function glMarkers(list) {
    GL.markers = list;
    const s_ = GL.ready && GL.map.getSource("sym");
    if (s_) s_.setData(FC(list));
    return list;
  }
  // move or hide one airplane without redrawing anything else
  const mkHandle = (f) => f ? { setLatLng: (p) => { f.geometry.coordinates = [p[1], p[0]]; flushSym(); },
    setOpacity: (o) => { f.properties.hide = o ? 0 : 1; flushSym(); } } : null;
  const SYM = (overlap) => ({ "icon-image": ["get", "img"], "icon-anchor": ["get", "anc"], "icon-allow-overlap": overlap, "icon-ignore-placement": overlap, "icon-padding": 1 });
  // ---------- place names and borders (Esri imagery) ----------
  // From Natural Earth via the add-on (/api/places): drawn as crisp text like the map's other labels,
  // so they're sharp at every zoom and don't jump in size between tile zooms. Countries, then states
  // and provinces, then cities by population, each from the zoom Natural Earth suggests; the map
  // drops ones that would collide. Hidden on Mapbox, Google and Dark, whose tiles have names.
  let PLACES = null, placesP = null;
  const placesOn = () => { const st = mapStyle(); return !(st === "dark" || (st === "mapbox" && mbTok()) || (st === "google" && gReady())); };
  function loadPlaces() {
    if (PLACES || placesP) return placesP;
    placesP = fetch("api/places").then((r) => r.ok ? r.json() : Promise.reject(new Error(r.status))).then((d) => {
      const pts = [], lines = [];
      (d.countries || []).forEach(([n, la, lo, mz]) => pts.push(symF([la, lo], ["pc", n], "plc", "center", 0, { mz: Math.max(0.6, mz - 1.2), k: mz })));
      (d.states || []).forEach(([n, la, lo, mz]) => pts.push(symF([la, lo], ["ps", n], "plc", "center", 0, { mz: Math.max(2.2, mz - 1.5), k: 20 + mz })));
      (d.cities || []).forEach(([n, la, lo, mz], i) => pts.push(symF([la, lo], ["pt", n], "plc", "left", 0, { mz: Math.max(2, mz - 1), k: 100 + i })));
      (d.borders || []).forEach((l) => lines.push({ type: "Feature", properties: { a: 0 }, geometry: { type: "LineString", coordinates: l } }));
      (d.state_lines || []).forEach((l) => lines.push({ type: "Feature", properties: { a: 1 }, geometry: { type: "LineString", coordinates: l } }));
      PLACES = { pts: FC(pts), lines: FC(lines) };
      paintPlaces();
    }).catch(() => { placesP = null; setTimeout(loadPlaces, 60000); });
    return placesP;
  }
  function paintPlaces() {
    const map = GL.map;
    if (!map || !GL.ready) return;
    const vis = placesOn() ? "visible" : "none";
    for (const id of ["plc", "brd0", "brd1"]) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", vis);
    if (vis === "none") return;
    if (!PLACES) { loadPlaces(); return; }
    const a = map.getSource("plc"), b = map.getSource("brd");
    if (a && a._ftSet !== PLACES.pts) { a.setData(PLACES.pts); a._ftSet = PLACES.pts; }
    if (b && b._ftSet !== PLACES.lines) { b.setData(PLACES.lines); b._ftSet = PLACES.lines; }
  }
  // the globe lowers its zoom number as you move toward the poles (it keeps the earth the same size),
  // so zoom-based rules would drop names and state lines there: shift them by the same amount
  let plcOff = null;
  function placeZoomFix(force) {
    const map = GL.map;
    if (!map || !GL.ready || !map.getLayer("plc")) return;
    const off = flatMap() ? 0 : Math.round(-Math.log2(Math.max(0.2, Math.cos(map.getCenter().lat * Math.PI / 180))) * 10) / 10;
    if (!force && off === plcOff) return;
    plcOff = off;
    map.setFilter("plc", [">=", ["+", ["zoom"], off], ["get", "mz"]]);
    map.setLayerZoomRange("brd1", Math.max(0, 2.3 - off), 24);
  }
  function placeLayers(add) {
    const map = GL.map;
    for (const id of ["plc", "brd"]) if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: FC() });
    add({ id: "brd0", type: "line", source: "brd", filter: ["==", ["get", "a"], 0], paint: { "line-color": "#fff", "line-opacity": 0.5, "line-width": 0.9 } });
    add({ id: "brd1", type: "line", source: "brd", minzoom: 2.3, filter: ["==", ["get", "a"], 1], paint: { "line-color": "#fff", "line-opacity": 0.28, "line-width": 0.7 } });
    add({ id: "plc", type: "symbol", source: "plc", filter: [">=", ["zoom"], ["get", "mz"]],
      layout: { ...SYM(false), "icon-padding": 3, "symbol-sort-key": ["get", "k"], "icon-offset": ["case", ["==", ["get", "anc"], "left"], ["literal", [-4, 0]], ["literal", [0, 0]]] } });
    setTimeout(() => { paintPlaces(); placeZoomFix(true); }, 0);
  }
  function symLayers(add) {
    const not = ["!=", ["get", "hide"], 1];
    add({ id: "sym-fix", type: "symbol", source: "sym", minzoom: 7, filter: ["all", ["==", ["get", "L"], "fix"], not], layout: SYM(false) });
    add({ id: "sym-code", type: "symbol", source: "sym", minzoom: 3, filter: ["all", ["==", ["get", "L"], "code"], not], layout: SYM(false) });
    add({ id: "lay-a", type: "symbol", source: "laysym", minzoom: 12.5, filter: ["==", ["get", "L"], "a"], layout: SYM(true) });
    add({ id: "lay-t", type: "symbol", source: "laysym", minzoom: 14, filter: ["==", ["get", "L"], "t"], layout: SYM(false) });
    add({ id: "lay-g", type: "symbol", source: "laysym", minzoom: 15, filter: ["==", ["get", "L"], "g"], layout: SYM(false) });
    add({ id: "surf-dot", type: "circle", source: "surf", filter: ["==", ["get", "L"], "dot"], paint: { "circle-radius": 2.5, "circle-color": "#8e8e93" } });
    add({ id: "surf-plane", type: "symbol", source: "surf", filter: ["==", ["get", "L"], "plane"],
      layout: { ...SYM(true), "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map" } });
    add({ id: "surf-tag", type: "symbol", source: "surf", filter: ["==", ["get", "L"], "tag"], layout: SYM(false) });
    add({ id: "sym-lbl", type: "symbol", source: "sym", filter: ["all", ["==", ["get", "L"], "lbl"], not], layout: SYM(true) });
    add({ id: "sym-plane", type: "symbol", source: "sym", filter: ["all", ["==", ["get", "L"], "plane"], not],
      layout: { ...SYM(true), "icon-rotate": ["get", "rot"], "icon-rotation-alignment": "map" } });
  }
  // Fit the page's routes into the part of the screen the sheet doesn't cover (at its middle stop).
  function glFit(animate, now) {
    const map = GL.map;
    if (!map) return;
    const b = GL.bounds;
    const padding = mapPad(GL.opts.kind === "leg" || GL.opts.kind === "nas", now);
    // a program's centers usually span the whole country, wider than a phone: use the full width
    // (center labels sit on their centers, nothing hangs off to the right)
    if (GL.opts.kind === "nas") { padding.left = 18; padding.right = 58; }
    // My Flights: the routes centered on the screen, as wide as it allows (the buttons may overlap a
    // corner; shifting everything left to dodge them made the globe look lopsided)
    if (!GL.opts.kind) { padding.left = 26; padding.right = 26; }   // Passport: the whole globe, centered
    // My Flights: keep clear of the blurred strip under the status bar (and the credit line in it)
    // and of the button column on the right, so the routes sit in the open part of the map
    if (GL.opts.kind === "home") { padding.left = 22; padding.right = 66; padding.top = Math.max(padding.top, 92); }
    if (GL.opts.kind === "leg") padding.top = Math.max(padding.top, 92);
    GL.fitBottom = padding.bottom;
    const dur = animate ? 900 : 0;
    // Face-on, like Flighty: the camera looks straight at the middle of the routes, and the map's
    // padding slides the whole picture into the part of the screen above the sheet. (fitBounds
    // instead aimed the camera far south of the routes to make room for the sheet: on the globe that
    // tilts the view, so the routes sat foreshortened near the top edge of the earth.)
    if (GL.globeView || !b.length) {
      const c = GL.globeView || [35, -85];
      camTo({ center: [c[1], c[0]], zoom: GL.globeZoom || 1.6, padding }, dur);
      return;
    }
    let w = 180, e = -180, s = 90, n = -90;
    for (const [la, lo] of b) { w = Math.min(w, lo); e = Math.max(e, lo); s = Math.min(s, la); n = Math.max(n, la); }
    const my = (la) => Math.log(Math.tan(Math.PI / 4 + Math.max(-85, Math.min(85, la)) * Math.PI / 360));
    const cLat = Math.atan(Math.sinh((my(s) + my(n)) / 2)) * 180 / Math.PI, center = [(w + e) / 2, cLat];
    // the zoom at which the box fills the padded area (Web Mercator: 512 px world at zoom 0); on the
    // globe the map's zoom number means the same scale at the point it's centered on
    const W = MAP_EL.clientWidth || window.innerWidth, H = MAP_EL.clientHeight || window.innerHeight;
    const aw = Math.max(80, W - padding.left - padding.right), ah = Math.max(80, H - padding.top - padding.bottom);
    const dx = Math.max(1e-6, (e - w) / 360), dy = Math.max(1e-6, (my(n) - my(s)) / (2 * Math.PI));
    // with room around them, like Flighty: My Flights' routes take about 90% of the open space (some
    // context and the globe's curve around them), a flight 85%, Passport 90%, an FAA program all of it
    const fill = { home: 0.9, leg: 0.85, nas: 1 }[GL.opts.kind] || 0.9;
    const maxZ = GL.maxZoom || 7;
    let c = center, z = Math.max(0.5, Math.min(Math.log2(Math.min(aw / (dx * 512), ah / (dy * 512)) * fill), maxZ));
    // Then measure on screen, the way you'd eyeball it: try the camera (no frame is drawn in
    // between), see where the routes' northern, southern, eastern and western ends land, and nudge
    // until they sit the same distance from the edges of the space above the sheet. The globe's
    // curve makes a flat-map estimate sit a little high or low; this takes it out.
    try {
      const orig = { center: map.getCenter(), zoom: map.getZoom(), padding: map.getPadding() };
      const pts = b.map(([la, lo]) => [lo, la]);
      for (let i = 0; i < 4; i++) {
        map.jumpTo({ center: c, zoom: z, padding });
        let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9;
        for (const p of pts) { const q = map.project(p); x0 = Math.min(x0, q.x); x1 = Math.max(x1, q.x); y0 = Math.min(y0, q.y); y1 = Math.max(y1, q.y); }
        if (!isFinite(x0)) break;
        const k = Math.min(aw / Math.max(1, x1 - x0), ah / Math.max(1, y1 - y0)) * fill;
        const tx = padding.left + aw / 2, ty = padding.top + ah / 2, mx = (x0 + x1) / 2, my_ = (y0 + y1) / 2;
        const cc = map.unproject([mx, my_]);   // the camera's point shows at (tx, ty): aim it at the routes' middle
        c = [cc.lng, cc.lat];
        z = Math.max(0.5, Math.min(z + Math.log2(k), maxZ));
        if (Math.abs(Math.log2(k)) < 0.03 && Math.abs(mx - tx) < 3 && Math.abs(my_ - ty) < 3) break;
      }
      map.jumpTo(orig);
    } catch {}
    camTo({ center: c, zoom: z, padding }, dur);
  }
  // Short moves glide. Zooming far out (from an airport back to your routes) pulls back like a
  // camera: the zoom eases out while the center travels in step with how much of the earth comes
  // into view, so the airport stays roughly put at first and the view settles over your routes as
  // it widens (a fly-to slid sideways first; a plain glide looked like passing through the globe).
  // Zooming far in still flies, which already looks right.
  let camAnim = 0;
  // A long zoom (into an airport, out to your routes) would load and draw imagery at every zoom
  // level it passes through, most of it on screen for a split second: that's the stutter. The way
  // Google's map does it instead: keep showing the pictures already loaded, stretched, while the
  // camera moves, refreshing them only every 3 zoom levels, then load the sharp ones where it stops
  // (usually already on the phone: the trip-map download saves the close-ups of your airports).
  const HOLD = { on: false, z: 0, t: 0 };
  const holdCaches = () => { const sc = (GL.map && GL.map.style && GL.map.style.sourceCaches) || {}; return ["base", "under"].map((k) => sc[k]).filter((c) => c && c.pause && c.resume); };
  function holdTiles() {
    if (!GL.map || HOLD.on) return;
    HOLD.on = true; HOLD.z = GL.map.getZoom();
    for (const c of holdCaches()) { try { c.pause(); } catch {} }
    clearTimeout(HOLD.t); HOLD.t = setTimeout(releaseTiles, 3500);   // never stuck
  }
  function releaseTiles() {
    clearTimeout(HOLD.t);
    if (!HOLD.on) return;
    HOLD.on = false;
    for (const c of holdCaches()) { try { c.resume(); } catch {} }
    try { GL.map.triggerRepaint(); } catch {}
  }
  function holdStep() {   // during the move: a fresh set every 3 zoom levels
    if (!HOLD.on) return;
    const z = GL.map.getZoom();
    if (Math.abs(z - HOLD.z) < 3) return;
    HOLD.z = z;
    for (const c of holdCaches()) { try { c.resume(); c.pause(); } catch {} }
  }
  function camTo(o, dur) {
    const map = GL.map;
    cancelAnimationFrame(camAnim); camAnim = 0;
    const z0 = map.getZoom(), z1 = o.zoom ?? z0;
    if (dur && Math.abs(z1 - z0) > 2.5) holdTiles();
    if (!dur || Math.abs(z1 - z0) <= 2.5) { map.easeTo({ ...o, duration: dur }); return; }
    if (z1 > z0) { map.flyTo({ ...o, duration: Math.min(2200, Math.max(1300, dur * 1.6)), curve: 1.42, essential: true }); return; }
    const M = maplibregl.MercatorCoordinate, a = M.fromLngLat(map.getCenter()), bC = M.fromLngLat(o.center);
    const p0 = map.getPadding(), p1 = { ...p0, ...(o.padding || {}) };
    const s0 = 2 ** -z0, s1 = 2 ** -z1, T = Math.min(1800, Math.max(1100, dur * 1.4)), t0 = performance.now();
    const ease = (t) => t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    map.stop();
    const stopOnTouch = () => { cancelAnimationFrame(camAnim); camAnim = 0; releaseTiles(); };
    map.once("dragstart", stopOnTouch); map.once("zoomstart", (e) => { if (e.originalEvent) stopOnTouch(); });
    const step = (now) => {
      const t = Math.min(1, (now - t0) / T), e = ease(t), z = z0 + (z1 - z0) * e;
      const f = (2 ** -z - s0) / (s1 - s0);   // how far the view has widened, 0 to 1
      const x = a.x + (bC.x - a.x) * f, y = a.y + (bC.y - a.y) * f;
      const pad = {}; for (const k of ["top", "bottom", "left", "right"]) pad[k] = p0[k] + (p1[k] - p0[k]) * e;
      map.jumpTo({ center: new M(x, y).toLngLat(), zoom: z, padding: pad });
      if (t < 1) camAnim = requestAnimationFrame(step);
      else { camAnim = 0; map.fire("moveend"); }
    };
    camAnim = requestAnimationFrame(step);
  }
  // The part of the screen the map shows above the sheet (at its middle stop), less room for the
  // buttons on the right, the speed pill on top, and labels that hang off to the right of a point.
  // now: use where the sheet is right now (pulled down to peek: the bigger map), else its middle stop
  function mapPad(labels, now) {
    const H = MAP_EL.clientHeight || window.innerHeight;
    let top = midTop();
    if (now) { const g = document.querySelector(".sheet-gap"); const t = g ? g.getBoundingClientRect().bottom : top; if (t > top) top = t; }
    return { top: document.getElementById("telem") ? 96 : 52, left: 36, right: labels ? 140 : 80,
      bottom: Math.max(24, Math.min(H - 170, H - top + 12)) };
  }
  // shift a centered point into the middle of the part of the map you can see
  const padOffset = (p) => [(p.left - p.right) / 2, (p.top - p.bottom) / 2];
  function restyleAll() {
    if (!GL.map) return;
    GL.ready = false;
    GL.layoutFor = null;
    GL.map.setStyle(glStyle(), { diff: false });
  }
  function glControls() {
    const ctl = {
      onAdd() {
        const d = document.createElement("div");
        d.className = "maplibregl-ctrl gl-ctrls";
        d.innerHTML = `<div class="map-stack"><button type="button" data-a="style" aria-label="Map style">${IC.map}</button><button type="button" data-a="radar" class="${radarOn() ? "on" : ""}" aria-label="Weather radar">${IC.cloud}</button><button type="button" data-a="apts" class="only-home" aria-label="Airport status">${TOWER}</button></div>
          <button type="button" class="map-center" data-a="fit" aria-label="Center">${IC.locate}</button>
          <button type="button" class="map-center only-leg" data-a="dep" aria-label="Zoom to the departure airport">${IC.takeoff}</button>
          <button type="button" class="map-center only-leg" data-a="arr" aria-label="Zoom to the arrival airport">${IC.land}</button>
          <button type="button" class="map-center xpdr only-surf" data-a="xpdr" aria-label="Transponder filter">XPDR</button>
          <button type="button" class="map-center xpdr only-surf" data-a="ids" aria-label="Show callsigns">ID</button>`;
        const on = (a, f) => { d.querySelector(`[data-a="${a}"]`).onclick = (e) => { e.preventDefault(); f(); }; };
        on("style", () => { const L = STYLES(); setPref("ft-mapstyle", L[(L.indexOf(mapStyle()) + 1) % L.length]); restyleAll(); });
        on("radar", () => {
          const v = !radarOn();
          setPref("ft-radar", v ? "1" : "0");
          d.querySelector('[data-a="radar"]').classList.toggle("on", v);
          glRadar(v);
        });
        on("apts", () => go("#/airports"));
        on("fit", () => glFit(true, true));
        for (const [a, side] of [["dep", "dep_ap"], ["arr", "arr_ap"]]) on(a, () => { const p = GL.leg && ll(GL.leg[side]); if (p && Math.abs(GL.map.getZoom() - 14) > 2.5) holdTiles(); if (p) GL.map.flyTo({ center: [p[1], p[0]], zoom: 14, duration: 1200, padding: mapPad(false) }); });
        on("xpdr", () => { setPref("ft-surf-all", pref("ft-surf-all", "0") === "1" ? "0" : "1"); if (GL.lastSurface) paintSurface(GL.lastSurface); });
        on("ids", () => { setPref("ft-surf-ids", pref("ft-surf-ids", "0") === "1" ? "0" : "1"); if (GL.lastSurface) paintSurface(GL.lastSurface); });
        GL.ctl = d;
        paintTower();
        return d;
      },
      onRemove() {},
    };
    GL.map.addControl(ctl, "top-right");
  }
  function glInit() {
    if (GL.map) return true;
    if (!window.maplibregl || !MAP_EL) return false;
    tcInit();
    const map = GL.map = new maplibregl.Map({ container: MAP_EL, style: glStyle(), center: [-85, 35], zoom: 1.6, attributionControl: false,
      dragRotate: false, pitchWithRotate: false, maxPitch: 0, fadeDuration: 120, maxTileCacheZoomLevels: 8,
      pixelRatio: mapPR() });
    map.touchZoomRotate.disableRotation();
    map.keyboard.disable();
    map.addControl(GL.attrib = new maplibregl.AttributionControl({ compact: false }), "top-left");
    map.on("styleimagemissing", (e) => {
      if (!e.id || !e.id.startsWith("x:") || map.hasImage(e.id)) return;
      try { map.addImage(e.id, drawImg(JSON.parse(e.id.slice(2))), { pixelRatio: IMG_R }); } catch {}
    });
    glControls();
    // every style load (the first, and after a Satellite/Dark switch): our layers, then the page's picture
    map.on("style.load", () => {
      glLayers();
      GL.ready = true;
      if (GL.paint) { const fit = GL.fitPending; GL.fitPending = false; GL.paint(GL.data, fit); }
      layoutCheck();
    });
    // labels by zoom (airport codes, route fixes, taxiways, gates), in the old map's zoom numbers
    const zc = () => { const z = map.getZoom() + 1; for (const n of [4, 8, 15, 16, 17]) MAP_EL.classList.toggle(`z${n}`, z >= n); };
    map.on("zoom", zc); zc();
    map.on("moveend", () => { if (camAnim) return; releaseTiles(); layoutCheck(); surfaceNow(); gAttr(); });
    map.on("move", holdStep);
    map.on("dragstart", releaseTiles); map.on("touchstart", () => { if (HOLD.on && !camAnim) { map.stop(); releaseTiles(); } });
    // The globe keeps its size as you drag north or south by lowering the zoom number (by
    // log2 cos(latitude)), and tiles are picked from that number: halfway across, the map switched
    // to the next coarser tiles, so the picture went soft and baked-in labels jumped bigger. Scaling
    // each source's tile size by cos(latitude) undoes that: the same tiles as long as the globe
    // looks the same size.
    const latFix = () => {
      if (!GL.ready) return;
      // (the flat map doesn't change its zoom number with latitude: nothing to undo)
      const c = flatMap() ? 1 : Math.max(0.25, Math.cos(map.getCenter().lat * Math.PI / 180));
      for (const id of ["under", "base", "lbl"]) {
        const src = map.getSource(id);
        if (!src) continue;
        if (!src._ftTs) src._ftTs = src.tileSize;
        src.tileSize = src._ftTs * c;
      }
    };
    map.on("move", latFix);
    map.on("moveend", () => { if (!camAnim) placeZoomFix(); });
    setTimeout(warmWorld, 8000);
    setTimeout(warmTrips, 20000);
    // back on screen (after swiping home, locking, another app): pick up where it stopped, a few
    // seconds in so the screen you return to loads first. Finished runs still wait their 6 hours.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) setTimeout(() => { if (!document.hidden) warmTrips(); }, 4000);
    });
    map.on("style.load", latFix);
    map.on("zoomend", () => { if (GL.lastSurface) paintSurface(GL.lastSurface); });
    // the night side moves: redraw it every 10 minutes
    setInterval(() => { const src = GL.ready && map.getSource("night"); if (src) src.updateImage({ url: nightImage(), coordinates: MERC }); }, 600000);
    return true;
  }
  // A page puts its picture on the map. A new page glides the camera to its routes; the same page
  // (a live update) only redraws, leaving the map where you put it.
  function showMap(view, paint, data, opts) {
    opts = opts || {};
    if (!glInit()) { GL.queued = [view, paint, data, opts]; return; }
    const changed = GL.view !== view;
    if (changed) { clearLayout(); stopSurface(); GL.leg = null; GL.globeView = null; }
    GL.view = view; GL.paint = paint; GL.data = data; GL.opts = opts;
    if (GL.ctl) GL.ctl.dataset.kind = opts.kind || "";
    const fit = changed || !!opts.refit;
    if (!GL.ready) { GL.fitPending = GL.fitPending || fit; return; }
    GL.map.resize();
    paint(data, fit);
  }
  // always framed for where the sheet actually is (Passport opens with it lower than My Flights)
  // A page draws its map before the router moves the sheet to that page's resting spot, so the
  // framing waits a frame and measures the sheet where it really is (like the center button).
  // A page draws its map before the sheet has glided to that page's resting spot (Passport rests
  // lower than My Flights), so the framing waits until the sheet stops moving, then frames once for
  // the room actually left above it (as the center button does): one movement, not two.
  const fitNow = (fit) => {
    if (!fit) return;
    cancelAnimationFrame(GL.fitRaf);
    const view = GL.view, t0 = performance.now();
    let last = null, still = 0;
    const tick = () => {
      if (GL.view !== view) return;
      const g = document.querySelector(".sheet-gap"), y = g ? Math.round(g.getBoundingClientRect().bottom) : 0;
      still = y === last ? still + 1 : 0; last = y;
      if (still >= 3 || performance.now() - t0 > 700) { GL.fitRaf = 0; glFit(!!GL.everFit, true); GL.everFit = true; return; }
      GL.fitRaf = requestAnimationFrame(tick);
    };
    GL.fitRaf = requestAnimationFrame(tick);
  };


  // ---------- what each page draws ----------
  // My Flights: every route in the next two weeks, airports, the airplanes in the air.
  function paintHome(data, fit) {
    GL.leg = null;
    const routes = [], aps = new Map(), bounds = [], seen = new Set(), marks = [];
    for (const l of data.legs) {
      const A = ll(l.dep_ap), B = ll(l.arr_ap);
      if (!A || !B) continue;
      const k = [l.dep, l.arr].sort().join("-");
      if (!seen.has(k)) { seen.add(k); routes.push(lineF(gcPoints(A, B, 40))); }
      aps.set(l.dep, A); aps.set(l.arr, B);
      bounds.push(...gcPoints(A, B, 8));
    }
    const f = data.focus;
    for (const l of data.legs) {
      if (!["airborne", "taxi_out"].includes(l.phase) || (f && f.id === l.id)) continue;
      const p = l.flight && l.flight.last_position, B = ll(l.arr_ap);
      if (p && p[0] != null) marks.push(planeMk([p[0], p[1]], B ? bearing([p[0], p[1]], B) : 0, null, true));
    }
    const track = [];
    let own = -1;
    if (f && ["airborne", "taxi_out", "landed"].includes(f.phase)) {
      const tr = ((f.flight || {}).track || []).filter((p) => p[0] != null);
      if (tr.length > 1) track.push(lineF(smooth(tr.map((p) => [p[0], p[1]])), { own: 1 }));
      let pos = null, hdg = 0;
      if (f.live) { pos = newestPos(f.live, tr); hdg = headingOf(f.live, tr); }
      else if (tr.length) { pos = tr[tr.length - 1]; hdg = headingOf(null, tr); }
      if (pos) { own = marks.length; marks.push(planeMk([pos[0], pos[1]], hdg, null, true)); GL.ownHdg = hdg; }
    }
    glSet({ routes, track, aps: [...aps.values()].map((p) => ptF(p)) });
    const mk = glMarkers(marks);
    GL.ownMarker = own >= 0 ? mkHandle(mk[own]) : null; GL.inbMarker = null;
    GL.bounds = bounds; GL.maxZoom = 6.5;
    fitNow(fit);
  }
  // Passport: everything you've flown; airport codes once you zoom in.
  function paintPass(data, fit) {
    GL.leg = null;
    GL.passFC = GL.passFC && GL.passFC.d === data ? GL.passFC : { d: data,
      routes: FC(data.routes.map(([a, b]) => lineF(gcPoints([a.lat, a.lon], [b.lat, b.lon], 48), { c: "#e8eeff", w: 1.4 }))),
      aps: FC(data.airports.map((a) => ptF([a.lat, a.lon], { r0: 2.6, s0: 1.2 }))) };
    glSet({ routes: GL.passFC.routes, aps: GL.passFC.aps });
    glMarkers(data.airports.map((a) => symF([a.lat, a.lon], ["code", a.code, 0, 6], "code", "left")));
    GL.ownMarker = GL.inbMarker = null;
    GL.bounds = data.airports.map((a) => [a.lat, a.lon]); GL.maxZoom = 4;
    if (fit) {
      let x = 0, y = 0, z = 0;
      for (const a of data.airports) {
        const la = a.lat * Math.PI / 180, lo = a.lon * Math.PI / 180;
        x += Math.cos(la) * Math.cos(lo); y += Math.cos(la) * Math.sin(lo); z += Math.sin(la);
      }
      GL.globeView = data.airports.length ? [Math.atan2(z, Math.hypot(x, y)) * 180 / Math.PI, Math.atan2(y, x) * 180 / Math.PI] : [30, -80];
      GL.globeZoom = 1.35;
      fitNow(true);
      GL.globeView = null;   // the locate button fits the routes afterwards
    }
  }
  // Airports: every airport the FAA lists now, red for programs and stops, amber for delays.
  function paintAirports(data, fit) {
    GL.leg = null;
    const red = css("--red"), amber = "#ffc53d", list = data.airports.filter((a) => a.lat != null);
    glSet({ pts: list.map((a) => ptF([a.lat, a.lon], { c: issueRank(a) < 2 ? red : amber })) });
    glMarkers(list.map((a) => symF([a.lat, a.lon], ["code", a.code, 0, 8], "lbl", "left")));
    GL.ownMarker = GL.inbMarker = null;
    GL.bounds = list.map((a) => [a.lat, a.lon]); GL.maxZoom = 5;
    if (!list.length) { GL.globeView = [38, -97]; GL.globeZoom = 2.6; }
    fitNow(fit);
    GL.globeView = null;
  }
  // A flight: the route ahead (ATC's when the FAA has it, else the filed one), the flown track, the
  // airplane, its inbound, and the airports. Zoomed in on an airport: its diagram and surface traffic.
  // ---------- warm-up: the first zoom into an airport is as smooth as the second ----------
  // The first time you zoom into an airport this session, three things happened mid-gesture: its
  // diagram was downloaded and built, every runway/taxiway/gate label was drawn as a picture, and
  // the close-up imagery was fetched. Now, a moment after a flight's map opens and while nothing is
  // moving, all three are done ahead of time in small slices: the diagram built, its labels drawn
  // into the map, and the imagery around each airport (zoom 12-15) put in the phone's tile store.
  const WARM = new Set();
  const idle = (fn) => (window.requestIdleCallback ? requestIdleCallback(fn, { timeout: 2000 }) : setTimeout(() => fn({ timeRemaining: () => 8 }), 200));
  function warmAirports(leg) {
    const aps = [leg.dep_ap, leg.arr_ap, ...[leg.inbound, ...(leg.chain || [])].filter(Boolean).flatMap((r) => [r.origin, r.destination])]
      .filter((a) => a && (a.iata || a.icao) && ll(a));
    const seen = new Set();
    for (const a of aps) {
      const code = a.iata || a.icao;
      if (seen.has(code)) continue;
      seen.add(code);
      if (WARM.has(code)) continue;
      WARM.add(code);
      setTimeout(() => idle(() => warmOne(code, ll(a))), 1500 + seen.size * 1200);
    }
  }
  async function warmOne(code, pos) {
    if (!LAYOUTS[code]) LAYOUTS[code] = api(`api/airport/${encodeURIComponent(code)}/layout`).then(buildLayout).catch(() => { delete LAYOUTS[code]; return null; });
    const L_ = await LAYOUTS[code];
    // label pictures, a few per idle slice
    const ids = L_ ? [...new Set(L_.labels.features.map((f) => f.properties.img))] : [];
    const drawSome = (dl) => {
      const map = GL.map;
      if (!map || !GL.ready) return;
      while (ids.length && (dl.timeRemaining() > 2 || ids.length % 8)) {
        const id = ids.pop();
        if (!map.hasImage(id)) { try { map.addImage(id, drawImg(JSON.parse(id.slice(2))), { pixelRatio: IMG_R }); } catch {} }
      }
      if (ids.length) idle(drawSome);
    };
    if (ids.length) idle(drawSome);
    // close-up imagery around the airport into the tile store (Esri, kept on the phone)
    if (!TC_ON || lowData() || mapStyle() !== "satellite") return;
    try {
      const c = await caches.open(TC_NAME), [la, lo] = pos;
      for (let z = 12; z <= 15; z++) {
        const n = 2 ** z, x0 = Math.floor((lo + 180) / 360 * n), R = la * Math.PI / 180;
        const y0 = Math.floor((1 - Math.log(Math.tan(R) + 1 / Math.cos(R)) / Math.PI) / 2 * n), r = z >= 14 ? 2 : 1;
        for (let x = x0 - r; x <= x0 + r; x++) for (let y = y0 - r; y <= y0 + r; y++) {
          const u = ESRI_IMG.replace("{z}", z).replace("{x}", x).replace("{y}", y);
          if (await c.match(u)) continue;
          try { const res = await fetch(u); if (res.ok) await c.put(u, res); } catch {}
          await new Promise((ok) => setTimeout(ok, 30));   // gently, behind everything else
        }
      }
    } catch {}
  }
  function paintLeg(leg, fit) {
    GL.leg = leg;
    setTimeout(() => idle(layoutCheck), 400);   // its airports' diagrams, ready before you zoom in
    warmAirports(leg);
    const route = css("--route"), violet = css("--violet"), muted = "#9aa3b3";
    const plan = [], track = [], aps = [], fixes = [], marks = [], bounds = [];
    const A = ll(leg.dep_ap), B = ll(leg.arr_ap);
    const f = leg.flight || {};
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
      plan.push(lineF(ahead, { c: route, o: atc ? 0.8 : 0.6 }));
      wps.forEach((p) => bounds.push(p));
      if (atc) for (const [la, lo, name] of (f.swim_route.fixes || []).slice(1, -1)) {
        fixes.push(ptF([la, lo]));
        marks.push(symF([la, lo], ["fix", name, 0, 3], "fix", "left"));
      }
    } else if (A && B) {
      const g = gcPoints(A, B);
      plan.push(lineF(g, { c: muted, o: 0.8 }));
      g.forEach((p) => bounds.push(p));
    }
    const tr = (f.track || []).filter((p) => p[0] != null);
    if (tr.length > 1) {
      const sm = smooth(tr.map((p) => [p[0], p[1]]));
      // the live position is fresher than the drawn track: join them so it sits on its line
      if (leg.live && ["airborne", "taxi_out", "landed"].includes(leg.phase)) sm.push([leg.live.lat, leg.live.lon]);
      track.push(lineF(sm, { own: 1 }));
    }
    // The inbound only frames the map while it still matters (not yet arrived, your flight not yet off the gate).
    const inb = leg.inbound;
    const inbOpen = !!inb && !["arrived", "cancelled", "diverted"].includes(inb.phase) && leg.phase === "scheduled";
    let inbIdx = -1;
    if (inb) {
      const ia = ll(inb.origin), itr = (inb.track || []).filter((p) => p[0] != null);
      if (itr.length > 1) {
        const pts = smooth(itr.map((p) => [p[0], p[1]]));
        if (leg.inbound_live) pts.push([leg.inbound_live.lat, leg.inbound_live.lon]);
        track.push(lineF(pts, { c: violet, w: 2.5, o: inbOpen ? 0.9 : 0.35 }));
      }
      const iw = (inb.waypoints || []).filter((p) => p[0] != null), iPlan = !["landed", "arrived", "cancelled", "diverted"].includes(inb.phase);
      if (iPlan && iw.length > 1) plan.push(lineF(iw, { c: violet, o: 0.45 }));
      else if (iPlan && ia && A) plan.push(lineF(gcPoints(ia, A), { c: violet, o: 0.45 }));
      let ip = null, ih = 0;
      if (leg.inbound_live) { ip = newestPos(leg.inbound_live, itr); ih = headingOf(leg.inbound_live, itr); }
      else if (inb.phase === "airborne" && itr.length) { ip = itr[itr.length - 1]; ih = headingOf(null, itr); }
      if (ip) { inbIdx = marks.length; marks.push(planeMk([ip[0], ip[1]], ih, violet)); GL.inbHdg = ih; if (inbOpen) bounds.push([ip[0], ip[1]]); }
      if (ia && inbOpen) { bounds.push(ia); marks.push(symF(ia, ["pill", (inb.origin || {}).iata || "", violet, 8], "lbl", "left")); }
    }
    for (const [ap, pos] of [[leg.dep_ap, A], [leg.arr_ap, B]]) {
      if (!pos) continue;
      bounds.push(pos);
      aps.push(ptF(pos));
      marks.push(symF(pos, ["city", cityOf(ap) || "", ap.iata || "", 9], "lbl", "left"));
    }
    let pos = null, hdg = 0, own = -1;
    if (leg.live) { pos = newestPos(leg.live, tr); hdg = headingOf(leg.live, tr); }
    else if (["airborne", "taxi_out", "landed"].includes(leg.phase) && tr.length) { pos = tr[tr.length - 1]; hdg = headingOf(null, tr); }
    else if (f.last_position && ["airborne", "taxi_out"].includes(leg.phase)) pos = f.last_position;
    // on the airport's surface radar, that (more exact) target is your airplane: don't draw it twice
    if (pos && !(GL.surfaceMine && Date.now() - GL.surfaceMine < 10000)) { own = marks.length; marks.push(planeMk([pos[0], pos[1]], hdg, "#fff")); }
    GL.ownHdg = hdg;
    glSet({ plan, track, aps, fixes });
    const mk = glMarkers(marks);
    GL.ownMarker = own >= 0 ? mkHandle(mk[own]) : null;
    GL.inbMarker = inbIdx >= 0 ? mkHandle(mk[inbIdx]) : null;
    GL.bounds = bounds; GL.maxZoom = 8;
    fitNow(fit);
  }
  // A ground stop or delay program: the centers whose departures it holds, filled; the program's
  // airport; and where your flight (or its inbound) departs, red if held, green if not.
  function paintNas(d, fit) {
    GL.leg = null;
    glSet({ pts: d.pts, artcc: d.artcc || null });
    glMarkers(d.marks.concat(d.ctr || []));
    GL.ownMarker = GL.inbMarker = null;
    GL.bounds = d.bounds; GL.maxZoom = 6;
    fitNow(fit);
  }

  // ---------- airport diagrams (zoomed in on one of the flight's airports) ----------
  // OpenStreetMap data, fetched once per airport by the add-on. Runways and terminals from zoom 13,
  // taxiways 14, gates 15.
  const LAYOUTS = {};
  const LOG_ALL = new Set();   // flights whose whole lookup log you asked to see
  const TOWER = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M8 3h8l1.5 4.5H6.5zm-2.2 5.5h12.4l-1.4 4H7.2zM9 13.5h6l.6 7.5H8.4zM11 1h2v2h-2z"/></svg>';
  function clearLayout() {
    if (!GL.map) return;
    if (GL.ready) for (const id of LAY) { const s = GL.map.getSource(id); if (s) s.setData(FC()); }
    if (GL.ready) { const s_ = GL.map.getSource("laysym"); if (s_) s_.setData(FC()); }
    GL.layoutFor = null; GL.layoutShown = false;
  }
  // Both of the flight's airports are put on the map as soon as its page opens (the layers only
  // draw from zoom 12.5), so zooming in finds the diagram already there instead of loading it once
  // the camera stops.
  async function layoutCheck() {
    const map = GL.map, leg = GL.leg;
    if (!map || !GL.ready || !leg) return;
    const codes = [...new Set([leg.dep_ap, leg.arr_ap].filter((a) => a && ll(a)).map((a) => a.iata || a.icao).filter(Boolean))];
    if (!codes.length) return;
    const key = codes.join("+");
    if (GL.layoutFor === key && GL.layoutShown) return;
    GL.layoutFor = key; GL.layoutShown = false;
    for (const code of codes) if (!LAYOUTS[code]) {
      LAYOUTS[code] = api(`api/airport/${encodeURIComponent(code)}/layout`).then(buildLayout).catch(() => { delete LAYOUTS[code]; return null; });
    }
    const Ls = (await Promise.all(codes.map((c) => LAYOUTS[c]))).filter(Boolean);
    if (!Ls.length || GL.leg !== leg || GL.layoutFor !== key || !GL.ready) { if (GL.layoutFor === key) GL.layoutFor = null; return; }
    const merge = (id) => FC(Ls.flatMap((L_) => L_[id].features));
    for (const id of LAY) { const s_ = map.getSource(id); if (s_) s_.setData(merge(id)); }
    const s2 = map.getSource("laysym"); if (s2) s2.setData(merge("labels"));
    GL.layoutShown = true;
  }
  function buildLayout(d) {
    if (!d || !d.features) return null;
    const term = [], rwy = [], twy = [], labels = [], placed = {};
    const mid = (pts) => {
      let tot = 0; const seg = [];
      for (let i = 1; i < pts.length; i++) { const k = kmBetween(pts[i - 1], pts[i]); seg.push(k); tot += k; }
      let half = tot / 2;
      for (let i = 0; i < seg.length; i++) {
        if (half <= seg[i]) { const t = seg[i] ? half / seg[i] : 0; return [[pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t], tot]; }
        half -= seg[i];
      }
      return [pts[0], tot];
    };
    const centroid = (pts) => [pts.reduce((a, p) => a + p[0], 0) / pts.length, pts.reduce((a, p) => a + p[1], 0) / pts.length];
    const near = (ref, p, km) => (placed[ref] || []).some((q) => kmBetween(p, q) < km);
    const KIND = { "ap-term": ["term", "a"], "ap-rwy": ["rwy", "a"], "ap-twy": ["twy", "t"], "ap-gate": ["gate", "g"] };
    const lbl = (pos, text, cls) => labels.push(symF(pos, [KIND[cls][0], String(text)], KIND[cls][1]));
    for (const f of d.features) {
      if (f.k === "terminal" && Array.isArray(f.g[0])) {
        term.push({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [ring(f.g)] } });
        if (f.name || f.ref) lbl(centroid(f.g), f.name || f.ref, "ap-term");
      } else if (f.k === "runway" && Array.isArray(f.g[0])) {
        rwy.push(lineF(f.g));
        if (f.ref) lbl(mid(f.g)[0], f.ref.replace(/^0/, "").replace(/\/0/, "/"), "ap-rwy");
      } else if (f.k === "taxiway" && Array.isArray(f.g[0])) {
        twy.push(lineF(f.g));
        if (!f.ref) continue;
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
    return { "lay-term": FC(term), "lay-rwy": FC(rwy), "lay-twy": FC(twy), labels: FC(labels) };
  }

  // ---------- live position (every 5 s while the flight or its inbound is moving) ----------
  // Only the new position and track points are fetched; the map repaints in place. Between fixes
  // the airplane is carried forward along its heading. A request that hangs is dropped after 8 s.
  const LIVEW = { id: null, key: null, timer: null };
  function liveWatch(leg, key) {
    key = key || `leg-${leg.id}`;
    const moving = ["taxi_out", "airborne", "landed"].includes(leg.phase) ||
      (leg.inbound && ["taxi_out", "airborne"].includes(leg.inbound.phase));
    if (!moving) { if (LIVEW.key === key) { clearTimeout(LIVEW.timer); LIVEW.id = LIVEW.key = LIVEW.timer = null; } return; }
    if (LIVEW.key === key && LIVEW.timer) return;
    if (LIVEW.timer) clearTimeout(LIVEW.timer);
    LIVEW.id = leg.id; LIVEW.key = key;
    const id = leg.id;
    const tick = async () => {
      LIVEW.timer = null;
      if (LIVEW.key !== key || GL.view !== key) { if (LIVEW.key === key) LIVEW.id = LIVEW.key = null; return; }
      const cur = LEGS[id];
      if (cur && !document.hidden) {
        const tr = (cur.flight && cur.flight.track) || [], itr = (cur.inbound && cur.inbound.track) || [];
        const since = Math.min(tr.length ? tr[tr.length - 1][4] || 0 : 0, itr.length ? itr[itr.length - 1][4] || 0 : Infinity);
        const ctl = window.AbortController ? new AbortController() : null;
        const stop = ctl && setTimeout(() => ctl.abort(), 8000);
        try {
          const d = await api(`api/live/${encodeURIComponent(id)}?since=${Math.floor(since || 0)}`, ctl ? { signal: ctl.signal } : undefined);
          let moved = false;
          if (d.live && (!cur.live || d.live.at > cur.live.at)) { cur.live = d.live; moved = true; }
          if (d.inbound_live && (!cur.inbound_live || d.inbound_live.at > cur.inbound_live.at)) { cur.inbound_live = d.inbound_live; moved = true; }
          const add = (arr, pts) => { const last = arr.length ? arr[arr.length - 1][4] || 0 : 0; for (const p of pts || []) if ((p[4] || 0) > last) { arr.push(p); moved = true; } };
          if (cur.flight) { cur.flight.track = cur.flight.track || []; add(cur.flight.track, d.track); }
          if (cur.inbound) { cur.inbound.track = cur.inbound.track || []; add(cur.inbound.track, d.inbound_track); }
          if (moved && GL.view === key) GL.paint(key === "home" ? { ...GL.data, focus: withChain(cur) } : withChain(cur), false);
          const te = document.getElementById("telem");
          if (te && cur.live && key === `leg-${id}`) te.innerHTML = telem(cur.live);
          const fresh = (lv, trk) => { const l = (trk || []).filter((p) => p && p[0] != null).pop(); return !(l && (l[4] || 0) > (lv.at || 0) + 5); };
          if (GL.ownMarker && cur.live && cur.phase === "airborne" && fresh(cur.live, cur.flight && cur.flight.track)) { const a = aheadOf(cur.live, GL.ownHdg || 0, Date.now()); if (a) GL.ownMarker.setLatLng(a); }
          if (GL.inbMarker && cur.inbound_live && cur.inbound && cur.inbound.phase === "airborne" && fresh(cur.inbound_live, cur.inbound.track)) { const a = aheadOf(cur.inbound_live, GL.inbHdg || 0, Date.now()); if (a) GL.inbMarker.setLatLng(a); }
        } catch {} finally { if (stop) clearTimeout(stop); }
      }
      LIVEW.timer = setTimeout(tick, lowData() ? 15000 : 5000);
    };
    LIVEW.timer = setTimeout(tick, lowData() ? 15000 : 5000);
  }

  // ---------- live surface traffic (FAA STDDS) ----------
  // While the flight's page is open and it's on the ground at an airport with surface radar:
  // everything on the field, your flight highlighted. Every 3 s while there's traffic, else 20 s.
  const SURF = { id: null, timer: null, zoomed: false };
  function stopSurface() {
    if (SURF.timer) clearTimeout(SURF.timer);
    SURF.timer = null; SURF.id = null;
    const ss = GL.ready && GL.map.getSource("surf"); if (ss) ss.setData(FC());
    GL.lastSurface = null; GL.surfaceMine = 0;
    if (GL.ctl) GL.ctl.classList.remove("has-surf");
  }
  // o (an inbound leg's page): {key, lid, route, view, ident, ap()} — the airport to watch when
  // you're not zoomed in on one (where that airplane is on the ground), and whose flight to highlight
  function surfaceWatch(leg, o) {
    if (!STATE || !STATE.stdds || STATE.stdds.status !== "connected") { stopSurface(); return; }
    o = o || {};
    const id = o.key || leg.id, lid = o.lid || leg.id, route = o.route || `#/leg/${lid}`, view = o.view || `leg-${lid}`;
    if (SURF.id !== id) { stopSurface(); SURF.id = id; SURF.zoomed = false; }
    if (SURF.timer) return;
    const tick = async () => {
      SURF.timer = null;
      if (SURF.id !== id || ROUTE.split("?")[0] !== route && !(route.startsWith("#/leg/") && ROUTE.startsWith(route))) { if (SURF.id === id) stopSurface(); return; }
      let d = null;
      const ap = zoomedAirport(12) || (o.ap ? o.ap() : null);
      const q = [ap && `ap=${encodeURIComponent(ap)}`, o.ident && `ident=${encodeURIComponent(o.ident)}`].filter(Boolean).join("&");
      if (o.key && !ap) d = { targets: [] };   // an inbound leg in the air: nothing on the ground to show
      else try { d = await api(`api/surface/${encodeURIComponent(lid)}${q ? `?${q}` : ""}`); } catch {}
      if (SURF.id !== id) return;
      if (GL.view === view) paintSurface(d || {});
      SURF.timer = setTimeout(tick, d && d.targets && d.targets.length ? 3000 : 20000);
    };
    SURF.timer = setTimeout(tick, 0);
  }
  // The flight airport the map is zoomed in on (center within 8 km, at least zoom z), or null.
  function zoomedAirport(z) {
    const leg = GL.leg;
    if (!GL.map || !leg || GL.map.getZoom() < z) return null;
    const c = GL.map.getCenter(), here = [c.lat, c.lng];
    // this flight's airports, and the airports of the inbound legs drawn with it
    const more = [leg.inbound, ...(leg.chain || []), ...(leg.rotation || [])].filter(Boolean).flatMap((r) => [r.origin, r.destination]);
    const a = [leg.dep_ap, leg.arr_ap, ...more].find((a) => a && ll(a) && kmBetween(here, ll(a)) < 8);
    return a ? a.iata || a.icao : null;
  }
  // zoomed in on an airport: look for surface traffic now rather than at the next 20 s poll
  function surfaceNow() {
    const ap = zoomedAirport(12);
    if (!SURF.id || !GL.leg || GL.leg.id !== SURF.id || !ap || GL.surfAp === ap) return;
    GL.surfAp = ap;
    if (SURF.timer) clearTimeout(SURF.timer);
    SURF.timer = null;
    const l = GL.leg;
    SURF.id = null;
    surfaceWatch(l, l._surf);
  }
  function paintSurface(d) {
    const only = pref("ft-surf-all", "0") !== "1", ids = pref("ft-surf-ids", "0") === "1";
    const all = (d.targets || []).filter((t) => t.lat != null && t.lon != null);
    const tg = only ? all.filter((t) => t.mine || (t.xpdr && t.kind !== "vehicle")) : all;
    if (GL.ctl) {
      GL.ctl.classList.toggle("has-surf", all.length > 0);
      const xb = GL.ctl.querySelector('[data-a="xpdr"]'), ib = GL.ctl.querySelector('[data-a="ids"]');
      xb.classList.toggle("on", only);
      xb.title = only ? "Showing aircraft with transponders on (tap for everything)" : "Showing everything on the surface (tap for transponders only)";
      ib.classList.toggle("on", ids);
    }
    GL.lastSurface = d;
    const center = all.length && d.center ? d.center : null;
    if (!SURF.zoomed && center && ["taxi_out", "landed"].includes(d.phase)) {
      const me = all.find((t) => t.mine);
      if (Math.abs(GL.map.getZoom() - 14) > 2.5) holdTiles();
      GL.map.easeTo({ center: me ? [me.lon, me.lat] : [center[1], center[0]], zoom: 14, duration: 900, padding: mapPad(false) });
      SURF.zoomed = true;
    }
    // zoomed out past the airport, surface traffic is clutter over the route
    if (GL.map.getZoom() < 11) { GL.surfaceMine = 0; if (GL.ownMarker) GL.ownMarker.setOpacity(1); const s0 = GL.map.getSource("surf"); if (s0) s0.setData(FC()); return; }
    const mine = tg.some((t) => t.mine);
    GL.surfaceMine = mine ? Date.now() : 0;
    if (GL.ownMarker) GL.ownMarker.setOpacity(mine ? 0 : 1);
    const route = css("--route"), list = [];
    const surfSrc = GL.map.getSource("surf");
    for (const t of tg) {
      const label = [t.cs, t.type].filter(Boolean).join(" · ") || (t.kind === "vehicle" ? "Vehicle" : "Unidentified");
      if (!t.mine && (t.kind === "vehicle" || (!t.cs && !t.type))) { list.push({ type: "Feature", properties: { L: "dot" }, geometry: { type: "Point", coordinates: [t.lon, t.lat] } }); continue; }
      const sz = t.mine ? 22 : 15;
      list.push(planeMk([t.lat, t.lon], t.hdg || 0, t.mine ? route : "rgba(255,255,255,.88)", false, sz));
      const tag = t.mine ? label : ids && (t.cs || t.squawk) ? t.cs || t.squawk : "";
      if (tag) list.push(symF([t.lat, t.lon], ["stag", tag, t.mine ? "#2f6fe8" : 0, sz / 2 + 4], "tag", "left"));
    }
    if (surfSrc) surfSrc.setData(FC(list));
  }

  // ---------- views ----------
  function homeView() {
    const sortKey = (l) => (l.flight && best(l.flight.times, "out")) || l.sched_out;
    const all = [...STATE.legs].sort((a, b) => sortKey(a) - sortKey(b));
    const live = all.filter((l) => !l.finished);
    const moving = (l) => ["taxi_out", "airborne", "landed"].includes(l.phase);
    const focus = live.find(moving) || live[0];
    // NAS for airports on any flight in the watch window; programs go up top, minor delays below.
    const watching = live.filter((l) => l.watching);
    const myAirports = new Set(watching.flatMap((l) => [l.dep, l.arr]).filter(Boolean));
    const nowS = Date.now() / 1000;
    let list = "";
    live.slice(0, 40).forEach((l, i) => { if (i) list += layover(live[i - 1], l); list += row(l); });
    let h = `<div class="sheet-gap"></div><section class="sheet">
      <div class="sheet-head"><h1>My Flights<a class="hstat" href="#/tools" aria-label="Live updates and Drive backup"><i class="ld"></i>${IC.cloud}</a></h1>
        <div class="sh-btns"><a class="round" href="#/add" aria-label="Add a flight">${IC.plus}</a>${avatar()}</div></div>`;
    h += nasTop(watching);
    GL.tower = towerLevel(watching);
    if (STATE.schedule && STATE.schedule.error) h += `<div class="banner critical"><span class="dot critical"></span><div><b>Schedule</b><br>${esc(STATE.schedule.error)}</div></div>`;
    h += live.length ? `<div class="flist">${list}</div>`
      : `<div class="card empty"><b>No upcoming flights</b>Nothing coming up in your calendar${STATE.schedule && STATE.schedule.count === 0 ? " (no flights found in your schedule)" : ""}. Tap + to add a flight.</div>`;
    if (myAirports.size) {
      const minor = nasMinor(watching), major = nasGroups(watching).length - minor.length;  // major = shown up top
      h += `<h2 class="section"><span>FAA Status</span><span class="small" style="color:var(--muted)">${esc([...myAirports].join(" · "))}${STATE.nas_at ? ` · ${ago(STATE.nas_at)}` : ""}</span></h2>`;
      h += minor.length ? nasCard(minor) : `<div class="card small muted">${major ? "Shown at the top." : "No ground stops, delay programs or closures at your airports."}${STATE.nas_error ? `<div class="err">${esc(STATE.nas_error)}</div>` : ""}</div>`;
    }
    // Short feeds here; everything older lives in Passport and Alerts.
    const done = all.filter((l) => l.finished && nowS - (best(l.flight && l.flight.times, "in") || l.sched_in) < 18 * 3600)
      .sort((a, b) => sortKey(b) - sortKey(a)).slice(0, 3);
    if (done.length) h += `<h2 class="section"><span>Recent</span><a href="#/history">Passport ›</a></h2><div class="flist">${done.map(row).join("")}</div>`;
    const fresh = STATE.alerts.filter((a) => nowS - a.at < 12 * 3600).slice(0, 5);
    if (fresh.length) h += `<h2 class="section"><span>Latest Alerts</span><a href="#/history/alerts">All ›</a></h2>` + alertsCard(fresh);
    h += `<p class="small faint" style="margin:22px 4px">v${esc(STATE.version)} · alerts go to ${esc(STATE.notify_target)} · watching ${STATE.settings.watch_hours} h before departure${STATE.schedule && STATE.schedule.checked ? ` · schedule read ${ago(STATE.schedule.checked)}` : ""}</p></section>`;
    destroyMaps();
    $app.innerHTML = h;
    // the map: every route in the next two weeks, and the airplane you're on (or about to be)
    const data = (full) => ({ legs: live, focus: full ? withChain(full) : null });
    showMap("home", paintHome, data(focus && LEGS[focus.id]), { kind: "home" });
    paintTower();
    if (focus && moving(focus)) {
      api(`api/leg/${focus.id}`).then((full) => {
        LEGS[focus.id] = full;
        if (GL.view === "home") { showMap("home", paintHome, data(full), { kind: "home" }); liveWatch(full, "home"); }
      }).catch(() => {});
    }
  }

  let APTS = null;   // last airport-status answer, for the program map
  const issueRank = (a) => a.items.some((n) => ["gs", "closure"].includes(n.kind)) ? 0 : a.items.some((n) => n.kind === "gdp") ? 1 : 2;
  function airportsView() {
    return swr("api/airports/status", drawAirports).catch((e) => drawAirports({ airports: [], error: e.message }));
  }
  function drawAirports(d) {
    APTS = d;
    const list = [...d.airports].sort((a, b) => issueRank(a) - issueRank(b) || (b.mine - a.mine) || a.code.localeCompare(b.code));
    const major = list.filter((a) => issueRank(a) < 2), minor = list.filter((a) => issueRank(a) === 2);
    const item = (a, n) => {
      const when = n.end ? `${n.start && n.start > Date.now() / 1000 ? hm(n.start, a.tz) + "–" : "until "}${hm(n.end, a.tz)}` : "";
      const sub = [n.detail, n.reason && n.reason[0].toUpperCase() + n.reason.slice(1), when].filter(Boolean).join(" · ");
      const tap = ["gs", "gdp"].includes(n.kind) && (n.scope || []).length;
      return `<${tap ? `a href="#/nas/${encodeURIComponent(n.key)}"` : "div"} class="ap-item k-${esc(n.kind)}"><b>${esc(n.label)}${tap ? ` ${IC.chev}` : ""}</b>${sub ? `<span>${esc(sub)}</span>` : ""}</${tap ? "a" : "div"}>`;
    };
    const row = (a) => `<div class="ap-row">
      <div class="ap-dot ${issueRank(a) < 2 ? "red" : "amber"}"><i></i><span>${esc(a.code)}</span></div>
      <div class="ap-main"><div class="ap-name">${esc(apShort(a, a.code))}${a.mine ? ' <span class="tag">YOUR FLIGHT</span>' : ""}</div>${a.items.map((n) => item(a, n)).join("")}</div></div>`;
    $app.innerHTML = `<div class="sheet-gap"></div><section class="sheet">
      <div class="sheet-head"><h1>Airports</h1><div class="sh-btns"><a class="round back xbtn" href="#/" aria-label="Close">${IC.x}</a></div></div>
      <p class="small muted" style="margin:-4px 0 6px">FAA status for every US airport with a ground stop, delay program, closure or delay trend${d.at ? ` · updated ${ago(d.at)}` : ""}.</p>
      ${d.error ? `<div class="err">${esc(d.error)}</div>` : ""}
      ${major.length ? `<h2 class="ap-h">Major Issues</h2>${major.map(row).join("")}` : ""}
      ${minor.length ? `<h2 class="ap-h">Minor Issues</h2>${minor.map(row).join("")}` : ""}
      ${!list.length ? `<div class="card empty"><b>All clear</b>No FAA ground stops, delay programs, closures or delay trends right now.</div>` : ""}
    </section>`;
    showMap("apts", paintAirports, { airports: list });
  }

  // A ground stop or delay program on a map: the centers whose departures it holds, filled; the
  // program airport; and where your flight (or its inbound) departs, red if it's held, green if not.
  let ARTCC = null;
  const NAS_FC = {};   // each program's center shapes, built once
  const CTR_LABEL_AT = { ZNY: [41.05, -76.45], ZOA: [39.2, -120.6], ZLA: [34.8, -116.8], ZSE: [45.6, -120.4], ZMA: [26.8, -81.0],
    ZJX: [31.2, -82.6], ZBW: [43.6, -71.8], ZHU: [30.6, -93.8], ZDC: [37.6, -77.8] };
  async function nasView(key) {
    const legs = (STATE.legs || []).filter((l) => (l.nas || []).some((n) => n.key === key));
    let g = nasGroups(legs).find((x) => x.n.key === key);
    if (!g) {
      const n0 = (STATE.nas || []).find((n) => n.key === key);
      const ap0 = APTS && APTS.airports.find((a) => n0 && a.code === n0.airport);
      if (n0) g = { n: n0, tz: ap0 ? ap0.tz : undefined, uses: [] };
    }
    destroyMaps();
    if (!g) { $app.innerHTML = `<div class="sheet-gap"></div><section class="sheet">${sheetHead("Program ended", "FAA", "#/")}<div class="card empty"><b>Program ended</b>It's no longer in the FAA's list.</div></section>`; return; }
    const n = g.n;
    $app.innerHTML = `<div class="sheet-gap d"></div>
      <section class="sheet">${sheetHead(esc(`${n.airport} ${n.label}`), "FAA", "#/")}
      <p class="small faint" id="nas-map-note" style="margin:6px 4px 10px" hidden></p>
      ${nasAlert(g).replace("tappable", "")}
      <div class="card small muted">Shaded: the en-route centers whose departures this ${n.kind === "gs" ? "ground stop" : "program"} holds. ${(n.scope || []).filter((c) => !/^Z[A-Z]{2}$/.test(c)).length ? `Also: ${esc(n.scope.filter((c) => !/^Z[A-Z]{2}$/.test(c)).join(" "))}.` : ""}</div></section>`;
    const hot = n.kind === "gs" ? css("--red") : css("--amber");
    const ok = css("--green"), muted = "#8e8e93";
    const bounds = [], pts = [], marks = [];
    const apPos = {};
    for (const l of legs) for (const a of [l.dep_ap, l.arr_ap]) if (a && ll(a)) apPos[a.iata] = ll(a);
    for (const a of (APTS && APTS.airports) || []) if (a.lat != null && !apPos[a.code]) apPos[a.code] = [a.lat, a.lon];
    const prog = apPos[n.airport];
    if (prog) {
      pts.push(ptF(prog, { c: hot, r: 6.5, s: 2 }));
      marks.push(symF(prog, ["pill", n.airport, hot, 10], "lbl", "left"));
      bounds.push(prog);
    }
    for (const u of g.uses) for (const i of u.impact) {
      const p = apPos[i.from] || (u.leg.inbound && u.leg.inbound.origin && ll(u.leg.inbound.origin));
      if (!p) continue;
      const c = i.airborne ? muted : i.hit === true ? hot : i.hit === false ? ok : muted;
      pts.push(ptF(p, { c, r: 5, s: 0 }));
      if (i.from !== n.airport) marks.push(symF(p, ["pill", i.from, c, 9], "lbl", "left"));
      bounds.push(p);
    }
    const view = `nas-${key}`, base = { pts, marks, bounds };
    showMap(view, paintNas, NAS_FC[key] ? { ...base, ...NAS_FC[key], bounds: bounds.concat(NAS_FC[key].bounds) } : base, { kind: "nas" });
    if (NAS_FC[key]) return;
    try { ARTCC = ARTCC || await api("api/artcc"); } catch { ARTCC = null; }
    if (!ARTCC) {
      const note = document.getElementById("nas-map-note");
      if (note) { note.hidden = false; note.textContent = "Center boundaries aren't available right now; the scope is listed below."; }
      return;
    }
    const scope = new Set(n.scope || []), feats = [], ctr = [], sb = [];
    for (const [id, c] of Object.entries(ARTCC)) {
      const inS = scope.has(id);
      // all of a center's shapes (low and high altitude) as one area
      feats.push({ type: "Feature", properties: { c: inS ? hot : muted, o: inS ? 0.22 : 0, w: inS ? 1.5 : 0.8, lo: inS ? 0.9 : 0.5 },
        geometry: { type: "MultiPolygon", coordinates: c.rings.map((r) => [ring(r)]) } });
      const big = c.rings.slice().sort((a, b) => b.length - a.length)[0];
      if (big && big.length) {
        // centers with a lot of ocean get their label over land, where it reads as theirs
        let lat = big.reduce((a, p) => a + p[0], 0) / big.length, lon = big.reduce((a, p) => a + p[1], 0) / big.length;
        if (CTR_LABEL_AT[id]) [lat, lon] = CTR_LABEL_AT[id];
        ctr.push(symF([lat, lon], ["ctr", id, inS ? hot : 0], "lbl"));
        if (inS) sb.push([lat, lon]);
      }
    }
    NAS_FC[key] = { artcc: FC(feats), ctr, bounds: sb };
    if (GL.view === view) showMap(view, paintNas, { ...base, ...NAS_FC[key], bounds: bounds.concat(sb) }, { kind: "nas", refit: true });
  }

  async function legView(id) {
    const route0 = ROUTE;
    const quick = !morphing && (LEGS[id] || cacheGet(`api/leg/${id}`) || ((STATE && STATE.legs) || []).find((l) => l.id === id));
    if (quick) drawLeg(id, quick);
    let leg;
    try { leg = await api(`api/leg/${id}${LOG_ALL.has(id) ? "?log=1" : ""}`); }
    catch (e) {
      if (!quick) $app.innerHTML = `<div class="sheet-gap"></div><section class="sheet">${sheetHead("Flight not found", "", "#/")}<div class="card empty"><b>Couldn't load this flight</b>${esc(e.message)}. It may have dropped off your schedule.</div></section>`;
      return;
    }
    LEGS[id] = leg;
    cachePut(`api/leg/${id}`, slimLeg(leg));
    if (ROUTE !== route0) return;
    const was = morphing;
    morphing = morphing || !!quick;
    try { drawLeg(id, leg); } finally { morphing = was; }
  }
  function drawLeg(id, leg) {
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
      // One route row: the clearance's route once the tower has sent it; else ATC's; else the filed
      // one, unless the FAA's own expected path (the dashed line) doesn't go through the filed route's
      // fixes. Then the FAA's is the current plan (UA1102 MIA-EWR: United's flight plan matched the
      // FAA's inland path while FlightAware still showed an older filing). Airways (Q87) and
      // procedures (FATHE4) are left out of the comparison: the FAA lists every fix along them.
      ...(() => {
        if (sw.pdc && sw.pdc.route) return [["Route (cleared)", sw.pdc.route + (sw.pdc.at ? ` · ${hm(Date.parse(sw.pdc.at) / 1000, tzA)}` : "")]];
        const main = [f.route_source === "FAA" ? "Route (ATC)" : "Filed route", f.route ? f.route + (sw.route_changed ? ` · amended ${hm(sw.route_changed, tzA)}` : "") : ""];
        const fx = ((f.swim_route || {}).fixes || []).map((x) => x[2]).filter(Boolean);
        if (!fx.length || !f.route || f.route_source === "FAA") return [main];
        const have = new Set(fx);
        const named = (f.route || "").toUpperCase().split(/[\s.]+/).filter((w) => /^[A-Z]{2,5}$/.test(w) && w !== "DCT");
        const found = named.filter((w) => have.has(w)).length;
        if (named.length >= 2 && found / named.length < 0.5)
          return [["Route (FAA)", fx.join(" ")], ["FlightAware shows", `${f.route} · an older filing`]];
        return [main];
      })(),
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
      ["EDCT", sw.edct && !sw.edct.at ? "assigned (per the departure tower; time not received yet)" : sw.edct ? [`${hm(sw.edct.at, tzA)}`, sw.edct.prev && `was ${hm(sw.edct.prev, tzA)}`, sw.edct.for && `${sw.edct.for} program`,
        sw.edct.cta && `arrival slot ${hm(sw.edct.cta, tzB)}`].filter(Boolean).join(" · ") : ""],
      ["Clearance (datalink)", sw.pdc ? [sw.pdc.cleared_to && `cleared to ${sw.pdc.cleared_to}`, sw.pdc.departure && `via ${sw.pdc.departure}`,
        sw.pdc.altitude && (/^\d+$/.test(sw.pdc.altitude) ? `maintain ${sw.pdc.altitude}` : sw.pdc.altitude),
        sw.pdc.expect_alt ? `expect FL${sw.pdc.expect_alt}` : sw.pdc.fl && `FL${sw.pdc.fl}`, sw.pdc.dep_freq && `departure ${sw.pdc.dep_freq}`,
        sw.pdc.runway && `expect ${sw.pdc.runway}`, sw.pdc.squawk && `squawk ${sw.pdc.squawk}`, sw.pdc.edct && `EDCT ${sw.pdc.edct}Z`,
        sw.pdc.tail && `airplane ${sw.pdc.tail}`, sw.pdc.gate && `gate ${sw.pdc.gate}`, sw.pdc.response && sw.pdc.response.toLowerCase(), sw.pdc.at && `at ${hm(Date.parse(sw.pdc.at) / 1000, tzA)}`]
        .filter(Boolean).join(" · ") : ""],
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
    const ac = leg._ac;
    const T0 = tms(leg);
    const fa_ = leg.fa_id ? `https://www.flightaware.com/live/flight/id/${encodeURIComponent(leg.fa_id)}` : leg.ident ? `https://www.flightaware.com/live/flight/${encodeURIComponent(leg.ident)}` : "";
    $app.innerHTML = `
      ${leg.live && leg.phase === "airborne" ? `<div class="telem" id="telem">${telem(leg.live)}</div>` : ""}
      <div class="sheet-gap d"></div>
      <section class="sheet detail">
        ${sheetHead(`${esc(cityOf(leg.dep_ap, leg.dep))} to ${esc(cityOf(leg.arr_ap, leg.arr))}`,
          `${esc(flno(leg))} • ${ac ? (ac.n === 0 ? "YOUR INBOUND" : `${ac.n + 1} LEGS BACK`) : esc(fmt(T0.out, tzA, { weekday: "short", day: "numeric", month: "short" }).toUpperCase())}${leg.dh ? " • DH" : ""}${leg.extra ? ` • ${leg.kind === "manual" ? "MANUAL" : "FRIEND"}` : ""}`,
          ac ? (ac.n === 0 ? `#/leg/${ac.id}` : `#/aircraft/${ac.id}/${ac.n - 1}`) : "#/", alBadge(airlineCode(leg), "lg"))}
        ${ac ? rotStrip(ac) : ""}
        ${statusBanner(leg)}
        ${nasGroups([leg]).filter((g) => MAJOR.includes(g.n.kind)).map(nasAlert).join("")}
        ${problemBanners(leg)}
        ${endpoint(leg, "dep")}
        ${midLine(leg)}
        ${endpoint(leg, "arr")}
        ${goodToKnow(leg)}
        ${inboundF(leg)}
        ${timesCard(leg)}
        ${aircraftF(leg)}
        <div class="actions">
          ${ac || leg.finished ? "" : '<button class="btn" id="leg-refresh">Refresh this flight</button>'}
          ${!ac && STATE.aeroapi ? '<button class="btn" id="leg-aero">Check AeroAPI now</button>' : ""}
          ${!ac && leg.manual ? `<button class="btn" id="leg-kind">${leg.kind === "manual" ? "Make it a friend's flight" : "It's my flight"}</button><button class="btn" id="leg-remove">Remove</button>` : ""}
          ${fa_ ? `<a class="btn" href="${fa_}" target="_blank" rel="noopener">FlightAware ↗</a>` : ""}
        </div>
        ${fold("leg-details", "Flight Details", `<div class="card"><div class="kv">${kv.map(([k, v]) => `<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join("")}</div>
          ${leg.note ? `<p class="small muted">${esc(leg.note)}</p>` : ""}${leg.error ? `<p class="err">${esc(leg.error)}</p>` : ""}</div>`, false)}
        ${wx ? fold("leg-wx", "METARs", `<div class="card">${wx}</div>`, false) : ""}
        ${nasMinor([leg]).length ? fold("leg-nas", "Other NAS items", nasCard(nasMinor([leg])), false) : ""}
        ${(leg.alerts || []).length ? fold("leg-alerts", "Alerts for This Flight", alertsCard(leg.alerts), false) : ""}
        ${lookupLog(ac ? inboundHist(ac, leg.flight) : leg.history || [], ac ? 0 : leg.history_more)}
        ${leg.shot ? fold("leg-shot", "Latest Screenshot", `<div class="card shot"><img loading="lazy" alt="FlightAware page" onerror="this.closest('details').remove()" src="api/shot/${leg.id}.png?t=${Math.round(leg.updated || 0)}"></div>`, false) : ""}
      </section>`;
    if (ac) {
      // the same map as a flight page, for this leg: surface traffic where that airplane is on the ground
      const h = leg.flight, o = leg.dep_ap, d = leg.arr_ap, key = `ac-${ac.id}-${ac.n}`;
      const surf = { key, lid: ac.id, route: `#/aircraft/${ac.id}/${ac.n}`, view: key, ident: (h.ident || h.display_ident || "").replace(/\s/g, ""),
        ap: () => ["scheduled", "taxi_out"].includes(h.phase) ? o.iata : ["landed", "arrived"].includes(h.phase) ? d.iata : null };
      const earlier = h.phase === "scheduled" ? chainFrom(ac.rot, ac.n + 1) : [];
      wireFolds();
      showMap(key, paintLeg, { id: key, _surf: surf, dep_ap: o, arr_ap: d, flight: h, phase: h.phase, inbound: earlier[0] || null,
        chain: [], live: null, display: h.display_ident }, { kind: "leg" });
      surfaceWatch({ id: key }, surf);
      return;
    }
    showMap(`leg-${leg.id}`, paintLeg, withChain(leg), { kind: "leg" });
    surfaceWatch(leg);
    liveWatch(leg);
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
  // Flight number and date (today unless you change it). Within AeroAPI's reach (the past 9 days to
  // 2 days ahead, with a key set) one query lists every flight with that number that day and you
  // tap yours; further out, or without a key, you type the airports and it's added at once.
  function addView() {
    destroyMaps();
    const manual = STATE.legs.filter((l) => l.manual);
    const tz = STATE.home_tz || undefined;
    const iso = (t) => new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(t));
    const today = iso(Date.now()), earliest = iso(Date.now() - 9 * 86400000), reach = iso(Date.now() + 2 * 86400000);
    $app.innerHTML = `
      <a class="back" href="#/">‹ My Flights</a>
      <h1 class="ptitle">Add a Flight</h1>
      <form id="addform" class="form add-form" autocomplete="off">
        <div class="seg" role="radiogroup">
          <label><input type="radio" name="f-kind" value="manual" checked><span>Mine</span></label>
          <label><input type="radio" name="f-kind" value="friend"><span>A friend's</span></label>
        </div>
        <p class="small faint" style="margin:-4px 0 2px" id="f-kind-help">Tracked like your calendar flights and kept in History.</p>
        <div class="form-2">
          <label>Flight<input id="f-ident" placeholder="UA1234" autocapitalize="characters" spellcheck="false" required></label>
          <label>Date<input id="f-date" type="date" min="${earliest}" value="${today}"></label>
        </div>
        <div id="f-aps" class="form-2" hidden>
          <label>From<input id="f-dep" placeholder="EWR" maxlength="4" autocapitalize="characters"></label>
          <label>To<input id="f-arr" placeholder="SFO" maxlength="4" autocapitalize="characters"></label>
        </div>
        <button class="btn primary" type="submit" id="f-go">Find flights</button>
        <div id="f-msg" class="small"></div>
      </form>
      <div id="f-results"></div>
      ${manual.length ? `<h2 class="section">Added in the App</h2><div class="flist">${manual.map(row).join("")}</div>` : ""}
      <p class="small faint" style="margin:16px 4px">Looking up a flight number is one AeroAPI query (the flight you pick reuses it, so tracking doesn't spend another). Your own flights are saved to History when they finish; friends' aren't. App-added flights leave the list a day and a half after departure.</p>`;
    const $ = (id) => document.getElementById(id);
    const msg = $("f-msg"), res = $("f-results"), aps = $("f-aps"), go_ = $("f-go");
    const kind = () => (document.querySelector('[name="f-kind"]:checked') || {}).value || "manual";
    $app.querySelectorAll('[name="f-kind"]').forEach((r) => r.onchange = () => {
      $("f-kind-help").textContent = kind() === "manual" ? "Tracked like your calendar flights and kept in History." : "Live tracking and pushes only; not saved to History.";
    });
    // airports needed: beyond AeroAPI's 2 days (no waiting, no query), no key, or no flights found
    const askAirports = (why) => {
      aps.hidden = false; go_.textContent = "Add";
      $("f-dep").required = $("f-arr").required = true;
      msg.className = "small muted"; msg.textContent = why || "";
      res.innerHTML = "";
    };
    const reset = () => { aps.hidden = true; go_.textContent = "Find flights"; $("f-dep").required = $("f-arr").required = false; msg.textContent = ""; res.innerHTML = ""; };
    const far = () => { const d = $("f-date").value; return d && d > reach; };
    const onDate = () => { if (far() || !STATE.aeroapi) askAirports(far() ? "More than 2 days out: enter the airports (added right away)." : "Enter the airports."); else reset(); };
    $("f-date").onchange = onDate;
    $("f-ident").oninput = () => { if (!aps.hidden && !far() && STATE.aeroapi) reset(); };
    onDate();
    const add = async (body) => {
      msg.className = "small muted"; msg.textContent = "Adding…";
      try {
        const r = await fetch("api/flights", { method: "POST", body: JSON.stringify({ ...body, kind: kind() }) });
        changed("api/flights");
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || r.status);
        await load();
        go(`#/leg/${d.id}`);
      } catch (err) { msg.className = "small err"; msg.textContent = err.message; }
    };
    $("addform").onsubmit = async (e) => {
      e.preventDefault();
      const ident = $("f-ident").value.trim().toUpperCase().replace(/\s/g, ""), date = $("f-date").value;
      if (!aps.hidden) return add({ ident, dep: $("f-dep").value, arr: $("f-arr").value, date });
      msg.className = "small muted"; msg.textContent = "Looking up…"; res.innerHTML = "";
      let d;
      try { d = await api(`api/lookup?${new URLSearchParams({ ident, date })}`); }
      catch (err) { return askAirports(`Couldn't look it up (${err.message}). Enter the airports.`); }
      if (d.error) { msg.className = "small err"; msg.textContent = d.error; return; }
      if (d.mode !== "list") return askAirports(d.reason);
      if (!d.flights.length) return askAirports(`No ${ident} flights found on ${date}. Check the number, or enter the airports.`);
      msg.textContent = "";
      res.innerHTML = `<h2 class="section">${esc(d.flights[0].display || ident)} · ${esc(fmt(Date.parse(date + "T12:00:00") / 1000, undefined, { weekday: "short", month: "short", day: "numeric" }))}</h2>
        <div class="flist pick">${d.flights.map((f, i) => {
          const o = f.out || {}, n = f.in || {}, so = o.estimated || o.scheduled, si = n.estimated || n.scheduled;
          const late = o.scheduled && so && so - o.scheduled >= 300;
          return `<button type="button" class="frow pickrow" data-i="${i}">
            <div class="fw"><span class="fw-ic">${IC.plus}</span></div>
            <div class="fr-main">
              <div class="fr-top">${alBadge(airlineCode({ display: f.display }))}<span class="fr-no">${esc(flno({ display: f.display }))}</span>
                <span class="fr-st ${f.cancelled ? "red" : late ? "amber" : ""}">${esc(f.cancelled ? "Cancelled" : f.status || "")}</span></div>
              <div class="fr-cities">${esc(f.dep_city || f.dep)} to ${esc(f.arr_city || f.arr)}</div>
              <div class="fr-times"><span class="ft"><i class="ti">${IC.dep}</i>${esc(f.dep)} <b class="${late ? "amber" : ""}">${so ? hm(so, f.dep_tz) : "—"}</b></span>
                <span class="ft"><i class="ti">${IC.arr}</i>${esc(f.arr)} <b>${si ? hm(si, f.arr_tz) : "—"}</b></span></div>
              ${f.aircraft_type || f.tail ? `<div class="fr-warn" style="color:var(--muted)">${esc([f.aircraft_type, f.tail].filter(Boolean).join(" · "))}</div>` : ""}
            </div></button>`;
        }).join("")}</div>
        <button type="button" class="linkbtn" id="f-other" style="margin:10px 0 0">Not listed? Enter the airports</button>`;
      res.querySelectorAll("[data-i]").forEach((b) => b.onclick = () => {
        const f = d.flights[+b.dataset.i];
        b.disabled = true;
        add({ ident: d.ident, fa_id: f.fa_id, date: d.date });
      });
      $("f-other").onclick = () => askAirports("");
    };
  }

  // ---------- notification settings ----------
  function notifyView() { return swr("api/rules", drawNotify); }
  function drawNotify(d) {
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
      <a class="back" href="#/tools">‹ Settings</a>
      <h1 class="ptitle">Notifications</h1>
      <div class="card">
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

  function toolsView() {
    return swr(["api/debug", "api/settings", "api/backup"], drawTools);
  }
  function drawTools(d, st, bk) {
    st = st || { settings: [] }; bk = bk || { configured: false };
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
      <div class="ptitle-row"><h1 class="ptitle">Settings</h1><a class="round back" href="#/" aria-label="Close">${IC.x}</a></div>
      <div class="glist">
        <a class="gl-row" href="#/notify"><span>Notifications</span>${IC.chev}</a>
        <a class="gl-row" href="#/add"><span>Add a flight</span>${IC.chev}</a>
        <button type="button" class="gl-row" id="p-refresh"><span>Check all flights now</span><span class="small muted" id="p-refresh-msg"></span></button>
        <label class="gl-row"><span>24-hour time</span><span class="switch"><input type="checkbox" id="p-24h" ${CLOCK24 ? "checked" : ""}><span></span></span></label>
        <div class="gl-row"><span>Map sharpness<small class="gl-sub">Fewer pixels to draw: Fast is softer but the lightest</small></span><div class="seg mini">${[["full", "Full"], ["balanced", "Balanced"], ["fast", "Fast"]].map(([k, l]) => `<button type="button" data-mapq="${k}" class="${pref("ft-mapq", "full") === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
        <div class="gl-row"><span>Map shape<small class="gl-sub">Flat is lighter to draw: smoother dragging and zooming</small></span><div class="seg mini">${[["globe", "Globe"], ["flat", "Flat"]].map(([k, l]) => `<button type="button" data-proj="${k}" class="${(flatMap() ? "flat" : "globe") === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
        <div class="gl-row"><span>Map</span><div class="seg mini">${[["satellite", "Satellite"], ["dark", "Dark"]].map(([k, l]) => `<button type="button" data-ms="${k}" class="${mapStyle() === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
        ${mapStyle() === "google" && GSESS.err ? `<div class="gl-row"><span class="err small">Google tiles: ${esc(GSESS.err)} (showing Esri)</span></div>` : ""}
        <div class="gl-row"><span>Map for your trips<small class="gl-sub" id="wt-info">${(() => { const w = warmStatus(); return WARMT.running ? `Downloading… ${WARMT.done.toLocaleString()} of ${WARMT.total.toLocaleString()}${WARMT.home ? " · home network, full speed" : ""}` : w ? `Saved: your routes, every zoom over their airports and the inbound airports, their diagrams, and your Passport airports (${w.tiles.toLocaleString()} tiles) · checked ${ago(w.at / 1000)}` : "Downloads in the background while the app is open"; })()}</small></span>${TC_ON ? '<button type="button" class="btn mini" id="wt-now">Update</button>' : ""}</div>
        <div class="gl-row"><span>Saved map tiles<small class="gl-sub" id="tc-info">${TC_ON ? "…" : "Needs the https address (the Home Assistant app)"}</small></span>${TC_ON ? '<button type="button" class="btn mini" id="tc-clear">Clear</button>' : ""}</div>
        <label class="gl-row"><span>Weather radar on maps</span><span class="switch"><input type="checkbox" id="p-radar" ${radarOn() ? "checked" : ""}><span></span></span></label>
        <div class="gl-row"><span>Radar source<small class="gl-sub">NOAA: filtered, US and nearby · RainViewer: worldwide, unfiltered</small></span><div class="seg mini">${[["noaa", "NOAA"], ["rv", "RainViewer"]].map(([k, l]) => `<button type="button" data-rs="${k}" class="${radarSrc() === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
        <label class="gl-row"><span>Data saver<small class="gl-sub">Lighter map imagery, no radar, live position every 15 s</small></span><span class="switch"><input type="checkbox" id="p-low" ${lowData() ? "checked" : ""}><span></span></span></label>
      </div>
      <h2 class="section"><span>Add-on</span><span id="s-msg" class="small"></span></h2>
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
        <h3 style="margin:18px 0 4px">Notifications on this phone</h3>
        <p class="small muted" style="margin:0 0 10px" id="wp-state">…</p>
        <div class="actions" style="margin:0"><button class="btn primary" id="wp-on" hidden>Turn on</button><button class="btn" id="wp-test" hidden>Send a test</button><button class="btn" id="wp-off" hidden>Turn off</button></div>
        <div id="wp-msg" class="small" style="margin-top:8px"></div>
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
        <div class="k">FAA registry</div><div>${d.faa && d.faa.rows ? `${d.faa.rows.toLocaleString()} aircraft · FAA file of ${esc(d.faa.file_date || "?")}` : d.faa && d.faa.busy ? "downloading…" : "not loaded yet"}${d.faa && d.faa.busy && d.faa.rows ? " · updating now" : ""}${d.faa && d.faa.error ? ` · <span class="err">${esc(d.faa.error)}</span>` : ""}</div>
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
    document.getElementById("p-24h").onchange = (e) => { CLOCK24 = e.target.checked; setPref("ft-24h", CLOCK24 ? "1" : "0"); };
    document.getElementById("p-refresh").onclick = () => {
      document.getElementById("p-refresh-msg").textContent = "Checking… (about 20 s)";
      $refresh.click();
    };
    document.getElementById("p-radar").onchange = (e) => setPref("ft-radar", e.target.checked ? "1" : "0");
    $app.querySelectorAll("[data-rs]").forEach((b) => b.onclick = () => {
      setPref("ft-radar-src", b.dataset.rs);
      $app.querySelectorAll("[data-rs]").forEach((x) => x.classList.toggle("on", x === b));
      if (radarOn()) glRadar(true);
    });
    document.getElementById("p-low").onchange = (e) => { setPref("ft-lowdata", e.target.checked ? "1" : "0"); restyleAll(); };
    $app.querySelectorAll("[data-mapq]").forEach((b) => b.onclick = () => {
      setPref("ft-mapq", b.dataset.mapq);
      $app.querySelectorAll("[data-mapq]").forEach((x) => x.classList.toggle("on", x === b));
      if (GL.map) { try { GL.map.setPixelRatio(mapPR()); } catch {} restyleAll(); }   // takes effect at once
    });
    $app.querySelectorAll("[data-proj]").forEach((b) => b.onclick = () => {
      setPref("ft-proj", b.dataset.proj);
      $app.querySelectorAll("[data-proj]").forEach((x) => x.classList.toggle("on", x === b));
      restyleAll();
      GL.everFit = false; GL.view = null;   // frame afresh for the new shape when you go back to a map
    });
    $app.querySelectorAll("[data-ms]").forEach((b) => b.onclick = () => {
      setPref("ft-mapstyle", b.dataset.ms);
      $app.querySelectorAll("[data-ms]").forEach((x) => x.classList.toggle("on", x === b));
      restyleAll();
    });
    const tci = document.getElementById("tc-info");
    const tcShow = () => tileCacheInfo().then((i) => { if (tci && i) tci.textContent = `${i.n.toLocaleString()} tiles kept on this phone${i.bytes ? ` · ${i.bytes < 1048576 ? `${Math.max(1, Math.round(i.bytes / 1024))} KB` : `${Math.round(i.bytes / 1048576)} MB`} stored by the app` : ""}`; });
    if (TC_ON) tcShow();
    const wtb = document.getElementById("wt-now"), wti = document.getElementById("wt-info");
    if (wtb) wtb.onclick = () => {
      warmTrips(true);
      const t = setInterval(() => { if (!document.body.contains(wti)) return clearInterval(t);
        wti.textContent = WARMT.running ? `Downloading… ${WARMT.done.toLocaleString()} of ${WARMT.total.toLocaleString()}${WARMT.home ? " · home network, full speed" : ""}` : "Done"; if (!WARMT.running) { clearInterval(t); tcShow(); } }, 700);
    };
    const tcb = document.getElementById("tc-clear");
    if (tcb) tcb.onclick = async () => { tcb.disabled = true; await tileCacheClear(); tcb.disabled = false; tcShow(); };
    wirePush();
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
  try { localStorage.removeItem("ft-mapshot"); } catch {}   // the old saved map picture (removed in 4.16.1)
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
    document.body.dataset.drive = st;
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
      if (force || !STATE) {
        // no signal or the add-on is unreachable: carry on with the phone's copy (Live dot gray)
        try { await load(); } catch (e) { if (!STATE) throw e; try { setLive(false); } catch {} }
      }
      document.body.classList.toggle("sheetmode", !/^#\/(add|tools|notify|history\/(stats|find|alerts))/.test(route));
      morphing = route === lastRoute && $root.childElementCount > 0;
      document.body.classList.toggle("sheetmode", !/^#\/(add|tools|notify|history\/(stats|find|alerts))/.test(route));
      const m = route.match(/^#\/leg\/([\w-]+)/);
      const ma = route.match(/^#\/aircraft\/([\w-]+)\/(\d+)/);
      const mh = route.match(/^#\/history(?:\/(stats|alerts))?(?:\/(\d{4}))?$/);
      const mf = route.match(/^#\/hist\/(.+)$/);
      const mn = route.match(/^#\/nas\/(.+)$/);
      const first = $app.firstElementChild;
      const job = (async () => {
        if (route.startsWith("#/history/find")) await findView(route);
        else if (mh) await (mh[1] === "stats" ? statsView(mh[2] || "") : mh[1] === "alerts" ? alertsHistoryView() : historyView(mh[2] || ""));
        else if (mf) await histFlightView(decodeURIComponent(mf[1]));
        else if (mn) await nasView(decodeURIComponent(mn[1]));
        else if (ma) await aircraftView(ma[1], parseInt(ma[2], 10));
        else if (m) await legView(m[1]);
        else if (route.startsWith("#/add")) addView();
        else if (route.startsWith("#/airports")) await airportsView();
        else if (route.startsWith("#/tools")) await toolsView();
        else if (route.startsWith("#/notify")) await notifyView();
        else homeView();
      })();
      // The page's copy on the phone is drawn right away (before its fresh data arrives): move the
      // sheet to the page's resting spot now, so the map frames once, for the right room, instead
      // of framing for the old spot and correcting when the fresh data lands (first Passport visit).
      let placed = false;
      if (restoreY == null && route !== lastRoute && $app.firstElementChild !== first) {
        window.scrollTo(0, document.body.classList.contains("sheetmode") ? midY() : 0); placed = true;
      }
      await job;
      // hero map is drawn after its own fetch; give it a moment before clearing unused maps
      const tab = /^#\/history\/alerts/.test(route) ? "alerts" : /^#\/history\/find/.test(route) ? "search"
        : /^#\/(history|hist\/)/.test(route) ? "passport" : /^#\/(tools|notify|add)/.test(route) ? "" : "flights";
      document.querySelectorAll("#tabbar a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
      if (restoreY != null) { const y = restoreY; restoreY = null; window.scrollTo(0, y); requestAnimationFrame(() => { window.scrollTo(0, y); sheetUp(); }); }
      else if (route !== lastRoute && !placed) window.scrollTo(0, document.body.classList.contains("sheetmode") ? midY() : 0);
      lastRoute = route;
    } catch (e) {
      // no signal / add-on unreachable: keep the page drawn from the phone's copy (the Live dot goes
      // gray); only an empty screen gets the error
      if (STATE && $app.childElementCount && !$app.querySelector(".card.empty b")) { try { setLive(false); } catch {} }
      else $app.innerHTML = `<div class="card empty"><b>Can't reach the add-on</b>${esc(e.message)}</div>`;
    } finally {
      morphing = false;
    }
  }

  $refresh.onclick = async () => {
    $refresh.classList.add("spin");
    const hr = document.getElementById("h-refresh"); if (hr) hr.classList.add("spin");
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
  // once the sheet has scrolled over the map, the strip under the phone's status bar takes the
  // sheet's color (the sticky header sits just below it)
  const sheetUp = () => {
    const g = document.querySelector(".sheet-gap");
    document.body.classList.toggle("sheet-up", !!g && document.body.classList.contains("sheetmode") && window.scrollY >= g.offsetHeight - 4);
  };
  window.addEventListener("scroll", sheetUp, { passive: true });
  // Let go between peeking and the middle stop and the sheet settles on the nearer one, like iOS.
  let touching = false, settleT = null;
  const settle = () => {
    clearTimeout(settleT);
    settleT = setTimeout(() => {
      if (touching || !document.body.classList.contains("sheetmode")) return;
      const m = midY(), y = window.scrollY, g = document.querySelector(".sheet-gap"), top = g ? g.offsetHeight : 0;
      if (m > 0 && y > 2 && y < m - 2) window.scrollTo({ top: y < m * 0.5 ? 0 : m, behavior: "smooth" });
      else if (top > m && y > m + 2 && y < top - 2) window.scrollTo({ top: y < (m + top) / 2 ? m : top, behavior: "smooth" });
    }, 140);
  };
  window.addEventListener("scroll", settle, { passive: true });
  let restT = null;
  window.addEventListener("scroll", () => {
    clearTimeout(restT);
    restT = setTimeout(() => {
      if (touching || !GL.map || !GL.ready || !document.body.classList.contains("sheetmode") || GL.fitRaf || GL.map.isMoving()) return;
      const g = document.querySelector(".sheet-gap");
      if (!g || window.scrollY >= g.offsetHeight - 40) return;   // sheet up over the map
      const H = MAP_EL.clientHeight || window.innerHeight, top = g.getBoundingClientRect().bottom;
      const p = GL.map.getPadding(), bottom = Math.max(24, Math.min(H - 170, H - top + 12));
      if (Math.abs(bottom - p.bottom) > 30) { GL.map.easeTo({ padding: { ...p, bottom }, duration: 450 }); GL.fitBottom = bottom; }
    }, 320);
  }, { passive: true });
  document.addEventListener("touchstart", () => { touching = true; clearTimeout(settleT); }, { passive: true });
  document.addEventListener("touchend", () => { touching = false; settle(); }, { passive: true });
  document.addEventListener("touchcancel", () => { touching = false; settle(); }, { passive: true });
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
    document.body.classList.toggle("is-live", !!on);
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
    render(true).then(() => setTimeout(prefetchAll, 1500));  // fresh data right away, not after the stream reconnects
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
  // The app itself stays on the phone (sw.js): it opens instantly, then refreshes in the background.
  // Only on the add-on's own https address; through Home Assistant the page's address changes every
  // session, so there's nothing stable to keep.
  if ("serviceWorker" in navigator && window.isSecureContext && !/hassio_ingress|api\/ingress/.test(location.pathname)) {
    navigator.serviceWorker.register("sw.js", { scope: "./" }).catch(() => {});
    // a tapped notification while the app is open: go to that flight
    navigator.serviceWorker.addEventListener("message", (e) => { if (e.data && e.data.go) go(e.data.go); });
    // ask iOS / the browser to keep the app's storage (map tiles, flights) rather than clear it when space runs low
    try { if (navigator.storage && navigator.storage.persist) navigator.storage.persisted().then((p) => p || navigator.storage.persist()); } catch {}
  }
  // Draw the last known state right away, then fetch the current one.
  const snap = loadSnapshot();
  if (snap) {
    STATE = snap;
    $updated.textContent = "updating…";
    render(false).then(() => render(true)).then(() => setTimeout(prefetchAll, 800));
  } else render(true).then(() => setTimeout(prefetchAll, 800));
})();
