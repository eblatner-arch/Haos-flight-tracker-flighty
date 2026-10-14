#!/usr/bin/env python3
"""Flight Tracker add-on: schedule watcher, tracker worker, alerts, sensors and the web app."""

import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import airports
import alerts as alerting
import flightaware as fa
import schedule
import sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flight_tracker")

DATA_DIR = os.environ.get("FT_DATA", "/data")
SHOT_DIR = os.environ.get("FT_SHOTS", "/share/flight_tracker")
WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
PORT = int(os.environ.get("FT_PORT", "8099"))
VERSION = os.environ.get("FT_VERSION", "dev")
SUPERVISOR = os.environ.get("FT_SUPERVISOR_URL", "http://supervisor")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HOUR = 3600

DEFAULTS = {
    "ics_file_path": "/homeassistant/ics2gcal/feed.ics",
    "airline_icao": "",
    "flight_regex": "",
    "watch_hours": 12,
    "track_deadheads": True,
    "track_flights": [],
    "notify_service": "",
    "notify_watch_start": True,
    "notify_inbound_progress": True,
    "notify_progress": False,
    "notify_progress_deadhead": True,
    "time_sensitive": True,
    "delay_threshold_minutes": 15,
    "delay_step_minutes": 10,
    "min_turn_minutes": 35,
    "poll_far_minutes": 30,
    "poll_near_minutes": 10,
    "poll_fast_minutes": 3,
    "nas_minutes": 5,
    "live_positions": True,
    "gemini_api_key": "",
    "gemini_model": "gemini-flash-lite-latest",
    "home_timezone": "",
    "publish_sensors": True,
}


def load_options():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(DATA_DIR, "options.json")) as f:
            cfg.update({k: v for k, v in json.load(f).items() if v is not None})
    except FileNotFoundError:
        log.warning("No options.json; using defaults")
    cfg["airline_icao"] = (cfg.get("airline_icao") or "").strip().upper()
    return cfg


CFG = load_options()
TZNAME = (CFG.get("home_timezone") or "").strip() or os.environ.get("TZ") or "UTC"
try:
    HOME_TZ = ZoneInfo(TZNAME)
except Exception:
    TZNAME, HOME_TZ = "UTC", ZoneInfo("UTC")

LOCK = threading.RLock()
WAKE = threading.Event()
REFRESH_Q = queue.Queue()
STATE_PATH = os.path.join(DATA_DIR, "state.json")
STATE = {"legs": {}, "nas": [], "nas_at": None, "nas_error": None, "wx": {}, "wx_at": 0,
         "alerts": [], "schedule": {"mtime": None, "error": None, "count": 0, "checked": None},
         "worker": {"busy": None, "last_cycle": None, "error": None}}
INGRESS = {"path": None}

ICAO_TO_IATA = {v: k for k, v in schedule.IATA_TO_ICAO.items()}


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            saved = json.load(f)
        STATE.update({k: saved[k] for k in ("legs", "alerts", "nas", "nas_at", "wx") if k in saved})
        log.info("Loaded %d legs from saved state", len(STATE["legs"]))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Could not read saved state (%s); starting fresh", e)


def save_state():
    with LOCK:
        data = json.dumps({k: STATE[k] for k in ("legs", "alerts", "nas", "nas_at", "wx")})
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# Home Assistant
# --------------------------------------------------------------------------

def ha(method, path, body=None, timeout=10):
    req = urllib.request.Request(f"{SUPERVISOR}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def find_ingress():
    try:
        info = ha("GET", "/addons/self/info")
        slug = (info or {}).get("data", {}).get("slug")
        if slug:
            INGRESS["path"] = f"/hassio/ingress/{slug}"
    except Exception as e:
        log.info("Could not look up the ingress path (%s); notifications won't open the app", e)


def notify(alert):
    svc = (CFG.get("notify_service") or "").strip()
    if svc.startswith("notify."):
        svc = svc[7:]
    data = {"tag": f"{alert['tag']}-{alert['key']}", "group": "flight-tracker"}
    if INGRESS["path"]:
        data["url"] = INGRESS["path"]
        data["clickAction"] = INGRESS["path"]
    if alert["level"] in ("warn", "critical") and CFG.get("time_sensitive", True):
        data["push"] = {"interruption-level": "time-sensitive"}
        data["ttl"] = 0
        data["priority"] = "high"
    try:
        if svc:
            ha("POST", f"/core/api/services/notify/{svc}",
               {"title": alert["title"], "message": alert["message"], "data": data})
        else:
            ha("POST", "/core/api/services/persistent_notification/create",
               {"title": alert["title"], "message": alert["message"],
                "notification_id": data["tag"]})
        log.info("Pushed: %s — %s", alert["title"], alert["message"])
    except Exception as e:
        log.warning("Notification failed (%s): %s", svc or "persistent_notification", e)


def post_state(entity, state, attrs):
    ha("POST", f"/core/api/states/{entity}", {"state": str(state)[:255], "attributes": attrs})


def iso(ts, tz=None):
    return datetime.fromtimestamp(ts, tz or timezone.utc).isoformat() if ts else None


def publish():
    if not CFG.get("publish_sensors", True) or not TOKEN:
        return
    now = time.time()
    with LOCK:
        legs = sorted((dict(L) for L in STATE["legs"].values() if not L.get("extra")), key=lambda L: L["sched_out"])
        nas = list(STATE["nas"])
    upcoming = [L for L in legs if not finished(L, now)]
    nxt = upcoming[0] if upcoming else None
    try:
        if nxt:
            f = nxt.get("flight") or {}
            tz = alerting.tz_for(nxt["dep"], f.get("origin"))
            post_state("sensor.flight_tracker_next_leg", alerting.status_label(nxt, CFG), {
                "friendly_name": "Next flight", "icon": "mdi:airplane-takeoff",
                "flight": nxt.get("display"), "ident": nxt.get("ident"), "deadhead": nxt.get("dh"),
                "origin": nxt["dep"], "destination": nxt["arr"],
                "scheduled_departure": iso(nxt["sched_out"], tz),
                "estimated_departure": iso(fa.best_time(f["times"], "out"), tz) if f else None,
                "scheduled_arrival": iso(nxt["sched_in"], alerting.tz_for(nxt["arr"], f.get("destination"))),
                "estimated_arrival": iso(fa.best_time(f["times"], "in"),
                                         alerting.tz_for(nxt["arr"], f.get("destination"))) if f else None,
                "departure_delay": fa.delay_minutes(f, nxt["sched_out"]) if f else None,
                "gate": (f.get("origin") or {}).get("gate"), "terminal": (f.get("origin") or {}).get("terminal"),
                "arrival_gate": (f.get("destination") or {}).get("gate"),
                "aircraft": f.get("aircraft_type"), "tail": f.get("tail"),
                "inbound": (nxt.get("inbound") or {}).get("display_ident"),
                "inbound_turn_minutes": round(alerting.turn_minutes(nxt)) if alerting.turn_minutes(nxt) is not None else None,
                "problems": [p["text"] for p in alerting.problems(nxt, CFG, nas if watched(nxt, now) else [])],
                "watching": watched(nxt, now),
                "last_update": iso(nxt.get("updated")),
            })
        else:
            post_state("sensor.flight_tracker_next_leg", "None", {"friendly_name": "Next flight",
                                                                  "icon": "mdi:airplane-off"})
        probs = []
        for L in upcoming:
            if watched(L, now):
                probs += [f"{L.get('display') or L['dep']}: {p['text']}" for p in alerting.problems(L, CFG, nas)]
        post_state("binary_sensor.flight_tracker_problem", "on" if probs else "off",
                   {"friendly_name": "Flight problem", "device_class": "problem", "problems": probs})
        post_state("sensor.flight_tracker_nas_programs", len(nas), {
            "friendly_name": "NAS programs", "icon": "mdi:radar", "unit_of_measurement": "programs",
            "programs": [f"{n['airport']} {n['label']}" for n in nas[:40]]})
    except Exception as e:
        log.warning("Could not update sensors: %s", e)


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------

def display_ident(airline, flight):
    al = ICAO_TO_IATA.get(airline, airline)
    return f"{al} {flight}".strip() if flight else ""


def reload_schedule(force=False):
    path = CFG["ics_file_path"]
    now = time.time()
    try:
        mtime = os.path.getmtime(path) if os.path.exists(path) else None
        if not force and mtime and mtime == STATE["schedule"]["mtime"]:
            STATE["schedule"]["checked"] = now
            return
        data, mtime = schedule.load_file(path)
        events = schedule.parse_ics(data, HOME_TZ, now)
        legs = schedule.legs_from_events(events, CFG)
    except Exception as e:
        STATE["schedule"].update(error=str(e), checked=now)
        log.error("Schedule: %s", e)
        return
    with LOCK:
        cur = STATE["legs"]
        seen = set()
        for L in legs:
            if L["dh"] and not CFG.get("track_deadheads", True):
                continue
            seen.add(L["id"])
            L["display"] = display_ident(L["airline"], L["flight_no"])
            old = cur.get(L["id"])
            if old:
                if old.get("ident") != L["ident"] or old.get("dep") != L["dep"]:
                    old.pop("fa_id", None)
                old.update(L)
            else:
                L.update(notified={}, alerts=[], next_check=0)
                cur[L["id"]] = L
        for lid in list(cur):
            L = cur[lid]
            if L.get("extra"):
                continue
            if lid not in seen and L["sched_out"] > now:
                del cur[lid]  # dropped from the schedule
            elif L["sched_in"] < now - 36 * HOUR:
                del cur[lid]
        STATE["schedule"].update(mtime=mtime, error=None, count=len(legs), checked=now)
    log.info("Schedule: %d flights in the next 3 weeks", len(legs))


def sync_extras():
    wanted = {}
    for x in CFG.get("track_flights") or []:
        ident = (x.get("ident") if isinstance(x, dict) else str(x)).strip().upper().replace(" ", "")
        if ident:
            wanted["x-" + re.sub(r"[^A-Z0-9]", "", ident)] = ident
    with LOCK:
        for lid in [k for k, L in STATE["legs"].items() if L.get("extra") and k not in wanted]:
            del STATE["legs"][lid]
        for lid, ident in wanted.items():
            if lid not in STATE["legs"]:
                m = re.match(r"^([A-Z]{2,3}|[A-Z]\d|\d[A-Z])(\d+)$", ident)
                airline = schedule.IATA_TO_ICAO.get(m.group(1), m.group(1)) if m else ""
                STATE["legs"][lid] = {
                    "id": lid, "extra": True, "ident": (airline + m.group(2)) if m else ident,
                    "display": display_ident(airline, m.group(2)) if m else ident, "airline": airline,
                    "flight_no": m.group(2) if m else "", "dep": "", "arr": "", "dh": False,
                    "sched_out": time.time(), "sched_in": time.time(), "summary": "Followed flight",
                    "notified": {"watch": True}, "alerts": [], "next_check": 0}


# --------------------------------------------------------------------------
# tracking
# --------------------------------------------------------------------------

def finished(L, now):
    f = L.get("flight")
    ph = fa.phase(f, now)
    if L.get("extra"):
        return False
    if ph == "arrived":
        t = (f["times"]["in"]["actual"] if f else None) or L["sched_in"]
        return now > t + 45 * 60
    if ph == "cancelled":
        return now > L["sched_out"] + 2 * HOUR
    return now > max(L["sched_in"], (fa.best_time(f["times"], "in") if f else 0) or 0) + 2 * HOUR


def watched(L, now):
    if L.get("extra"):
        return True
    return now >= L["sched_out"] - float(CFG["watch_hours"]) * HOUR and not finished(L, now)


def poll_seconds(L, now):
    f = L.get("flight")
    ph = fa.phase(f, now)
    out_t = (fa.best_time(f["times"], "out") if f else None) or L["sched_out"]
    if ph in ("taxi_out", "airborne", "landed"):
        m = CFG["poll_fast_minutes"]
    elif out_t - now <= 45 * 60:
        m = CFG["poll_fast_minutes"]
    elif out_t - now <= 3 * HOUR:
        m = CFG["poll_near_minutes"]
    else:
        m = CFG["poll_far_minutes"]
    if L.get("extra") and ph in ("scheduled", "arrived", "cancelled"):
        m = max(m, CFG["poll_near_minutes"])
    return max(60, float(m) * 60)


def fetch_flight(browser, L):
    """Load the leg's FlightAware page(s). Returns (flight, source, shot_path, note)."""
    ident, lid = L.get("ident"), L["id"]
    if not ident:
        return None, None, None, "No flight number found in the calendar title"
    shot = None
    if L.get("fa_id"):
        boot, shot = browser.load(fa.id_url(L["fa_id"]), lid)
        cur, _ = fa.flights_from_bootstrap(boot)
        if cur:
            return cur, "FlightAware page data", shot, None
        if boot is not None:
            L.pop("fa_id", None)  # stale instance; look it up again
    if not L.get("fa_id"):
        boot, shot = browser.load(fa.ident_url(ident), lid)
        cur, activity = fa.flights_from_bootstrap(boot)
        if L.get("extra"):
            if cur:
                return cur, "FlightAware page data", shot, None
        elif boot is not None:
            inst = fa.pick_instance(cur, activity, L["dep"], L["sched_out"])
            if inst is None:
                return None, None, shot, "FlightAware doesn't list this departure yet"
            L["fa_id"] = inst["fa_id"]
            if cur and inst["fa_id"] == cur["fa_id"]:
                return cur, "FlightAware page data", shot, None
            boot2, shot = browser.load(fa.id_url(inst["fa_id"]), lid)
            c2, _ = fa.flights_from_bootstrap(boot2)
            return (c2 or inst), "FlightAware page data", shot, None
    # No page data: read the screenshot.
    if browser.client:
        when = datetime.fromtimestamp(L["sched_out"], alerting.tz_for(L.get("dep"))).strftime("%a %d %b %H:%M")
        f = browser.read_screenshot(shot, ident, L.get("dep"), when)
        if f:
            return f, "screenshot (Gemini)", shot, None
        return None, None, shot, "The page screenshot shows no matching flight"
    return None, None, shot, "FlightAware page data unavailable and no Gemini key set for the screenshot fallback"


def update_leg(browser, lid):
    with LOCK:
        L = STATE["legs"].get(lid)
        if not L:
            return
        L = json.loads(json.dumps(L))  # work on a copy outside the lock
    now = time.time()
    try:
        flight, source, shot, note = fetch_flight(browser, L)
        if flight:
            # Fill in airport details FlightAware left out.
            for side, code in (("origin", L.get("dep")), ("destination", L.get("arr"))):
                ap = flight.get(side) or {}
                ref = airports.get(ap.get("iata") or ap.get("icao") or code)
                for k in ("iata", "icao", "name", "city", "lat", "lon", "tz"):
                    if not ap.get(k) and ref.get(k) is not None:
                        ap[k] = ref[k]
                flight[side] = ap
            if L.get("extra"):
                L["dep"] = flight["origin"].get("iata") or L["dep"]
                L["arr"] = flight["destination"].get("iata") or L["arr"]
                L["sched_out"] = flight["times"]["out"]["scheduled"] or flight["times"]["off"]["scheduled"] or now
                L["sched_in"] = flight["times"]["in"]["scheduled"] or flight["times"]["on"]["scheduled"] or now
            # Keep the flown track across refreshes when a page only shows the recent part.
            old_track = (L.get("flight") or {}).get("track") or []
            if old_track and flight.get("track") and flight["track"][0][4] and old_track[0][4] \
                    and old_track[0][4] < flight["track"][0][4] and L.get("fa_id") == flight.get("fa_id"):
                flight["track"] = [p for p in old_track if p[4] and p[4] < flight["track"][0][4]] + flight["track"]
                flight["track"] = flight["track"][-1500:]
            L["flight"], L["source"] = flight, source
        L["note"] = note
        L["shot"] = bool(shot)
        ph = fa.phase(L.get("flight"), now)

        # Inbound aircraft
        f = L.get("flight") or {}
        if f.get("inbound_fa_id") and ph == "scheduled":
            try:
                boot, _ = browser.load(fa.id_url(f["inbound_fa_id"]), lid + "-inbound")
                inb, _ = fa.flights_from_bootstrap(boot)
                if inb:
                    for side in ("origin", "destination"):
                        ap = inb.get(side) or {}
                        ref = airports.get(ap.get("iata") or ap.get("icao"))
                        for k in ("lat", "lon", "tz", "name", "city"):
                            if ap.get(k) is None and ref.get(k) is not None:
                                ap[k] = ref[k]
                    L["inbound"] = inb
            except Exception as e:
                log.warning("%s inbound: %s", L.get("display"), e)
        elif ph != "scheduled":
            pass  # keep the last inbound snapshot for the history
        elif f.get("inbound_ident") and not f.get("inbound_fa_id"):
            L["inbound"] = L.get("inbound") or {"display_ident": f["inbound_ident"], "times": {
                k: {"scheduled": None, "estimated": None, "actual": None} for k in fa.TIME_KEYS},
                "origin": {}, "destination": {}}

        # Live positions
        if CFG.get("live_positions", True):
            if ph in ("taxi_out", "airborne", "landed") and L.get("ident"):
                try:
                    L["live"] = sources.fetch_live(L["ident"])
                except Exception as e:
                    log.info("%s live position: %s", L.get("display"), e)
            else:
                L["live"] = None
            inb = L.get("inbound")
            if inb and ph == "scheduled" and fa.phase(inb, now) in ("taxi_out", "airborne") and inb.get("ident"):
                try:
                    L["inbound_live"] = sources.fetch_live(inb["ident"])
                except Exception as e:
                    log.info("inbound live position: %s", e)
            else:
                L["inbound_live"] = None

        L["updated"] = now
        L["error"] = None
    except Exception as e:
        log.error("%s: %s", L.get("display") or lid, e)
        L["error"] = str(e).splitlines()[0][:300]
    L["next_check"] = now + poll_seconds(L, now)

    with LOCK:
        nas = list(STATE["nas"])
        new_alerts = alerting.evaluate(L, CFG, now, nas) if not L.get("extra") or L.get("flight") else []
        live = STATE["legs"].get(lid)
        if live is None:
            return
        # Keep schedule fields that may have changed while we were fetching.
        for k in ("sched_out", "sched_in", "summary", "dh") if not L.get("extra") else ():
            L[k] = live.get(k, L.get(k))
        L["alerts"] = (L.get("alerts") or []) + new_alerts
        L["alerts"] = L["alerts"][-60:]
        STATE["legs"][lid] = L
        for a in new_alerts:
            STATE["alerts"].append({**a, "leg": lid, "display": L.get("display"), "route": f"{L['dep']}→{L['arr']}"})
        STATE["alerts"] = STATE["alerts"][-200:]
    for a in new_alerts:
        notify(a)
    log.info("%s %s→%s: %s%s", L.get("display"), L.get("dep"), L.get("arr"),
             alerting.status_label(L, CFG), f" ({L['note']})" if L.get("note") else "")


def refresh_nas(now):
    try:
        items = sources.fetch_nas()
        with LOCK:
            STATE["nas"], STATE["nas_at"], STATE["nas_error"] = items, now, None
    except Exception as e:
        with LOCK:
            STATE["nas_error"] = str(e)[:200]
            STATE["nas_at"] = now
        log.warning("NAS status: %s", e)


def refresh_wx(now):
    with LOCK:
        codes = set()
        for L in STATE["legs"].values():
            if watched(L, now) or (L["sched_out"] - now < 36 * HOUR and not finished(L, now)):
                for c in (L.get("dep"), L.get("arr")):
                    codes.add(airports.icao(c) or ("K" + c if c and len(c) == 3 else ""))
    try:
        wx = sources.fetch_metars(codes)
        with LOCK:
            STATE["wx"], STATE["wx_at"] = wx, now
    except Exception as e:
        log.warning("METAR: %s", e)
        with LOCK:
            STATE["wx_at"] = now


def worker():
    from browser import Browser
    browser = Browser(os.path.join(DATA_DIR, "browser_profile"), SHOT_DIR,
                      CFG.get("gemini_api_key") or "", CFG.get("gemini_model") or "gemini-flash-lite-latest")
    last_sched = last_save = last_pub = 0
    while True:
        now = time.time()
        try:
            forced = set()
            while not REFRESH_Q.empty():
                forced.add(REFRESH_Q.get_nowait())
            if now - last_sched > 300 or "all" in forced:
                reload_schedule(force="all" in forced)
                sync_extras()
                last_sched = now
            if now - (STATE["nas_at"] or 0) > CFG["nas_minutes"] * 60 or "all" in forced:
                refresh_nas(now)
            if now - STATE.get("wx_at", 0) > 600 or "all" in forced:
                refresh_wx(now)
            with LOCK:
                due = sorted((L["next_check"] if L["id"] not in forced and "all" not in forced else 0, L["id"])
                             for L in STATE["legs"].values() if watched(L, now))
            for when, lid in due:
                if when <= time.time():
                    STATE["worker"]["busy"] = lid
                    update_leg(browser, lid)
            STATE["worker"].update(busy=None, last_cycle=time.time(), error=None)
            if time.time() - last_pub > 60 or due:
                publish()
                last_pub = time.time()
            if time.time() - last_save > 120 or any(w <= now for w, _ in due):
                save_state()
                last_save = time.time()
        except Exception as e:
            log.exception("Worker cycle failed")
            STATE["worker"]["error"] = str(e)[:300]
        WAKE.wait(20)
        WAKE.clear()


# --------------------------------------------------------------------------
# web
# --------------------------------------------------------------------------

def leg_summary(L, now, nas, full=False):
    f = L.get("flight")
    out = {k: L.get(k) for k in ("id", "ident", "display", "airline", "flight_no", "dep", "arr", "dh", "extra",
                                  "sched_out", "sched_in", "summary", "source", "note", "error", "updated",
                                  "next_check", "shot", "fa_id")}
    out["status"] = alerting.status_label(L, CFG)
    out["phase"] = fa.phase(f, now)
    out["watching"] = watched(L, now)
    out["finished"] = finished(L, now)
    if out["finished"] and out["phase"] == "scheduled":
        out["status"] = "Completed"
    out["problems"] = alerting.problems(L, CFG, nas if out["watching"] else [])
    out["delay"] = fa.delay_minutes(f, L["sched_out"]) if f else None
    out["arr_delay"] = fa.arrival_delay_minutes(f, L["sched_in"]) if f else None
    t = alerting.turn_minutes(L)
    out["turn"] = round(t) if t is not None else None
    for side, code in (("dep_ap", L.get("dep")), ("arr_ap", L.get("arr"))):
        ref = airports.get(code)
        fl = (f or {}).get("origin" if side == "dep_ap" else "destination") or {}
        out[side] = {**ref, **{k: v for k, v in fl.items() if v is not None}}
        if not out[side].get("tz"):
            out[side]["tz"] = TZNAME
    out["nas"] = alerting.nas_for(L, nas)
    if f:
        fl = {k: v for k, v in f.items() if k not in ("track", "waypoints")}
        if full:
            fl["track"] = f.get("track") or []
            fl["waypoints"] = f.get("waypoints") or []
        else:
            tr = f.get("track") or []
            fl["last_position"] = tr[-1] if tr else None
        out["flight"] = fl
    else:
        out["flight"] = None
    inb = L.get("inbound")
    if inb:
        out["inbound"] = {k: v for k, v in inb.items() if k not in ("track", "waypoints")}
        if full:
            out["inbound"]["track"] = inb.get("track") or []
            out["inbound"]["waypoints"] = inb.get("waypoints") or []
        out["inbound"]["phase"] = fa.phase(inb, now)
        out["inbound"]["status"] = alerting.PHASE_LABEL[fa.phase(inb, now)]
        out["inbound"]["delay"] = fa.arrival_delay_minutes(inb)
    else:
        out["inbound"] = None
    out["live"] = L.get("live")
    out["inbound_live"] = L.get("inbound_live")
    if full:
        out["alerts"] = list(reversed(L.get("alerts") or []))
        wx = STATE.get("wx") or {}
        out["wx"] = {c: wx.get(airports.icao(c) or ("K" + c if c else "")) for c in (L.get("dep"), L.get("arr")) if c}
    return out


def api_state():
    now = time.time()
    with LOCK:
        nas = list(STATE["nas"])
        legs = [leg_summary(L, now, nas) for L in STATE["legs"].values()]
        wx = dict(STATE.get("wx") or {})
        data = {
            "now": now, "version": VERSION, "home_tz": TZNAME,
            "legs": sorted(legs, key=lambda L: (L["extra"] or False, L["sched_out"])),
            "nas": nas, "nas_at": STATE["nas_at"], "nas_error": STATE["nas_error"],
            "wx": wx,
            "alerts": list(reversed(STATE["alerts"][-50:])),
            "schedule": STATE["schedule"], "worker": STATE["worker"],
            "settings": {k: CFG[k] for k in ("watch_hours", "delay_threshold_minutes", "min_turn_minutes",
                                             "airline_icao", "live_positions")},
            "notify_target": CFG.get("notify_service") or "persistent notification",
            "gemini": bool(CFG.get("gemini_api_key")),
        }
    return data


def api_leg(lid):
    now = time.time()
    with LOCK:
        L = STATE["legs"].get(lid)
        return leg_summary(L, now, list(STATE["nas"]), full=True) if L else None


STATIC = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
          ".webmanifest": "application/manifest+json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache="no-store"):
        if isinstance(body, (dict, list)) or body is None:
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            path = "/index.html"
        if path == "/api/state":
            return self._send(200, api_state())
        m = re.match(r"^/api/leg/([\w-]+)$", path)
        if m:
            d = api_leg(m.group(1))
            return self._send(200 if d else 404, d or {"error": "not found"})
        m = re.match(r"^/api/shot/([\w-]+)\.png$", path)
        if m:
            p = os.path.join(SHOT_DIR, m.group(1) + ".png")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            return self._send(404, {"error": "no screenshot"})
        name = os.path.basename(path)
        p = os.path.join(WWW, name)
        ext = os.path.splitext(name)[1]
        if ext in STATIC and os.path.isfile(p):
            with open(p, "rb") as f:
                return self._send(200, f.read(), STATIC[ext], "no-cache")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        m = re.match(r"^/api/refresh(?:/([\w-]+))?$", path)
        if m:
            REFRESH_Q.put(m.group(1) or "all")
            WAKE.set()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def main():
    load_state()
    if not CFG["airline_icao"]:
        log.warning("airline_icao is empty: titles without an airline code (e.g. '2204 EWR-IAH') can't be tracked")
    find_ingress()
    threading.Thread(target=worker, daemon=True, name="tracker").start()
    log.info("Flight Tracker %s on port %d (watch window %sh, home time zone %s)", VERSION, PORT,
             CFG["watch_hours"], TZNAME)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
