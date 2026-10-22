#!/usr/bin/env python3
"""Flight Tracker Flighty add-on: schedule watcher, tracker worker, alerts, sensors and the web app."""

import gzip
import hashlib
import json
import logging
import os
import queue
import re
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import aeroapi
import airports
import alerts as alerting
import artcc
import flightaware as fa
import gdrive
import faa
import history
import livery
import rules as rules_mod
import schedule
import sources
import swim
try:
    import webpush
except Exception as _e:   # cryptography missing: phone notifications off, everything else runs
    webpush = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flight_tracker_flighty")

DATA_DIR = os.environ.get("FT_DATA", "/data")
LIVERY_DIR = os.path.join(DATA_DIR, "livery")
SHOT_DIR = os.environ.get("FT_SHOTS", "/share/flight_tracker_flighty")
WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
# /leaflet/ was a second copy of the app while Leaflet was being tried out; it now answers with the
# same app (Home Screen icons made for it keep working).
PORT = int(os.environ.get("FT_PORT", "8099"))
VERSION = os.environ.get("FT_VERSION", "dev")
SUPERVISOR = os.environ.get("FT_SUPERVISOR_URL", "http://supervisor")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HOUR = 3600

DEFAULTS = {
    "ha_notify": True,
    "ios_app_links": False,
    "ics_file_path": "/homeassistant/ics2gcal/feed.ics",
    "airline_icao": "",
    "flight_regex": "",
    "watch_hours": 18,
    "track_deadheads": True,
    "track_flights": [],
    "notify_service": "",
    "notify_watch_start": True,
    "notify_inbound_progress": True,
    "notify_progress": False,
    "notify_progress_deadhead": True,
    "time_sensitive": True,
    "gate_alert_hours": 2,
    "swap_alert_hours": 2,
    "inbound_alert_minutes": 60,
    "delay_threshold_minutes": 15,
    "delay_step_minutes": 10,
    "min_turn_minutes": 35,
    "poll_far_minutes": 30,
    "poll_near_minutes": 10,
    "poll_fast_minutes": 3,
    "poll_air_minutes": 10,
    "schedule_days": 60,
    "adsb_inbound_minutes": 3,
    "nas_minutes": 5,
    "live_positions": True,
    "gemini_api_key": "",
    "aeroapi_key": "",
    "aeroapi_monthly_budget": 5.0,
    "aeroapi_cost_per_query": 0.005,
    "aeroapi_max_per_leg": 6,
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
         "worker": {"busy": None, "last_cycle": None, "error": None},
         "manual": [], "debug": [], "gemini_calls": [], "aero_calls": [], "rules": {}, "flags": {},
         "blocked_streak": 0}
PERSIST = ("legs", "alerts", "nas", "nas_at", "wx", "manual", "gemini_calls", "aero_calls", "rules", "flags", "app_settings")
INGRESS = {"path": None}
CHANGE = threading.Condition()
CHANGE_V = [0]


def bump():
    """Tell open pages something changed."""
    with CHANGE:
        CHANGE_V[0] += 1
        CHANGE.notify_all()

STATE["rules"] = rules_mod.seed_from_config(CFG)  # replaced by the saved rules in init_rules()
ICAO_TO_IATA = {v: k for k, v in schedule.IATA_TO_ICAO.items()}


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH) as f:
            saved = json.load(f)
        STATE.update({k: saved[k] for k in PERSIST if k in saved})
        log.info("Loaded %d legs from saved state", len(STATE["legs"]))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Could not read saved state (%s); starting fresh", e)


# Settings that live in the app (Settings page) rather than the add-on configuration.
# key: (kind, min, max, label, help)
APP_SETTINGS = {
    "schedule_days": ("int", 7, 180, "Show schedule ahead", "Days of your crew calendar shown on the Flights screen. Showing them costs nothing: no checks run until the watch window.", "Watch window"),
    "watch_hours": ("int", 1, 48, "Start watching", "Hours before departure. Nothing runs for a flight before this; then the first look is AeroAPI, then page checks.", "Watch window"),
    "poll_far_minutes": ("int", 5, 120, "More than 3 h out", "Minutes between FlightAware page checks.", "FlightAware page checks"),
    "poll_near_minutes": ("int", 2, 60, "Within 3 h", "Minutes between FlightAware page checks.", "FlightAware page checks"),
    "poll_fast_minutes": ("int", 1, 30, "Last 45 min, taxi, landing", "Minutes between page checks around departure, in the last 20 min before landing and taxiing in.", "FlightAware page checks"),
    "poll_air_minutes": ("int", 2, 60, "In the air", "Minutes between page checks after takeoff. Tracking stops at the gate.", "FlightAware page checks"),
    "adsb_inbound_minutes": ("int", 0, 30, "ADS-B on your inbound", "While your inbound is taxiing or flying, read its transponder this often (free) to catch a tail swap that kept the same flight number, and keep its position fresh on the map. 0 turns it off.", "Tail and position sources"),
    "live_positions": ("bool", None, None, "Live ADS-B positions", "Use adsb.lol for up-to-the-minute positions on the map while taxiing or airborne.", "Tail and position sources"),
    "nas_minutes": ("int", 2, 60, "FAA NAS status", "Minutes between NAS status checks.", "FAA NAS status"),
    "ios_app_links": ("bool", None, None, "Open alerts in the iPhone app", "Tapping a Home Assistant notification opens the flight in the Flights iPhone app (ericsflights://) instead of this web app. Only turn on with that app installed.", "Notifications"),
    "ha_notify": ("bool", None, None, "Home Assistant app notifications", "Also send alerts through Home Assistant (notify service in the add-on configuration). Turn off once your phone gets them straight from the app (Settings → Notifications on this phone), so you don't get each one twice.", "Notifications"),
}


def init_app_settings():
    """First run: carry over whatever the add-on configuration had; afterwards the app owns them."""
    s = STATE.setdefault("app_settings", {})
    for k in APP_SETTINGS:
        if k not in s:
            s[k] = CFG.get(k, DEFAULTS.get(k))
    flags = STATE.setdefault("flags", {})
    for gone in ("fr24_minutes", "adb_minutes"):  # sources removed in 3.11.0
        s.pop(gone, None)
    CFG.update({k: s[k] for k in APP_SETTINGS})


def clean_app_settings(new):
    out = dict(STATE.get("app_settings") or {})
    for k, v in (new or {}).items():
        spec = APP_SETTINGS.get(k)
        if not spec:
            continue
        kind, lo, hi = spec[0], spec[1], spec[2]
        if kind == "bool":
            out[k] = bool(v)
        else:
            try:
                out[k] = max(lo, min(hi, int(float(v))))
            except (TypeError, ValueError):
                pass
    return out


def init_rules():
    saved = STATE.get("rules") or {}
    flags = STATE.setdefault("flags", {})
    if not flags.get("nas_delay_trend") and isinstance(saved.get("nas_delay"), dict):
        saved["nas_delay"]["on"] = False  # FAA delay figures are trends, not programs: off unless turned back on
    flags["nas_delay_trend"] = True
    STATE["rules"] = rules_mod.merge(saved, CFG)
    sync_rule_cfg()


def sync_rule_cfg():
    """Display thresholds (status label, problem banners) follow the rules."""
    R = STATE["rules"]
    CFG["delay_threshold_minutes"] = R["delay"]["params"]["threshold"]
    CFG["min_turn_minutes"] = R["inbound_late"]["params"]["min_turn"]
    mx = R["inbound_delayed"]["params"].get("max_sched_turn")
    if mx is not None:
        CFG["inbound_alert_minutes"] = mx


ALERT_KEEP = 3000  # notification history (the Flights screen only shows the last few hours)


def push_event(event, title, message, level="info", leg=None, tag=None, out_time=None):
    """A push that isn't tied to one flight's evaluation (schedule, system)."""
    now = time.time()
    ok, _ = rules_mod.allowed(STATE["rules"], event, leg, now, out_time)
    if not ok:
        return
    a = {"key": event, "title": title, "message": message, "level": level, "at": now,
         "tag": tag or f"sys-{event}", "ts": bool(STATE["rules"][event].get("ts"))}
    with LOCK:
        STATE["alerts"] = (STATE["alerts"] + [{**a, "leg": (leg or {}).get("id"), "display": (leg or {}).get("display"),
                                               "route": f"{(leg or {}).get('dep', '')}→{(leg or {}).get('arr', '')}"
                                               if leg else ""}])[-ALERT_KEEP:]
    notify({**a, "leg": (leg or {}).get("id")})


def save_state():
    with LOCK:
        data = json.dumps({k: STATE[k] for k in PERSIST})
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
    """Where a tapped notification should open: the add-on's sidebar panel in the HA app.
    notification_url overrides it; otherwise the panel path is /<add-on slug>."""
    manual = (CFG.get("notification_url") or "").strip()
    INGRESS["full"] = False
    if manual:
        scheme, _, rest = manual.partition("://")
        host, _, path = rest.partition("/")
        path = path.split("#", 1)[0].split("?", 1)[0].strip("/")
        if rest and scheme.lower() not in ("http", "https"):
            # another app's link (shortcuts://run-shortcut?name=…): sent as-is, iOS hands it to that app
            INGRESS["path"], INGRESS["full"] = manual, True
        elif rest and not path:
            # the add-on's own web address (http://homeassistant.local:8099/): opens in Safari, and the
            # tapped flight's page opens directly
            INGRESS["path"], INGRESS["full"] = f"{scheme}://{host}/", True
        elif rest:  # a Home Assistant page address: keep just the panel path, opened in the HA app
            INGRESS["path"] = "/" + path
        else:
            INGRESS["path"] = manual if manual.startswith("/") else "/" + manual
        INGRESS["how"] = "notification_url option"
        log.info("Notifications open %s", INGRESS["path"])
        return
    slug = None
    try:
        info = ha("GET", "/addons/self/info")
        slug = (info or {}).get("data", {}).get("slug")
        INGRESS["how"] = "add-on info"
    except Exception as e:
        log.info("Could not look up the add-on slug (%s); using the container name", e)
    if not slug:
        host = os.environ.get("HOSTNAME", "")
        if host and "-" in host:
            slug = host.replace("-", "_")
            INGRESS["how"] = "container name"
    if slug:
        INGRESS["path"] = f"/{slug}"
    log.info("Notifications open %s", INGRESS.get("path") or "(nothing: set notification_url)")


# Programs and EDCTs touch several of your flights at once (a ground stop at EWR, a revised slot
# list): pushes of those kinds that fire within a few seconds of each other go out as one.
BATCH_KEYS = {"nas_gs", "nas_gdp", "nas_closure", "nas_other", "nas_delay", "edct"}
BATCH_WAIT = 20
_batch = {"items": [], "timer": None}
_batch_lock = threading.Lock()


def notify(alert):
    if alert.get("key") in BATCH_KEYS and not alert.get("_single"):
        with _batch_lock:
            _batch["items"].append(alert)
            if not _batch["timer"]:
                t = threading.Timer(BATCH_WAIT, _flush_batch)
                t.daemon = True
                _batch["timer"] = t
                t.start()
        return True, "queued"
    return _send(alert)


def _flush_batch():
    with _batch_lock:
        items, _batch["items"], _batch["timer"] = _batch["items"], [], None
    groups = {}
    for a in items:
        # one push per program (same title: "EWR: Ground stop", "EWR: ground stop extended"); EDCTs together
        k = (a["key"], "" if a["key"] == "edct" else a["title"])
        groups.setdefault(k, []).append(a)
    for (key, title), lst in groups.items():
        if len(lst) == 1:
            _send(lst[0])
            continue
        lvl = max((a["level"] for a in lst), key=lambda v: ["info", "warn", "critical"].index(v) if v in ("info", "warn", "critical") else 0)
        if key == "edct":
            t = f"EDCTs for {len(lst)} of your flights"
            msg = "\n".join(a["title"] for a in lst)
        else:
            # "UA 2677 BNA→EWR: Ground stop until 16:30 · …" -> the program once, then the flights
            first = lst[0]["message"]
            detail = first.split(": ", 1)[1] if ": " in first else first
            flights = [a["message"].split(": ", 1)[0] for a in lst]
            t = f"{lst[0]['title']} · {len(lst)} of your flights"
            msg = f"{detail}\n{', '.join(flights)}"
        _send({**lst[0], "leg": lst[0].get("leg") if len({a.get("leg") for a in lst}) == 1 else None, "title": t, "message": msg, "level": lvl, "tag": f"batch-{key}",
               "ts": any(a.get("ts") for a in lst)})


# ---------- notifications straight to the phone (Web Push) ----------
# A Home Screen web app subscribes (Settings → Notifications on this phone); alerts then reach it
# directly through Apple's / Google's push service, as "Flighty", and a tap opens the app on that
# flight. Subscriptions are kept in /data; one the phone has dropped is removed on its first failure.
PUSH_PATH = os.path.join(DATA_DIR, "push_subs.json")
_push_lock = threading.Lock()
VAPID = None


def push_init():
    global VAPID
    if webpush is None:
        log.warning("Phone notifications unavailable (the cryptography package is missing)")
        return
    try:
        VAPID = webpush.Vapid(os.path.join(DATA_DIR, "vapid.pem"))
    except Exception as e:
        log.warning("Phone notifications unavailable: %s", e)


def push_subs():
    try:
        with open(PUSH_PATH) as f:
            return json.load(f) or []
    except (OSError, ValueError):
        return []


def push_save(subs):
    with _push_lock:
        tmp = PUSH_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(subs, f)
        os.replace(tmp, PUSH_PATH)


def push_send(alert, only=None):
    """-> (sent, failed) to every subscribed phone (or just `only`, an endpoint)."""
    if not VAPID:
        return 0, 0
    subs = [x for x in push_subs() if not only or x.get("endpoint") == only]
    if not subs:
        return 0, 0
    ts = alert.get("ts", alert.get("level") in ("warn", "critical"))
    payload = {"title": alert["title"], "body": alert["message"], "tag": f"{alert.get('tag')}-{alert.get('key')}",
               "url": f"#/leg/{alert['leg']}" if alert.get("leg") else "#/", "ts": bool(ts), "level": alert.get("level")}
    ok = bad = 0
    drop = set()
    for x in subs:
        try:
            st = webpush.send(VAPID, x, payload, ttl=0 if ts else 3600, urgency="high" if ts else "normal",
                              topic=payload["tag"])
        except Exception as e:
            log.warning("Phone notification failed (%s): %s", (x.get("label") or "phone"), e)
            bad += 1
            continue
        if st in (200, 201, 202):
            ok += 1
        elif st in (404, 410):
            drop.add(x["endpoint"])   # the phone unsubscribed (app removed, notifications turned off)
            bad += 1
        else:
            log.warning("Phone notification to %s: HTTP %s", x.get("label") or "phone", st)
            bad += 1
    if drop:
        push_save([x for x in push_subs() if x.get("endpoint") not in drop])
        log.info("Removed %d phone subscription(s) that ended", len(drop))
    return ok, bad


def _send(alert):
    sent = bad = 0
    try:
        sent, bad = push_send(alert)
        if sent:
            log.info("Pushed to %d phone(s): %s", sent, alert["title"])
    except Exception as e:
        log.warning("Phone notifications: %s", e)
    if not CFG.get("ha_notify", True):
        return (True, f"{sent} phone(s)") if sent or not bad else (False, "phone notification failed")
    ok_ha, detail = _send_ha(alert)
    if sent:
        detail = f"{detail} + {sent} phone(s)"
    return ok_ha or bool(sent), detail


def _send_ha(alert):
    svc = (CFG.get("notify_service") or "").strip()
    if svc.startswith("notify."):
        svc = svc[7:]
    data = {"tag": f"{alert['tag']}-{alert['key']}", "group": "flight-tracker-flighty",
            # Custom notification icon: Android companion today, iOS companion once its custom-icon
            # support ships (home-assistant/iOS#4672). Older apps ignore these keys.
            "notification_icon": "mdi:airplane", "notification_icon_color": "#FFFFFF", "color": "#3B4FB8"}
    if CFG.get("ios_app_links"):
        # the Flights iPhone app (its own link scheme, nothing else answers to it): open on the flight
        url = f"ericsflights://leg/{urllib.parse.quote(str(alert['leg']))}" if alert.get("leg") else "ericsflights://"
        data["url"] = url
        data["clickAction"] = url
    elif INGRESS["path"]:
        url = INGRESS["path"]
        if INGRESS.get("full") and url.endswith("/") and alert.get("leg"):
            url += f"#/leg/{alert['leg']}"  # the web app opens on the flight the push is about
        data["url"] = url
        data["clickAction"] = url
    if alert.get("ts", alert["level"] in ("warn", "critical")):
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
        return True, f"notify.{svc}" if svc else "persistent notification"
    except Exception as e:
        detail = str(e)
        if hasattr(e, "read"):
            try:
                detail += " " + e.read().decode()[:200]
            except Exception:
                pass
        log.warning("Notification failed (%s): %s", svc or "persistent_notification", detail)
        return False, detail


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
            post_state("sensor.flight_tracker_flighty_next_leg", alerting.status_label(nxt, CFG), {
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
                "problems": [p["text"] for p in alerting.problems(nxt, CFG, nas if watched(nxt, now) else [], rules=STATE['rules'])],
                "watching": watched(nxt, now),
                "last_update": iso(nxt.get("updated")),
            })
        else:
            post_state("sensor.flight_tracker_flighty_next_leg", "None", {"friendly_name": "Next flight",
                                                                  "icon": "mdi:airplane-off"})
        probs = []
        for L in upcoming:
            if watched(L, now):
                probs += [f"{L.get('display') or L['dep']}: {p['text']}" for p in alerting.problems(L, CFG, nas, rules=STATE['rules'])]
        post_state("binary_sensor.flight_tracker_flighty_problem", "on" if probs else "off",
                   {"friendly_name": "Flight problem", "device_class": "problem", "problems": probs})
        mine = []
        for L in upcoming:
            if watched(L, now):
                mine += [n for n in alerting.nas_for(L, nas) if n not in mine]
        post_state("sensor.flight_tracker_flighty_nas_programs", len(mine), {
            "friendly_name": "NAS programs at my airports", "icon": "mdi:radar", "unit_of_measurement": "programs",
            "airports": sorted({c for L in upcoming if watched(L, now) for c in (L["dep"], L["arr"]) if c}),
            "programs": [f"{n['airport']} {n['label']}" for n in mine]})
    except Exception as e:
        log.warning("Could not update sensors: %s", e)


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------

def check_schedule_stale(now):
    mt = STATE["schedule"].get("mtime")
    limit = (STATE["rules"]["sys_schedule_stale"]["params"].get("hours") or 24) * HOUR
    flags = STATE.setdefault("flags", {})
    if mt and now - mt > limit:
        if not flags.get("stale"):
            flags["stale"] = True
            push_event("sys_schedule_stale", "Schedule file not updating",
                       f"{CFG['ics_file_path']} last changed {int((now - mt) / HOUR)} h ago. Check the ICS2GCAL add-on.",
                       "warn")
    else:
        flags["stale"] = False


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
        events = schedule.parse_ics(data, HOME_TZ, now, int(CFG.get("schedule_days") or 60))
        legs = schedule.legs_from_events(events, CFG)
    except Exception as e:
        STATE["schedule"].update(error=str(e), checked=now)
        log.error("Schedule: %s", e)
        return
    first_load = STATE["schedule"].get("mtime") is None and not any(
        not x.get("extra") for x in STATE["legs"].values())
    changes = []
    with LOCK:
        cur = STATE["legs"]
        before = {lid: dict(sched_out=x["sched_out"], display=x.get("display"), dep=x.get("dep"), arr=x.get("arr"),
                            dh=x.get("dh"), id=lid)
                  for lid, x in cur.items() if not x.get("extra")}
        seen = set()
        for L in legs:
            seen.add(L["id"])
            L["display"] = display_ident(L["airline"], L["flight_no"])
            old = cur.get(L["id"])
            if old:
                if old.get("ident") != L["ident"] or old.get("dep") != L["dep"]:
                    for k in ("fa_id", "aero", "tail_info", "inbound", "chain"):
                        old.pop(k, None)
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
        matched = {L["id"] for L in legs}
        unmatched = sorted({e["summary"] for e in events if not e.get("allday") and e["oid"] not in matched
                            and e["start"] > now - 2 * HOUR})
        bump()
        STATE["schedule"].update(mtime=mtime, error=None, count=len(legs), checked=now, path=path,
                                 events=len(events), unmatched=unmatched[:25])
        horizon = now + 72 * HOUR
        after = {lid: x for lid, x in cur.items() if not x.get("extra")}
        for lid, x in after.items():
            if now < x["sched_out"] < horizon:
                b = before.get(lid)
                if b is None:
                    changes.append(("added", x, None))
                elif abs(b["sched_out"] - x["sched_out"]) >= 300:
                    changes.append(("retimed", x, b["sched_out"]))
        for lid, b in before.items():
            if lid not in after and now < b["sched_out"] < horizon:
                changes.append(("removed", b, None))
    if not first_load:
        for kind, x, old_out in changes[:10]:
            tz = alerting.tz_for(x.get("dep"))
            when = datetime.fromtimestamp(x["sched_out"], tz).strftime("%a %d %b %H:%M")
            what = f"{x.get('display') or 'Flight'} {x.get('dep')}→{x.get('arr')}"
            if kind == "added":
                push_event("schedule_change", "Schedule: flight added", f"{what} {when}", "warn", leg=x,
                           tag=f"sched-{x['id']}")
            elif kind == "removed":
                push_event("schedule_change", "Schedule: flight removed", f"{what} {when}", "warn", leg=x,
                           tag=f"sched-{x['id']}")
            else:
                push_event("schedule_change", "Schedule: flight retimed",
                           f"{what} {alerting.hhmm(old_out, tz)} → {when}", "warn", leg=x, tag=f"sched-{x['id']}")
    log.info("Schedule: %d flights in the next %s days", len(legs), CFG.get("schedule_days") or 60)


IDENT_RE = re.compile(r"^([A-Z]{2,3}|[A-Z]\d|\d[A-Z])(\d{1,4})[A-Z]?$")


def _iata(code):
    code = (code or "").upper()
    if not code:
        return None
    ap = airports.get(code)
    return (ap.get("iata") or code).upper() if ap else code


def swim_watch(now):
    """Tell the SWIM reader which callsigns (flights being watched and their inbounds) and which
    airports (for surface positions) to keep."""
    if swim.STATE.get("status") == "off":
        return
    keep, aps = set(), set()
    with LOCK:
        for L in STATE["legs"].values():
            if L.get("ident") and watched(L, now):
                keep.add(L["ident"].upper())
                inb = L.get("inbound") or {}
                if inb.get("ident"):
                    keep.add(inb["ident"].upper())
                f = L.get("flight") or {}
                codes = [L.get("dep"), L.get("arr"), (f.get("destination") or {}).get("iata"), f.get("diverted_to")]
                # the airplane's earlier legs: their callsigns (EDCTs, clearances) and airports
                # (surface traffic on an inbound leg's page), until each has landed
                for r in (L.get("rotation") or []) + ([inb] if inb else []):
                    if not r or (((r.get("times") or {}).get("in") or {}).get("actual")):
                        continue
                    if r.get("ident"):
                        keep.add(r["ident"].upper())
                    codes += [(r.get("origin") or {}).get("iata"), (r.get("destination") or {}).get("iata")]
                for code in codes:
                    icao = (airports.get(code) or {}).get("icao") if code else None
                    if icao:
                        aps.add(icao.upper())
    new = keep - swim.WATCH
    swim.WATCH.clear()
    swim.WATCH.update(keep)
    # a flight you just started watching may already have an EDCT from earlier: apply it now
    for cs in new:
        if swim.EDCTS.get(cs):
            swim.PENDING.add(cs)
    swim.WATCH_AIRPORTS.clear()
    swim.WATCH_AIRPORTS.update(aps)
    swim.prune()
    swim.save_edcts()
    swim.save_yours()


SWIM_FAST = set()   # legs whose next update applies SWIM news without loading the FlightAware page


def swim_news(now):
    """Legs to refresh right away because SWIM just reported their (or their inbound's) takeoff,
    landing or diversion: applied within seconds (no page load), at most every 20 s per leg."""
    if not swim.PENDING:
        return set()
    acids = set(swim.PENDING)
    swim.PENDING.difference_update(acids)
    out = set()
    with LOCK:
        for L in STATE["legs"].values():
            idents = {(L.get("ident") or "").upper(), ((L.get("inbound") or {}).get("ident") or "").upper()} | \
                     {(r.get("ident") or "").upper() for r in (L.get("rotation") or []) if r}
            if acids & idents and watched(L, now) and now - (L.get("swim_forced") or 0) >= 20:
                L["swim_forced"] = now
                out.add(L["id"])
                SWIM_FAST.add(L["id"])
    if out:
        log.info("FAA SWIM news for %s: refreshing now", ", ".join(sorted(acids)))
    return out


def _ts(iso):
    """ISO UTC time -> epoch seconds (None when missing or unreadable)."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _airline_src(ev):
    """Messages the airline itself sent carry its 3-letter code as the source (UAL); the FAA's carry
    TFMS or a facility (KZNY)."""
    src = ev.get("src") or ""
    return len(src) == 3 and src.isalpha() and src != "TFM"


def swim_events_for(L, f):
    """This leg's SWIM events (TFMS and TFDM): same callsign, same departure airport, and the FAA's
    first planned departure (igtd) close enough to this leg's schedule. The FAA's automatic
    re-timing of a flight that hasn't left is left out."""
    if not L.get("ident"):
        return []
    dep = _iata(L.get("dep"))
    so = (f.get("times") or {}).get("out", {}).get("scheduled") or L.get("sched_out") or 0
    mine = []
    for ev in swim.events(L["ident"]):
        if _iata(ev.get("dep")) != dep or ev.get("trigger") == "UPDATE_DEPARTURE_TIMEOUT":
            continue
        t = _ts(ev.get("igtd"))
        # igtd is the flight's first planned departure. After a diversion the FAA keeps the
        # original one on the continuation (UA2127 MDT→EWR carries LAX's 17:15Z), so allow
        # it to be well before this leg's scheduled time.
        if t and so and not (so - 18 * HOUR <= t <= so + 6 * HOUR):
            continue
        mine.append(ev)
    return mine


def _hhmm_near(hhmm, ref):
    """'1557' (UTC) -> the timestamp of that time nearest to ref (a clearance is issued within hours)."""
    if not hhmm or not ref or not re.fullmatch(r"\d{4}", hhmm):
        return None
    h, m = int(hhmm[:2]), int(hhmm[2:])
    if h > 23 or m > 59:
        return None
    d = datetime.fromtimestamp(ref, timezone.utc).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
    return min((d - 86400, d, d + 86400), key=lambda x: abs(x - ref))


def _tfms_edct(acid, dep):
    """The FAA's latest EDCT for this flight from the delay-program data (kept for every flight)."""
    best = None
    for x in swim.EDCTS.get((acid or "").upper(), []):
        if dep and _iata(x.get("dep")) != _iata(dep):
            continue
        if not best or (x.get("got") or 0) > (best.get("got") or 0):
            best = x
    return _ts(best.get("edct")) if best else None


def _gs_holding(dep, arr, now):
    """The ground stop at `arr` in effect now that includes departures from `dep`, if any."""
    a = _iata(arr) or (arr or "").upper()
    item = swim.NAS.get((a, "gs")) or swim.NAS.get(((arr or "").upper(), "gs"))
    if not item:
        # SWIM's own list starts empty after a restart until the FAA re-sends the stop (every ~15 min):
        # the merged list (with the NAS status page) has it meanwhile
        item = next((n for n in list(STATE.get("nas") or []) if n.get("kind") == "gs" and n.get("airport") == a), None)
    if not item or not item.get("end") or item["end"] <= now or (item.get("start") or 0) > now:
        return None
    if artcc.in_scope(item, _iata(dep)) is not True:   # only when the stop is known to include you
        return None
    return item


def _swim_take(sw, ev):
    """Fold one event into the leg's saved SWIM facts (newest wins, field by field)."""
    got = ev.get("got") or time.time()
    if ev.get("src") == "TBFM":
        # metering: keep what TBFM says about the flight (release time, runway times, meter fix),
        # and its EDCT when it's newer than the one held (TBFM carries the current one, slot swaps included)
        sw["tbfm"] = {**(sw.get("tbfm") or {}), **(ev.get("tbfm") or {}), "at": got}
        e = _ts(ev.get("edct"))
        # a ground stop holds this departure until its end: a TBFM EDCT before that can't be flown.
        # TBFM keeps repeating the program slot it had (UA2763 EWR-BOS 21 Oct: the GDP slot 19:40, for
        # half an hour and past that time, after the BOS ground stop had moved it to 20:01 then 21:01)
        gs = _gs_holding(ev.get("dep"), ev.get("arr"), time.time())
        if gs and sw.get("edct_src") == "TBFM" and sw.get("edct") and sw["edct"] < gs["end"]:
            # one taken from TBFM before this rule (or before the stop): back to the FAA's own
            f = _tfms_edct(ev.get("acid"), ev.get("dep"))
            if f and f != sw["edct"]:
                sw["tbfm_ignored"] = {"edct": sw["edct"], "at": got, "gs_end": gs["end"], "airport": gs.get("airport"), "new": True}
                sw["edct_prev"] = sw["edct"]
                sw["edct"], sw["edct_at"], sw["edct_new"], sw["edct_tfms"] = f, got, True, True
                sw.pop("edct_src", None)
        if e and e != sw.get("edct") and got > (sw.get("edct_at") or 0):
            if gs and e < gs["end"]:
                if sw.get("tbfm_ignored", {}).get("edct") != e:
                    sw["tbfm_ignored"] = {"edct": e, "at": got, "gs_end": gs["end"], "airport": gs.get("airport"), "new": True}
                return
            sw["edct_prev"] = sw.get("edct")
            sw["edct"], sw["edct_at"], sw["edct_new"], sw["edct_src"] = e, got, True, "TBFM"
            sw["edct_for"] = sw.get("edct_for") or _iata(ev.get("arr"))
        return
    if ev.get("src") == "STDDS":
        sf = ev.get("surface")
        if sf:
            ap = _iata(sf.get("airport"))
            side = "dep" if ap and ap == _iata(ev.get("dep")) else "arr"
            cur = sw.setdefault("surf_" + side, {"airport": ap})
            for e in sf.get("events") or []:
                t = _ts(e.get("at"))
                if not t or not e.get("event"):
                    continue
                k = {"on": "landed", "off": "took_off", "spotin": "ramp_in", "spotout": "ramp_out"}.get(e["event"])
                if k and not cur.get(k):
                    cur[k] = t
                    if e.get("runway"):
                        cur[k + "_rwy"] = e["runway"]
                if e["event"] == "runwayout" and cur.get("landed") and not cur.get("runway_exit") and t >= cur["landed"]:
                    cur["runway_exit"] = t
            if sf.get("status"):
                cur["status"] = sf["status"]
            if sf.get("hex"):
                sw["hex"] = sf["hex"]
            cur["at"] = got
        tw = ev.get("tower")
        if tw:
            sw["tower_dep"] = {"clearance": _ts(tw.get("clearance")), "gate": tw.get("gate"), "at": got}
        if ev.get("pdc"):
            # a later datalink message (an EDCT revision, the pilot's WILCO) adds to the clearance
            sw["pdc"] = {**(sw.get("pdc") or {}), **ev["pdc"], "got": got, "v": 3}
            e = _hhmm_near(ev["pdc"].get("edct"), _ts(ev["pdc"].get("at")) or got)
            newer = (_ts(ev["pdc"].get("at")) or got) > (sw.get("edct_at") or 0)
            if e and e != sw.get("edct") and (not sw.get("edct_tfms") or newer):
                # the EDCT printed in the clearance, when the FAA's own assignment didn't reach us
                sw["edct_prev"] = sw.get("edct")
                sw["edct"], sw["edct_at"], sw["edct_for"], sw["edct_new"] = e, got, None, True
                sw["edct_src"] = "clearance"
        return
    if ev.get("src") == "TFDM":
        td = ev.get("tfdm") or {}
        for side in ("dep", "arr"):
            cur = sw.setdefault("tfdm_" + side, {})
            for k, v in (td.get(side) or {}).items():
                cur[k] = {**cur.get(k), **v} if isinstance(v, dict) and isinstance(cur.get(k), dict) else v
        if td.get("state"):
            sw["tfdm_state"] = td["state"]
        sw["tfdm_at"] = got
        dep_rwy = (sw.get("tfdm_dep") or {}).get("runwayDepartureTime") or {}
        arr_rwy = (sw.get("tfdm_arr") or {}).get("runwayArrivalTime") or {}
        if dep_rwy.get("actual"):
            sw["off"] = sw.get("off") or _ts(dep_rwy["actual"])
        if arr_rwy.get("actual"):
            sw["on"] = sw.get("on") or _ts(arr_rwy["actual"])
        if arr_rwy.get("estimated") and (td.get("arr") or {}).get("runwayArrivalTime"):
            sw["tfdm_eta"], sw["tfdm_eta_at"] = _ts(arr_rwy["estimated"]), got
            sw["tfdm_eta_rep"] = _ts(ev.get("at")) or got   # when the tower system worked it out
        return
    airline = _airline_src(ev)
    if ev.get("off") and not airline:
        sw["off"] = _ts(ev["off"])
    if ev.get("on") and not airline:
        sw["on"] = _ts(ev["on"])
    if ev.get("eta") and not airline:
        sw["eta"], sw["eta_at"] = _ts(ev["eta"]), got
    if ev.get("arr") and not airline:
        sw["arr"] = _iata(ev["arr"])
    if ev.get("diverted"):
        sw["diverted"] = True
    if ev.get("edct"):
        e = _ts(ev["edct"])
        if e and e != sw.get("edct"):
            sw["edct_prev"] = sw.get("edct")
            sw["edct"], sw["cta"], sw["edct_at"] = e, _ts(ev.get("cta")), got
            sw["edct_for"] = _iata(ev.get("element")) if ev.get("element") else None
            sw["edct_new"] = True
            sw["edct_tfms"] = True
            sw.pop("edct_src", None)   # the FAA's own assignment
    pe = _ts(ev.get("etd_proposed"))
    if (pe and sw.get("edct") and abs(pe - sw["edct"]) >= 120 and pe != _ts(ev.get("igtd"))
            and (_ts(ev.get("at")) or got) > (sw.get("edct_at") or 0) and not ev.get("edct")):
        # Holding an EDCT, and the flight plan now says another departure time: the slot moved (an
        # airline slot swap, a revision) and only the refiled plan reached us. United refiles to the
        # EDCT it holds (UA1577: FAA's 8:27 PM, swapped to 7:59 then 7:42; the 7:42 showed here).
        sw["edct_prev"] = sw["edct"]
        sw["edct"], sw["edct_at"], sw["edct_new"], sw["edct_src"] = pe, got, True, "flight plan"
    if ev.get("traj"):
        sw["traj"] = [[p[0], p[1]] for p in ev["traj"]]
        sw["traj_fixes"] = ev.get("traj_fixes") or []
        sw["traj_at"] = got
    if ev.get("route"):
        txt = swim.route_text(ev["route"])
        if txt and txt != sw.get("route"):
            if sw.get("route"):
                sw["route_prev"], sw["route_changed"] = sw["route"], got
            sw["route"], sw["route_at"] = txt, got
    if airline and ev.get("airline"):
        a = ev["airline"]
        u = sw.setdefault("airline", {})
        for k in ("gate_out", "runway_off", "gate_in", "runway_on", "in", "on", "sched_out", "sched_in"):
            if a.get(k):
                u[k] = _ts(a[k])
        u["src"], u["at"] = ev["src"], got
        if ev.get("status"):
            u["status"] = ev["status"]
        if ev.get("remarks"):
            u["remarks"] = ev["remarks"]


def _tower_state(state):
    """TFDM flight state -> words: AMA_TAXI_IN -> 'taxiing in' (AMA/NMA: movement / ramp area)."""
    s = re.sub(r"^(AMA|NMA)_", "", state or "").lower().replace("_", " ")
    return {"taxi in": "taxiing in", "taxi out": "taxiing out", "on final": "on final", "en route": "en route",
            "in block": "at the gate", "out block": "pushed back"}.get(s, s)


def swim_check(L, f, planned):
    """Read this leg's new SWIM messages into its saved SWIM facts (L['swim'], kept across restarts
    and however long the flight), log what came in, and report a diversion.
    -> {diverted_to, new, off, on, eta} (times as ISO)."""
    out = {}
    if not L.get("ident") or swim.STATE.get("status") == "off":
        return out
    mine = swim_events_for(L, f)
    sw = L.setdefault("swim", {})
    pd = sw.get("pdc")
    if pd and pd.get("text") and pd.get("v") != 3:
        # saved before the parser knew this tower's format: read the saved text again
        sw["pdc"] = {**pd, **swim.parse_pdc(pd["text"]), "v": 3}
    seen_at = max(L.get("swim_seen") or 0, sw.get("seen") or 0)
    fresh = [e for e in mine if (e.get("got") or 0) > seen_at]
    for e in fresh:
        _swim_take(sw, e)
    # the delay program that set the EDCT was cancelled after it: the EDCT no longer applies
    ef = sw.get("edct_for")
    if sw.get("edct") and ef and (swim.NAS_CANCELLED.get((ef, "gdp")) or 0) > (sw.get("edct_at") or 0):
        sw["edct_prev"] = sw.pop("edct")
        sw.pop("edct_tfms", None)
        sw.pop("edct_new", None)
        saw(L, "FAA SWIM", None, f"{ef} delay program cancelled: the EDCT no longer applies")
    # The tower's own system (TFDM) targets the takeoff well before the EDCT, after the EDCT came in:
    # the flight was released from the program. Only its target time counts (it builds the EDCT in);
    # its "earliest possible" takeoff doesn't (a flight with a 7:42 slot can be ready at 7:07).
    if sw.get("edct") and (sw.get("tfdm_at") or 0) > (sw.get("edct_at") or 0):
        tdd = sw.get("tfdm_dep") or {}
        rdt = tdd.get("runwayDepartureTime") or {}
        tgt = _ts(rdt.get("target")) if isinstance(rdt.get("target"), str) else None
        if tgt and tgt < sw["edct"] - 15 * 60 and tdd.get("smpExemptionReason") != "EDCT":
            tza_ = alerting.tz_for(L.get("dep"), f.get("origin") or {})
            saw(L, "FAA SWIM", None, f"Tower targets takeoff {alerting.hhmm(tgt, tza_)}, well before the "
                f"{alerting.hhmm(sw['edct'], tza_)} EDCT: released from the program, the EDCT no longer applies")
            sw["edct_prev"] = sw.pop("edct")
            sw.pop("edct_tfms", None)
            sw.pop("edct_new", None)
            sw["edct_released"] = time.time()
    if fresh:
        out["new"] = True
        sw["seen"] = L["swim_seen"] = max(e.get("got") or 0 for e in fresh)
        # In plain words, local times: who said what.
        tza = alerting.tz_for(L.get("dep"), f.get("origin") or {})
        tzb = alerting.tz_for(L.get("arr"), f.get("destination") or {})
        hm = lambda iso, tz: alerting.hhmm(_ts(iso), tz) if _ts(iso) else "?"
        dep, arr_ = _iata(L.get("dep")), _iata(L.get("arr"))
        bits = []
        faa = [x for x in fresh if not _airline_src(x) and x.get("src") != "TFDM"]
        v = next((x["off"] for x in reversed(faa) if x.get("off")), None)
        if v:
            bits.append(f"FAA: took off {hm(v, tza)}")
        v = next((x["on"] for x in reversed(faa) if x.get("on")), None)
        if v:
            bits.append(f"FAA: landed {hm(v, tzb)}")
        v = next((x["eta"] for x in reversed(faa) if x.get("eta")), None)
        if v and not any(x.get("on") for x in faa):
            bits.append(f"FAA radar: landing est. {hm(v, tzb)}")
        al = next((x for x in reversed(fresh) if x.get("airline") and _airline_src(x)), None)
        if al:
            a_ = al["airline"]
            name = {"UAL": "United", "AAL": "American", "DAL": "Delta", "SWA": "Southwest"}.get(al["src"], al["src"])
            parts = [f"{lbl} {hm(a_[k], tz)}" for k, lbl, tz in (("gate_out", "pushed", tza), ("runway_off", "took off", tza),
                                                             ("in", "gate arrival est.", tzb), ("gate_in", "at gate", tzb)) if a_.get(k)]
            if parts:
                bits.append(f"{name}: " + ", ".join(parts))
        for x in reversed(fresh):
            if x.get("src") != "TFDM":
                continue
            td = x.get("tfdm") or {}
            ea = ((td.get("arr") or {}).get("runwayArrivalTime") or {})
            ed = ((td.get("dep") or {}).get("runwayDepartureTime") or {})
            if ea.get("actual"):
                bits.append(f"{arr_} tower: landed {hm(ea['actual'], tzb)}")
            elif ea.get("estimated"):
                bits.append(f"{arr_} tower: landing est. {hm(ea['estimated'], tzb)}")
            if ed.get("actual"):
                bits.append(f"{dep} tower: took off {hm(ed['actual'], tza)}")
            elif ed.get("estimated"):
                bits.append(f"{dep} tower: takeoff est. {hm(ed['estimated'], tza)}")
            if td.get("state"):
                bits.append(f"tower status {_tower_state(td['state'])}")
            break
        for x in fresh:
            for e in (x.get("surface") or {}).get("events") or []:
                ap_ = _iata((x.get("surface") or {}).get("airport"))
                tz_ = tza if ap_ == dep else tzb
                w = {"on": "landed", "off": "took off", "spotin": "on the ramp", "spotout": "left the ramp"}.get(e.get("event"))
                if w and e.get("at"):
                    bits.append(f"{ap_} surface radar: {w}{' ' + e['runway'] if e.get('runway') and e['event'] in ('on', 'off') else ''} {hm(e['at'], tz_)}")
            pd = x.get("pdc")
            if pd:
                parts = [("route_change", pd.get("route_change") and pd.get("route") and f"{pd['route_change']} route {pd['route']}"),
                         ("cleared_to", pd.get("cleared_to") and f"cleared to {pd['cleared_to']}"),
                         ("departure", pd.get("departure") and f"via {pd['departure']}"),
                         ("altitude", pd.get("altitude") and (pd["altitude"] if not pd["altitude"].isdigit() else f"maintain {pd['altitude']}")),
                         ("expect_alt", pd.get("expect_alt") and f"expect FL{pd['expect_alt']}"),
                         ("runway", pd.get("runway") and f"runway {pd['runway']}"),
                         ("gate", pd.get("gate") and f"gate {pd['gate']}"),
                         ("dep_freq", pd.get("dep_freq") and f"departure {pd['dep_freq']}"),
                         ("edct", pd.get("edct") and f"EDCT {pd['edct']}Z"),
                         ("tail", pd.get("tail") and f"airplane {pd['tail']}"),
                         ("response", pd.get("response") and f"crew answered {pd['response'].lower()}")]
                # a later message repeats the clearance with one thing added (the crew's ROGER, a new
                # EDCT): log only what's new
                done = sw.setdefault("pdc_logged", {})
                new = [txt for k, txt in parts if txt and done.get(k) != txt]
                for k, txt in parts:
                    if txt:
                        done[k] = txt
                if new:
                    bits.append(f"{dep} clearance by datalink: " + ", ".join(new))
            tw = x.get("tower")
            if tw and tw.get("clearance"):
                bits.append(f"{dep} tower: departure clearance {hm(tw['clearance'], tza)}" + (f", gate {tw['gate']}" if tw.get("gate") else ""))
        ig = sw.get("tbfm_ignored") or {}
        if ig.pop("new", False):
            bits.append(f"TBFM (metering): EDCT {alerting.hhmm(ig['edct'], tza)} not used, the {ig.get('airport') or ''} "
                        f"ground stop holds departures until {alerting.hhmm(ig['gs_end'], tza)}")
        if sw.get("edct_new"):
            prev = f" (was {alerting.hhmm(sw['edct_prev'], tza)})" if sw.get("edct_prev") else ""
            who = {"TBFM": "TBFM (metering)", "flight plan": "Refiled flight plan", "clearance": "Datalink clearance"}.get(sw.get("edct_src"), "FAA")
            bits.append(f"{who}: EDCT {alerting.hhmm(sw['edct'], tza)}{prev}"
                        + (f" for {sw['edct_for']}" if sw.get("edct_for") else ""))
        if any(x.get("route") for x in fresh) and sw.get("route_changed", 0) > seen_at:
            bits.append(f"ATC: new route {sw.get('route')}")
        if any(x.get("diverted") for x in fresh):
            bits.append("FAA: diversion flagged")
        if sw.get("arr") and planned and sw["arr"] != _iata(planned):
            bits.append(f"FAA: now going to {sw['arr']}")
        # only what's new since the last line: the same estimate every minute isn't news
        last = set(sw.get("logged") or [])
        new_bits = [b for b in dict.fromkeys(bits) if b not in last]
        sw["logged"] = list(dict.fromkeys(bits))[-20:]
        if new_bits:
            saw(L, "FAA SWIM", None, "; ".join(new_bits))
    if sw.pop("edct_new", False) and not L.get("_quiet") and fa.phase(f, time.time()) in ("scheduled", "taxi_out"):
        tza = alerting.tz_for(L.get("dep"), f.get("origin") or {})
        so_ = ((f.get("times") or {}).get("off") or {}).get("scheduled") or L.get("sched_out")
        late = f" · {round((sw['edct'] - so_) / 60)} min after scheduled takeoff" if so_ and sw["edct"] > so_ + 60 else ""
        prev = f" (was {alerting.hhmm(sw['edct_prev'], tza)})" if sw.get("edct_prev") else ""
        push_event("edct", f"{L.get('display') or L['ident']} EDCT {alerting.hhmm(sw['edct'], tza)}{prev}",
                   f"{L.get('dep')}→{L.get('arr')}: expect departure clearance {alerting.hhmm(sw['edct'], tza)}"
                   + (f" ({sw['edct_for']} program)" if sw.get("edct_for") else "") + late,
                   level="warn", leg=L, tag=f"edct-{L.get('id')}")
    if not sw.get("seen"):
        L.pop("swim", None)
        return out
    arr = sw.get("arr")
    if planned and arr and arr != _iata(planned):
        out["diverted_to"] = arr
    for k in ("off", "on", "eta"):
        if sw.get(k):
            out[k] = datetime.fromtimestamp(sw[k], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


ROT_LOG_KEEP = 80


def rotation_swim(L, now):
    """The FAA's SWIM data for the airplane's earlier legs (your inbound, its inbound, ...), read and
    applied exactly as for your own flight: the same events, the same priority (surface radar, the
    tower, the FAA, the airline over FlightAware) and the same log of who said what. Each leg's facts
    and log are kept in L['rot_swim'] by its FlightAware id and dropped 15 minutes after it's at the
    gate (FlightAware has the final times by then)."""
    if swim.STATE.get("status") == "off":
        return
    store = L.setdefault("rot_swim", {})
    legs, seen_obj, keys = [], set(), set()
    for h in [L.get("inbound")] + list(L.get("rotation") or []):
        if h and id(h) not in seen_obj and h.get("ident"):
            seen_obj.add(id(h))
            legs.append(h)
    for h in legs:
        t = h.get("times") or {}
        key = h.get("fa_id") or f"{h['ident']}-{(t.get('out') or {}).get('scheduled')}"
        keys.add(key)
        st = store.setdefault(key, {"swim": {}, "seen": 0, "log": []})
        if st.get("done"):
            continue
        o, d = h.get("origin") or {}, h.get("destination") or {}
        Lh = {"id": f"{L['id']}~{key}", "ident": h["ident"], "display": h.get("display_ident") or h["ident"],
              "dep": o.get("iata") or o.get("icao"), "arr": d.get("iata") or d.get("icao"),
              "sched_out": (t.get("out") or {}).get("scheduled"), "swim": st["swim"], "swim_seen": st["seen"],
              "source": "FlightAware", "_quiet": True}
        try:
            res = swim_check(Lh, h, Lh["arr"])
            st["swim"], st["seen"] = Lh.get("swim") or {}, Lh.get("swim_seen") or st["seen"]
            for x in Lh.pop("_seen", None) or []:
                if x.get("note"):
                    st["log"].append({"at": now, "src": x.get("src") or "FAA SWIM", "text": x["note"]})
            del st["log"][:-ROT_LOG_KEEP]
            if st["swim"]:
                swim_overlay(Lh, h, now)
            if res.get("diverted_to"):
                fa.fix_diversion(h, Lh["arr"], res["diverted_to"])
        except Exception as e:
            log.debug("inbound SWIM %s: %s", h.get("ident"), e)
        h["rot_key"] = key
        gin = ((h.get("times") or {}).get("in") or {}).get("actual")
        if gin and now - gin > 15 * 60:
            st["done"] = now
            st.pop("swim", None)
            st["log"] = []
    for k in [k for k in store if k not in keys]:
        del store[k]


def swim_overlay(L, f, now):
    """Put the FAA's and the airline's own times over FlightAware's: FAA takeoff and landing, the
    airline's actual push and gate arrival, the best landing estimate in the air (the tower's at
    TFDM airports, else the FAA's), the tower's takeoff estimate on the ground, and the route ATC
    has on file. Adds f['swim'] for the details page."""
    sw = L.get("swim") or {}
    if not sw or not f:
        return
    t = f.setdefault("times", {})
    for k in fa.TIME_KEYS:
        t.setdefault(k, {"scheduled": None, "estimated": None, "actual": None})
    base = f.get("time_src") or {}
    src = {k: {kind: (base.get(k) or {}).get(kind) or L.get("source") or "FlightAware"
               for kind in ("actual", "estimated") if t[k].get(kind)} for k in fa.TIME_KEYS}
    f["time_src"] = src
    a = sw.get("airline") or {}
    airline = {"UAL": "United", "AAL": "American", "DAL": "Delta"}.get(a.get("src"), a.get("src") or "airline")
    sd, sa = sw.get("surf_dep") or {}, sw.get("surf_arr") or {}
    # Wheels up / down, best first: surface radar (to the second), the FAA, the airline's report.
    for key, cands in (("off", ((sd.get("took_off"), f"{sd.get('airport')} surface radar"), (sw.get("off"), "FAA"),
                                (a.get("runway_off"), airline))),
                       ("on", ((sa.get("landed"), f"{sa.get('airport')} surface radar"), (sw.get("on"), "FAA"),
                               (a.get("runway_on"), airline)))):
        v, who = next(((v, w) for v, w in cands if v and v <= now + 60), (None, None))
        if v:
            t[key]["actual"] = v
            src[key]["actual"] = who
    if a.get("gate_out") and a["gate_out"] <= now + 60:
        t["out"]["actual"] = a["gate_out"]
        src["out"]["actual"] = airline
    if a.get("gate_in") and a["gate_in"] <= now + 60:
        t["in"]["actual"] = a["gate_in"]
        src["in"]["actual"] = airline
    td_dep, td_arr = sw.get("tfdm_dep") or {}, sw.get("tfdm_arr") or {}
    # In the air, only estimates worked out after takeoff: the tower's last one from before
    # departure still assumes the scheduled time (EWR→IAD showed 00:28, then 00:04 a minute later).
    off_a = t["off"].get("actual") or 0
    fresh_tfdm = (sw.get("tfdm_eta") and now - (sw.get("tfdm_eta_at") or 0) < 15 * 60
                  and (sw.get("tfdm_eta_rep") or sw.get("tfdm_eta_at") or 0) >= off_a - 60)
    fresh_faa = sw.get("eta") and now - (sw.get("eta_at") or 0) < 30 * 60 and (sw.get("eta_at") or 0) >= off_a - 60
    eta_src = None
    if t["off"].get("actual") and not t["on"].get("actual") and (fresh_tfdm or fresh_faa):
        eta, eta_src = (sw["tfdm_eta"], "tower") if fresh_tfdm else (sw["eta"], "FAA")
        on_e, in_e = t["on"].get("estimated"), t["in"].get("estimated")
        taxi = ((td_arr.get("arrivalTaxiTime") or {}).get("totalEstimatedTaxiInTime") or 0) * 60
        gap = (in_e - on_e) if on_e and in_e else taxi or 8 * 60
        t["on"]["estimated"] = eta
        who = f"{L.get('arr')} tower" if eta_src == "tower" else "FAA radar"
        src["on"]["estimated"] = who
        if not t["in"].get("actual"):
            new_in = eta + max(2 * 60, min(45 * 60, gap))
            # hold the shown arrival unless it moves 2+ minutes: the estimates wobble by a minute
            # every report, and each wobble would be a new line in the log
            shown = sw.get("in_shown")
            if shown and abs(new_in - shown) < 120:
                new_in = shown
            sw["in_shown"] = new_in
            t["in"]["estimated"] = new_in
            src["in"]["estimated"] = f"{who} + taxi"
    if t["on"].get("actual") and not t["in"].get("actual"):
        # Landed, taxiing in: the airline's gate estimate once it has updated after landing (United's
        # was right to the minute at IAD), else hold the last arrival we showed rather than drop
        # back to FlightAware's (which jumped 00:15 → 00:18 → 00:15 there).
        land = t["on"]["actual"]
        if a.get("in") and (a.get("at") or 0) >= land - 60 and a["in"] >= land:
            t["in"]["estimated"], src["in"]["estimated"] = a["in"], airline
        elif sw.get("in_shown") and sw["in_shown"] >= land:
            t["in"]["estimated"] = sw["in_shown"]
            src["in"]["estimated"] = (src.get("in") or {}).get("estimated") or "estimate before landing"
    for side, key, slot in ((td_dep, "offBlockTime", "out"), (td_arr, "inBlockTime", "in")):
        v = _ts(((side.get(key) or {}) if isinstance(side.get(key), dict) else {}).get("actual"))
        if v and v <= now + 60 and not t[slot].get("actual"):
            t[slot]["actual"] = v
            src[slot]["actual"] = "tower"
    # The EDCT is shown under the takeoff time, not in place of it: the tower (or the airline) can
    # predict later than the slot (a 14:58 push for a 14:59 EDCT), and both are worth seeing.
    est_off = (td_dep.get("runwayDepartureTime") or {}).get("estimated")
    if est_off and not t["off"].get("actual"):
        t["off"]["estimated"] = _ts(est_off)
        src["off"]["estimated"] = f"{L.get('dep')} tower"
    if sw.get("route"):
        if f.get("route_source") != "FAA":
            f["route_flightaware"] = f.get("route")
        f["route"], f["route_source"] = sw["route"], "FAA"
    delay = td_dep.get("departureDelay") or {}
    taxi_out = (td_dep.get("departureTaxiTime") or {}).get("totalEstimatedTaxiOutTime")
    f["swim"] = {
        "route_at": sw.get("route_at"), "route_prev": sw.get("route_prev"), "route_changed": sw.get("route_changed"),
        "off": sw.get("off"), "on": sw.get("on"), "eta_src": eta_src,
        "airline": {k: a.get(k) for k in ("src", "gate_out", "in", "gate_in", "status", "at")} if a else None,
        "dep": {"runway": td_dep.get("runwayAssigned") or td_dep.get("runwayPredicted"), "fix": td_dep.get("departureFix"),
                "delay": delay.get("predictedDelay") if delay.get("predictedDelay") is not None else delay.get("currentDelay"),
                "est_off": _ts(est_off), "taxi": taxi_out,
                "spot": td_dep.get("predictedDepartureSpot")} if td_dep else None,
        "arr": {"runway": td_arr.get("runwayAssigned") or td_arr.get("runwayPredicted"),
                "est_on": sw.get("tfdm_eta"), "at": sw.get("tfdm_eta_at"),
                "taxi": (td_arr.get("arrivalTaxiTime") or {}).get("totalEstimatedTaxiInTime")} if td_arr else None,
        "state": sw.get("tfdm_state"),
        "surf_dep": sd or None, "surf_arr": sa or None, "tower_dep": sw.get("tower_dep"),
        "pdc": sw.get("pdc"),
        # metering (TBFM): release time (ctm), the EDCT it holds (etm), meter fix, runway, scheduled times
        "tbfm": sw.get("tbfm"),
        "edct_src": sw.get("edct_src"),
        "edct": {"at": sw.get("edct"), "cta": sw.get("cta"), "prev": sw.get("edct_prev"), "for": sw.get("edct_for")}
        if sw.get("edct") else
        # the departure tower's system knows the flight has one even when the time itself hasn't reached us
        {"at": None, "tower": True} if (sw.get("tfdm_dep") or {}).get("smpExemptionReason") == "EDCT" else None,
    }
    if sw.get("traj"):
        # the FAA's trajectory along the route ATC has on file (updated with every amendment)
        f["swim_route"] = {"pts": sw["traj"], "fixes": sw.get("traj_fixes") or [], "at": sw.get("traj_at")}


def route_end(route):
    """The airport a filed route ends at (IATA), when it names one: 'KLAX DCT RIC261040 DCT KMDT' → MDT.
    Routes that end at an arrival procedure ('… SLT FQM3') give None."""
    toks = [t for t in re.split(r"\s+", (route or "").strip().upper()) if t and t != "DCT"]
    if len(toks) < 2:
        return None
    last = toks[-1].split("/")[0]
    if not re.fullmatch(r"[A-Z]{4}", last):
        return None
    ap = airports.get(last)
    return (ap.get("iata") or None) if ap and ap.get("icao") == last else None


MANUAL_AERO = {}   # leg id -> (when, answer) of the last "ask AeroAPI now"


def manual_aero(lid):
    """Settings-free 'ask AeroAPI now' for one flight (counts against the monthly budget like any call,
    but not the per-flight limit)."""
    if not aero_on():
        return 400, {"error": "AeroAPI isn't set up (add aeroapi_key in the add-on Configuration)."}
    with LOCK:
        L = STATE["legs"].get(lid)
        if not L:
            return 404, {"error": "not found"}
        L = json.loads(json.dumps(L))
    now = time.time()
    # the phone retries once when its connection drops mid-request: the answer from a check made in
    # the last minute is that answer, not a second paid query
    last = MANUAL_AERO.get(lid)
    if last and now - last[0] < 60:
        return last[1]
    a = L.setdefault("aero", {})
    limit = int(CFG.get("aeroapi_max_per_leg") or 6)
    a["calls"] = min(a.get("calls", 0), limit - 1)  # a manual check always gets through
    prev = snapshot(L)
    got = aero_call(L, now, "manual check")
    if not got:
        return 502, {"error": (L.get("aero") or {}).get("error") or "AeroAPI didn't return this flight."}
    with LOCK:
        cur = STATE["legs"].get(lid)
        if cur is None:
            return 404, {"error": "not found"}
        cur["aero"] = L["aero"]
        if L.get("tail_info"):
            cur["tail_info"] = L["tail_info"]
        log_lookup(cur, snapshot(cur), now, ["AeroAPI"],
                   seen=[{"src": "AeroAPI · manual check", "tail": (got.get("tail") or "").upper() or None,
                          "note": " · ".join(x for x in (got.get("status_text"), (got.get("destination") or {}).get("iata") and
                                                         f"to {(got.get('destination') or {}).get('iata')}") if x) or None}])
    REFRESH_Q.put(lid)  # fold it into a full check right away
    WAKE.set()
    bump()
    out = (200, {"ok": True, "status": got.get("status_text"), "tail": got.get("tail")})
    MANUAL_AERO[lid] = (time.time(), out)
    return out


def history_lookup(browser, L, ident, lid):
    """Find a followed flight through FlightAware's history list (…/UAL2608/history), then load its
    own page (…/history/20261017/0123Z/KABE/KEWR) and pin it."""
    dep_i = (airports.get(L["dep"]).get("icao") or "").upper()
    arr_i = (airports.get(L["arr"]).get("icao") or "").upper()
    if not dep_i or not arr_i:
        return None
    try:
        links = browser.links(fa.history_url(ident), "/history/")
    except Exception as e:
        log.info("%s history list: %s", L.get("display"), e)
        return None
    url = fa.pick_history_link(links, dep_i, arr_i, L.get("date"))
    with LOCK:
        STATE["debug"] = (STATE["debug"] + [{"at": time.time(), "leg": L.get("display"), "leg_id": lid,
                                              "what": "FlightAware history list", "url": fa.history_url(ident),
                                              "found": url or f"no {dep_i}→{arr_i} leg listed"}])[-60:]
    if not url:
        return None
    boot, _ = load(browser, url, lid, L, "flight (from history list)")
    cur, _ = fa.flights_from_bootstrap(boot)
    if cur and cur.get("fa_id"):
        L["fa_id"] = cur["fa_id"]
    return cur


def planned_arr(L):
    """Where this flight was supposed to go: your calendar's arrival, or what you entered when you
    added it (a followed flight's arrival is otherwise overwritten by wherever it actually goes)."""
    if not L.get("extra"):
        return L.get("arr")
    if L.get("planned_arr"):
        return L["planned_arr"]
    for m in STATE.get("manual") or []:
        if m.get("arr") and extra_id(m.get("ident") or "", m.get("dep") or "", m.get("arr") or "", m.get("date") or "") == L.get("id"):
            L["planned_arr"] = m["arr"]
            return m["arr"]
    return None


def extra_id(ident, dep="", arr="", date=""):
    parts = [re.sub(r"[^A-Z0-9]", "", ident), dep, arr, date.replace("-", "")]
    return "x-" + "-".join(p for p in parts if p)


def _ap_code(code):
    code = (code or "").strip().upper()
    if len(code) == 4:
        return airports.iata(code) or code
    return code


def _x(x):
    """Normalise a followed-flight entry from the config or the app."""
    if not isinstance(x, dict):
        x = {"ident": str(x)}
    return {"ident": (x.get("ident") or "").strip().upper().replace(" ", ""),
            "dep": _ap_code(x.get("dep") or x.get("from")), "arr": _ap_code(x.get("arr") or x.get("to")),
            "date": (x.get("date") or "").strip()}


def _kind(x):
    """Flights added in the app are yours (manual: tracked and kept in History) or a friend's."""
    return "manual" if (x or {}).get("kind") == "manual" else "friend"


def sync_extras():
    """Followed flights: track_flights from the config plus flights added in the app."""
    wanted = {}
    for raw in CFG.get("track_flights") or []:
        x = _x(raw)
        if x["ident"]:
            wanted[extra_id(**x)] = (dict(x, kind="friend"), False)
    now = time.time()
    with LOCK:
        for raw in STATE["manual"]:
            x = _x(raw)
            wanted[extra_id(**x)] = (dict(x, kind=_kind(raw)), True)
        for lid in [k for k, L in STATE["legs"].items() if L.get("extra") and k not in wanted]:
            del STATE["legs"][lid]
        for lid, (x, manual) in wanted.items():
            if lid in STATE["legs"]:
                STATE["legs"][lid]["kind"] = x.get("kind") or "friend"
                continue
            m = IDENT_RE.match(x["ident"])
            airline = schedule.IATA_TO_ICAO.get(m.group(1), m.group(1)) if m else ""
            start = now
            if x["date"]:  # until FlightAware answers, sort it at midday on that date
                try:
                    tz = ZoneInfo(airports.get(x["dep"]).get("tz") or TZNAME)
                    start = datetime.strptime(x["date"] + " 12:00", "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp()
                except Exception:
                    pass
            STATE["legs"][lid] = {
                "id": lid, "extra": True, "manual": manual, "date": x["date"], "kind": x.get("kind") or "friend",
                "ident": (airline + m.group(2)) if m else x["ident"],
                "display": display_ident(airline, m.group(2)) if m else x["ident"], "airline": airline,
                "flight_no": m.group(2) if m else "", "dep": x["dep"], "arr": x["arr"], "dh": False,
                "sched_out": start, "sched_in": start, "summary": "Added in the app" if manual else "track_flights",
                "notified": {"watch": True}, "alerts": [], "next_check": 0}


def add_manual(ident, dep, arr, date, kind="manual"):
    x = _x({"ident": ident, "dep": dep, "arr": arr, "date": date})
    ident, dep, arr, date = x["ident"], x["dep"], x["arr"], x["date"]
    if not IDENT_RE.match(ident):
        raise ValueError("Enter a flight number like UA1234 or UAL1234")
    if not re.fullmatch(r"[A-Z0-9]{3,4}", dep or "") or not re.fullmatch(r"[A-Z0-9]{3,4}", arr or ""):
        raise ValueError("Enter the From and To airports, like EWR and SFO")
    if dep == arr:
        raise ValueError("From and To can't be the same airport")
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Date must be YYYY-MM-DD")
    with LOCK:
        lid = extra_id(ident, dep, arr, date)
        if not any(extra_id(**_x(m)) == lid for m in STATE["manual"]):
            STATE["manual"].append({"ident": ident, "dep": dep, "arr": arr, "date": date, "added": time.time(),
                                    "kind": _kind({"kind": kind})})
    sync_extras()
    return lid


# ---------------------------------------------------------------- add a flight: number + date -> pick
# One AeroAPI query returns every flight with that number over the day, fully detailed. The one you
# pick becomes the tracker's first lookup (no second query). Kept 10 minutes so picking reuses it.
LOOKUPS = {}    # (ident, date) -> {"at", "flights"}
PRESEED = {}    # leg id -> the AeroAPI flight picked from a lookup, used as its first lookup


def _local_date(ts, tzname):
    try:
        tz = ZoneInfo(tzname) if tzname else HOME_TZ
    except Exception:
        tz = HOME_TZ
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def api_lookup(q):
    ident = _x({"ident": q.get("ident")})["ident"]
    if not IDENT_RE.match(ident or ""):
        return {"error": "Enter a flight number like UA1234 or UAL1234"}
    now = time.time()
    date = (q.get("date") or "").strip() or datetime.now(HOME_TZ).strftime("%Y-%m-%d")
    try:
        d0 = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=HOME_TZ).timestamp()
    except ValueError:
        return {"error": "Date must be YYYY-MM-DD"}
    if not aero_on():
        return {"mode": "manual", "date": date, "reason": "Enter the airports (looking up a flight number needs an AeroAPI key)."}
    if d0 > now + 2 * 86400 - 3600 or d0 + 86400 < now - 9 * 86400:
        return {"mode": "manual", "date": date, "reason": "AeroAPI covers the past 9 days to 2 days ahead: enter the airports."}
    key = (ident, date)
    hit = LOOKUPS.get(key)
    if not hit or now - hit["at"] > 600:
        cost = float(CFG.get("aeroapi_cost_per_query") or 0.005)
        _, spent = aero_month(now)
        if spent + cost > float(CFG.get("aeroapi_monthly_budget") or 0) + 1e-9:
            return {"mode": "manual", "date": date, "reason": "This month's AeroAPI budget is used up: enter the airports."}
        rec = {"at": now, "leg": ident, "leg_id": None, "reason": "flight lookup", "billed": True, "pages": 1}
        try:
            flights, pages = aeroapi.query(CFG["aeroapi_key"].strip(), ident, d0 - 6 * HOUR, d0 + 30 * HOUR)
            rec.update(pages=pages or 1, billed=bool(pages), ok=True, found=bool(flights))
        except Exception as e:
            msg = str(e).splitlines()[0][:200]
            rec.update(ok=False, error=msg, billed="rejected" not in msg and "rate limit" not in msg)
            with LOCK:
                STATE["aero_calls"] = [c for c in STATE["aero_calls"] if c["at"] > now - 40 * 86400] + [rec]
            return {"mode": "manual", "date": date, "reason": f"AeroAPI: {msg}. Enter the airports instead."}
        with LOCK:
            STATE["aero_calls"] = [c for c in STATE["aero_calls"] if c["at"] > now - 40 * 86400] + [rec]
        hit = LOOKUPS[key] = {"at": now, "flights": flights}
        for k in [k for k, v in LOOKUPS.items() if now - v["at"] > 3600]:
            del LOOKUPS[k]
    out = []
    for f in hit["flights"]:
        t = f.get("times") or {}
        so = (t.get("out") or {}).get("scheduled") or (t.get("off") or {}).get("scheduled")
        o, d = f.get("origin") or {}, f.get("destination") or {}
        if not so or _local_date(so, o.get("tz")) != date:
            continue
        oa, da = airports.get(o.get("iata") or o.get("icao")), airports.get(d.get("iata") or d.get("icao"))
        out.append({"fa_id": f.get("fa_id"), "ident": f.get("ident"), "display": f.get("display_ident") or ident,
                    "dep": o.get("iata") or o.get("icao"), "arr": d.get("iata") or d.get("icao"),
                    "dep_city": oa.get("city") or o.get("city") or "", "arr_city": da.get("city") or d.get("city") or "",
                    "dep_tz": o.get("tz") or oa.get("tz"), "arr_tz": d.get("tz") or da.get("tz"),
                    "out": t.get("out") or {}, "in": t.get("in") or {}, "status": f.get("status_text") or "",
                    "cancelled": f.get("cancelled"), "aircraft_type": f.get("aircraft_type"), "tail": f.get("tail")})
    out.sort(key=lambda x: x["out"].get("scheduled") or 0)
    return {"mode": "list", "date": date, "ident": ident, "flights": out}


def add_picked(body):
    """Add the flight picked from a lookup; its AeroAPI answer becomes the first lookup."""
    fid, date = body.get("fa_id"), (body.get("date") or "").strip()
    ident = _x({"ident": body.get("ident")})["ident"]
    hit = LOOKUPS.get((ident, date)) or {}
    f = next((x for x in hit.get("flights") or [] if x.get("fa_id") == fid), None)
    if not f:
        raise ValueError("That lookup expired; search again")
    o, d = f.get("origin") or {}, f.get("destination") or {}
    lid = add_manual(ident, o.get("iata") or o.get("icao"), d.get("iata") or d.get("icao"), date, body.get("kind") or "manual")
    PRESEED[lid] = json.loads(json.dumps(f))
    # show what the lookup already knows right away (times, gates, airplane), instead of a placeholder
    # until the tracker's first full check gets to it; that check runs next, ahead of the others
    with LOCK:
        L = STATE["legs"].get(lid)
        if L is not None and not L.get("flight"):
            t = f.get("times") or {}
            L["flight"], L["source"] = json.loads(json.dumps(f)), "AeroAPI"
            so, si = (t.get("out") or {}).get("scheduled"), (t.get("in") or {}).get("scheduled")
            if so:
                L["sched_out"] = so
            if si:
                L["sched_in"] = si
            L["next_check"] = 0
    REFRESH_Q.put(lid)
    WAKE.set()
    bump()
    return lid


def set_kind(lid, kind):
    with LOCK:
        for x in STATE["manual"]:
            if extra_id(**_x(x)) == lid:
                x["kind"] = _kind({"kind": kind})
                if lid in STATE["legs"]:
                    STATE["legs"][lid]["kind"] = x["kind"]
                return True
    return False


def auto_recovery(now):
    """After a diversion lands, follow the same flight number on from the diversion airport to the
    original destination (UA2608 IAH→EWR diverted to ABE: follow UA2608 ABE→EWR). It's found through
    FlightAware like any added flight. If your work calendar later lists that leg, the calendar's
    copy wins and the automatic one is dropped."""
    adds = []
    with LOCK:
        legs = list(STATE["legs"].values())
        for L in legs:
            f = L.get("flight") or {}
            t = f.get("times") or {}
            to = f.get("diverted_to") or (((f.get("destination") or {}).get("iata") or "").upper() if f.get("diverted") else "")
            planned = planned_arr(L)
            landed = (t.get("on") or {}).get("actual") or (t.get("in") or {}).get("actual")
            if not to or not planned or to == planned or not landed or L.get("recovery_added"):
                continue
            m = IDENT_RE.match((L.get("display") or "").replace(" ", "")) or IDENT_RE.match(L.get("ident") or "")
            if not m:
                continue
            num = m.group(2)
            if any(o is not L and (o.get("flight_no") or "") == num and o.get("dep") == to and planned_arr(o) == planned
                   for o in legs):
                L["recovery_added"] = "existing"  # already on your calendar or added
                continue
            try:
                tz = ZoneInfo(airports.get(to).get("tz") or TZNAME)
            except Exception:
                tz = HOME_TZ
            date = datetime.fromtimestamp(landed, tz).strftime("%Y-%m-%d")
            kind = "friend" if rules_mod.kind_of(L) == "friends" else "manual"
            adds.append((L, f"{m.group(1)}{num}", to, planned, date, kind))
        # the calendar now lists an automatic recovery leg: keep the calendar's
        cal = {(o.get("flight_no"), o.get("dep"), o.get("arr")) for o in legs if not o.get("extra")}
        drop = [x for x in STATE["manual"] if x.get("auto") and
                ((IDENT_RE.match(x["ident"]) or [None, None, None])[2], x["dep"], x["arr"]) in cal]
    for x in drop:
        remove_manual(extra_id(**_x(x)))
        log.info("Recovery leg %s %s→%s is on the calendar now; dropped the automatic copy", x["ident"], x["dep"], x["arr"])
    for L, ident, dep, arr, date, kind in adds:
        try:
            lid = add_manual(ident, dep, arr, date, kind)
        except ValueError as e:
            log.info("Recovery leg for %s: %s", L.get("display"), e)
            continue
        with LOCK:
            for x in STATE["manual"]:
                if extra_id(**_x(x)) == lid:
                    x["auto"] = L["id"]
            cur = STATE["legs"].get(L["id"])
            if cur is not None:
                cur["recovery_added"] = lid
        REFRESH_Q.put(lid)
        WAKE.set()
        log.info("%s diverted to %s: now following %s %s→%s", L.get("display"), dep, ident, dep, arr)
        push_event("recovery", f"{L.get('display') or ident} continues {dep}→{arr}",
                   f"Now following {L.get('display') or ident} {dep}→{arr}, the flight on to {arr} after the diversion.",
                   leg=L)
    if adds or drop:
        save_state()
        bump()


def remove_manual(lid):
    with LOCK:
        STATE["manual"] = [x for x in STATE["manual"] if extra_id(**_x(x)) != lid]
        STATE["legs"].pop(lid, None)


SHOT_KEEP_HOURS = 48


def clean_screenshots(now):
    """Delete screenshots of flights that are gone or finished more than two days ago."""
    with LOCK:
        keep = {lid for lid, L in STATE["legs"].items()
                if not finished(L, now) or now < (L.get("updated") or L["sched_out"]) + SHOT_KEEP_HOURS * HOUR}
    removed = 0
    try:
        for name in os.listdir(SHOT_DIR):
            if not name.endswith(".png"):
                continue
            base = name[:-4]
            lid = re.sub(r"-(inbound|chain\d+)$", "", base)
            path = os.path.join(SHOT_DIR, name)
            if lid in keep:
                continue
            if now - os.path.getmtime(path) > SHOT_KEEP_HOURS * HOUR or lid not in STATE["legs"]:
                os.remove(path)
                removed += 1
    except FileNotFoundError:
        return
    except Exception as e:
        log.warning("Screenshot cleanup: %s", e)
    if removed:
        log.info("Deleted %d old screenshots", removed)


def shot_usage():
    try:
        files = [os.path.join(SHOT_DIR, n) for n in os.listdir(SHOT_DIR) if n.endswith(".png")]
        return len(files), sum(os.path.getsize(p) for p in files)
    except Exception:
        return 0, 0


def expire_manual(now):
    """Drop app-added flights a day and a half after they took off or were due."""
    with LOCK:
        keep = []
        for x in STATE["manual"]:
            L = STATE["legs"].get(extra_id(**_x(x)))
            f = (L or {}).get("flight")
            off = f and (f["times"]["off"]["actual"] or f["times"]["out"]["actual"])
            due = L and L.get("flight") and L["sched_in"]
            gone = (off and now > off + 36 * HOUR) or (due and finished(L, now) and now > due + 36 * HOUR)
            if not gone:
                keep.append(x)
        STATE["manual"] = keep


# --------------------------------------------------------------------------
# tracking
# --------------------------------------------------------------------------

def finished(L, now):
    """Tracking stops at the arrival gate (or once long cancelled / long past)."""
    f = L.get("flight")
    ph = fa.phase(f, now)
    due_in = max(L["sched_in"], (fa.best_time(f["times"], "in") if f else 0) or 0)
    if ph == "arrived":
        return True
    if ph == "cancelled":
        return now > L["sched_out"] + 2 * HOUR
    if ph == "diverted":
        return bool(f["times"]["in"]["actual"]) or now > due_in + 4 * HOUR
    if ph in ("airborne", "landed"):
        return now > due_in + 6 * HOUR  # lost track of it; don't check forever
    if L.get("extra"):
        return False
    return now > max(L["sched_in"], (fa.best_time(f["times"], "in") if f else 0) or 0) + 2 * HOUR


def watched(L, now):
    """Inside the page-check window. Nothing at all runs before watch_hours out."""
    if finished(L, now):
        return False
    if L.get("extra") and not L.get("flight"):
        if not aero_on():
            return True  # no AeroAPI: the page check is the only way to find it
        if not (L.get("aero") or {}).get("at"):
            return False  # AeroAPI looks first
        # AeroAPI didn't have it: FlightAware's own page often does (a diversion's recovery leg,
        # ABE→EWR, shows there long before AeroAPI lists it), and looking there is free
        start, end = aero_window(L, now)
        return start <= now <= end
    return now >= L["sched_out"] - float(CFG["watch_hours"]) * HOUR


def poll_seconds(L, now):
    f = L.get("flight")
    ph = fa.phase(f, now)
    # The earlier of scheduled and estimated departure: a delay never slows the checks down.
    sched = ((f or {}).get("times", {}).get("out", {}).get("scheduled") if f else None) or L["sched_out"]
    est = fa.best_time(f["times"], "out") if f else None
    out_t = min(t for t in (sched, est) if t)
    on_t = fa.best_time(f["times"], "on") if f else None
    if ph in ("airborne", "landed", "diverted"):
        near = ph == "landed" or (on_t and on_t - now <= 20 * 60)
        return max(60, float(CFG["poll_fast_minutes"] if near else CFG.get("poll_air_minutes", 10)) * 60)
    if ph == "taxi_out":
        m = CFG["poll_fast_minutes"]
    elif out_t - now <= 45 * 60:
        m = CFG["poll_fast_minutes"]
    elif out_t - now <= 3 * HOUR:
        m = CFG["poll_near_minutes"]
    else:
        m = CFG["poll_far_minutes"]
    if L.get("extra") and not L.get("flight"):
        m = max(m, CFG["poll_near_minutes"])
    return max(60, float(m) * 60)


def load(browser, url, shot, L, what):
    """browser.load with a record in the debug log."""
    t0 = time.time()
    rec = {"at": t0, "leg": L.get("display") or L.get("ident"), "leg_id": L["id"], "what": what, "url": url,
           "shot": shot}
    try:
        boot, path = browser.load(url, shot)
        cur, act = fa.flights_from_bootstrap(boot)
        rec.update(page_data=boot is not None, flights=(1 if cur else 0) + len(act),
                   has_shot=bool(path), ms=int((time.time() - t0) * 1000),
                   found=(f"{cur['origin'].get('iata')}→{cur['destination'].get('iata')} "
                          f"{fa.phase(cur, time.time())}") if cur else None)
        return boot, path
    except Exception as e:
        rec.update(error=str(e).splitlines()[0][:300], ms=int((time.time() - t0) * 1000))
        raise
    finally:
        with LOCK:
            STATE["debug"] = (STATE["debug"] + [rec])[-60:]
            streak = None
            if "page_data" in rec or "error" in rec:
                STATE["blocked_streak"] = 0 if rec.get("page_data") else STATE.get("blocked_streak", 0) + 1
                streak = STATE["blocked_streak"]
        need = STATE["rules"]["sys_blocked"]["params"].get("count") or 3
        if streak == need:
            push_event("sys_blocked", "FlightAware checks failing",
                       f"The last {need} page checks found no flight data. Open the browser viewer on port 6081 "
                       "to clear a CAPTCHA or sign in.", "warn")


def fetch_flight(browser, L):
    """Load the leg's FlightAware page(s). Returns (flight, source, shot_path, note)."""
    ident, lid = L.get("ident"), L["id"]
    if not ident:
        return None, None, None, "No flight number found in the calendar title"
    shot = None
    if L.get("fa_id"):
        boot, shot = load(browser, fa.id_url(L["fa_id"]), lid, L, "flight (pinned)")
        cur, act = fa.flights_from_bootstrap(boot)
        note_same_number(L, act)
        if cur:
            return cur, "FlightAware page data", shot, None
        if boot is not None:
            L.pop("fa_id", None)  # stale instance; look it up again
    if not L.get("fa_id"):
        boot, shot = load(browser, fa.ident_url(ident), lid, L, "flight lookup")
        cur, activity = fa.flights_from_bootstrap(boot)
        note_same_number(L, activity)
        if L.get("extra") and boot is not None:
            inst = fa.pick_followed(cur, activity, L.get("dep"), L.get("arr"), L.get("date"), time.time())
            if inst is None and L.get("dep") and L.get("arr"):
                # Not on the flight number's main page (a diversion's recovery leg often isn't):
                # FlightAware's history list for the number links every leg by route and date.
                hist = history_lookup(browser, L, ident, lid)
                if hist:
                    return hist, "FlightAware page data", None, None
            if inst is None:
                route = f" {L['dep']}→{L['arr']}" if L.get("dep") else ""
                when = f" on {L['date']}" if L.get("date") else ""
                return None, None, shot, (f"FlightAware doesn't list {ident}{route}{when} yet" if when
                                          else f"No upcoming {ident}{route} on FlightAware yet")
            L["fa_id"] = inst["fa_id"]
            if cur and inst["fa_id"] == cur["fa_id"]:
                return cur, "FlightAware page data", shot, None
            boot2, shot = load(browser, fa.id_url(inst["fa_id"]), lid, L, "flight (dated)")
            c2, _ = fa.flights_from_bootstrap(boot2)
            return (c2 or inst), "FlightAware page data", shot, None
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
            boot2, shot = load(browser, fa.id_url(inst["fa_id"]), lid, L, "flight (matched day)")
            c2, _ = fa.flights_from_bootstrap(boot2)
            return (c2 or inst), "FlightAware page data", shot, None
    # No page data: read the screenshot.
    if browser.client:
        when = L.get("date") or datetime.fromtimestamp(L["sched_out"], alerting.tz_for(L.get("dep"))).strftime("%a %d %b %H:%M")
        with LOCK:
            STATE["gemini_calls"] = [t for t in STATE["gemini_calls"] if t > time.time() - 40 * 86400] + [time.time()]
        f = browser.read_screenshot(shot, ident, L.get("dep"), when)
        with LOCK:
            STATE["debug"] = (STATE["debug"] + [{"at": time.time(), "leg": L.get("display"), "leg_id": lid,
                                                  "what": "Gemini read screenshot", "url": "",
                                                  "shot": lid, "has_shot": True,
                                                  "found": (f or {}).get("status_text") or "nothing usable"}])[-60:]
        if f:
            return f, "screenshot (Gemini)", shot, None
        return None, None, shot, "The page screenshot shows no matching flight"
    return None, None, shot, "FlightAware page data unavailable and no Gemini key set for the screenshot fallback"


def inbound_needs_check(L, f):
    """Re-check the inbound until it's at the gate. After that, only if the aircraft
    changes: a different tail on our flight, or FlightAware links a different inbound."""
    inb = L.get("inbound") or {}
    if not inb or not fa.same_flight(inb.get("fa_id"), f.get("inbound_fa_id")):
        return True
    if f.get("tail") and inb.get("checked_for_tail") and f["tail"] != inb["checked_for_tail"]:
        return True
    return fa.phase(inb, time.time()) not in ("arrived", "cancelled", "diverted")


CHAIN_HOPS = 3
AERO_RETRY_HOURS = (6, 3, 1.5)


def _month(ts):
    return datetime.fromtimestamp(ts, HOME_TZ).strftime("%Y-%m")


def aero_month(now):
    """(queries billed this month, dollars)."""
    with LOCK:
        pages = sum(c.get("pages", 1) for c in STATE["aero_calls"] if c.get("billed") and _month(c["at"]) == _month(now))
    return pages, pages * float(CFG.get("aeroapi_cost_per_query") or 0.005)


def aero_on():
    return bool((CFG.get("aeroapi_key") or "").strip())


def aero_window(L, now):
    """Query window (start, end) for this flight."""
    if not L.get("extra"):
        return L["sched_out"] - 6 * HOUR, L["sched_out"] + 6 * HOUR
    if L.get("date"):
        try:
            tz = ZoneInfo(airports.get(L.get("dep")).get("tz") or TZNAME)
            d0 = datetime.strptime(L["date"], "%Y-%m-%d").replace(tzinfo=tz).timestamp()
            return d0 - 6 * HOUR, d0 + 30 * HOUR
        except Exception:
            pass
    return now - 3 * HOUR, now + 47 * HOUR


def aero_call(L, now, reason, retry_h=None, observed_inbound=None, observed_tail=None):
    """One AeroAPI query for this flight, within budget. Returns the matched flight or None."""
    a = L.setdefault("aero", {})
    if a.get("calls", 0) >= int(CFG.get("aeroapi_max_per_leg") or 6):
        return None
    cost = float(CFG.get("aeroapi_cost_per_query") or 0.005)
    _, spent = aero_month(now)
    if spent + cost > float(CFG.get("aeroapi_monthly_budget") or 0) + 1e-9:
        if a.get("blocked") != _month(now):
            a["blocked"] = _month(now)
            if STATE["flags"].get("budget100") != _month(now):
                STATE["flags"]["budget100"] = _month(now)
                push_event("sys_budget", "AeroAPI budget used up",
                           f"${spent:.2f} spent this month; AeroAPI is paused until next month.", "warn")
            log.warning("AeroAPI monthly budget reached ($%.2f); skipping (%s)", spent, reason)
        return None
    rec = {"at": now, "leg": L.get("display"), "leg_id": L["id"], "reason": reason, "billed": True, "pages": 1}
    flight = None
    try:
        start, end = aero_window(L, now)
        flights, pages = aeroapi.query(CFG["aeroapi_key"].strip(), L["ident"], start, end)
        if L.get("extra"):
            flight = fa.pick_followed(None, flights, L.get("dep"), L.get("arr"), L.get("date"), now)
            # Not found: say which routes this flight number does fly, so a wrong airport is obvious.
            routes = []
            for f in flights or []:
                r = f"{(f.get('origin') or {}).get('iata') or '?'}→{(f.get('destination') or {}).get('iata') or '?'}"
                if r not in routes:
                    routes.append(r)
            a["other_routes"] = [] if flight else routes[:4]
        else:
            flight = fa.pick_instance(None, flights, L.get("dep"), L["sched_out"], window=4 * HOUR)
        rec.update(pages=pages or 1, billed=bool(pages), ok=True, found=bool(flight),
                   tail=(flight or {}).get("tail"), inbound=(flight or {}).get("inbound_fa_id"))
        a.update(at=now, calls=a.get("calls", 0) + 1, reason=reason, found=bool(flight), error=None)
        if flight:
            a.update(tail=flight.get("tail") or a.get("tail"), inbound_id=flight.get("inbound_fa_id"),
                     fa_id=flight.get("fa_id"), aircraft_type=flight.get("aircraft_type"))
        log.info("AeroAPI %s (%s): %s", L.get("display"), reason,
                 f"tail {flight.get('tail') or 'not assigned'}, inbound {flight.get('inbound_fa_id') or 'none'}"
                 if flight else "flight not found")
    except Exception as e:
        msg = str(e).splitlines()[0][:200]
        rec.update(ok=False, error=msg, billed="rejected" not in msg and "rate limit" not in msg)
        a.update(at=a.get("at") or now, error=msg)
        log.warning("AeroAPI %s: %s", L.get("display"), msg)
    if retry_h:
        a["retries"] = (a.get("retries") or []) + [retry_h]
    if observed_inbound:
        a["rechecked_for"] = observed_inbound
    if observed_tail:
        a["rechecked_tail"] = observed_tail
    with LOCK:
        STATE["aero_calls"] = [c for c in STATE["aero_calls"] if c["at"] > now - 40 * 86400] + [rec]
    _, spent = aero_month(now)
    budget = float(CFG.get("aeroapi_monthly_budget") or 0)
    if budget and spent >= 0.8 * budget and STATE["flags"].get("budget80") != _month(now):
        STATE["flags"]["budget80"] = _month(now)
        push_event("sys_budget", "AeroAPI budget 80% used", f"${spent:.2f} of ${budget:.2f} this month.")
    return flight


def not_found_note(L):
    routes = (L.get("aero") or {}).get("other_routes") or []
    want = f"{L.get('dep') or '?'}→{L.get('arr') or '?'}"
    if routes:
        return (f"Not found yet: AeroAPI has {L.get('display') or L.get('ident')} as {', '.join(routes)}, not {want}. "
                "Checking FlightAware's page meanwhile (a diversion's recovery flight often shows there first); "
                "if it never turns up, the airports may be wrong.")
    return f"Not found yet: AeroAPI has no {L.get('display') or L.get('ident')} {want}. Checking FlightAware's page meanwhile."


def first_lookup_due(L, now):
    """The first look at a flight is AeroAPI, from watch_hours before departure."""
    a = L.get("aero") or {}
    if not aero_on() or a.get("found") or not L.get("ident") or L.get("flight"):
        return False
    if a.get("at") and now - a["at"] < 6 * HOUR:
        return False  # looked and didn't find it; try again in a few hours
    if not L.get("extra"):
        return now >= L["sched_out"] - float(CFG["watch_hours"]) * HOUR and now < L["sched_in"] + 2 * HOUR
    if L.get("date"):
        start, _ = aero_window(L, now)
        return now >= start + 6 * HOUR - float(CFG["watch_hours"]) * HOUR
    return True  # undated: one query to learn when its next departure is


def aero_reason(L, f, now):
    """Why this flight needs another AeroAPI query, or (None, None)."""
    a = L.get("aero") or {}
    if not a.get("at"):
        return None, None
    scraped = f.get("inbound_fa_id")
    page_tail = (f.get("tail") or "").upper() if not f.get("tail_source") else ""
    same_plane = bool(page_tail) and page_tail == (a.get("tail") or "").upper()
    if scraped and a.get("inbound_id") and not same_plane and not fa.same_flight(scraped, a["inbound_id"]) \
            and not fa.same_flight(scraped, a.get("rechecked_for")):
        short = lambda x: "-".join(str(x).split("-")[:1]) if x else "?"
        return f"FlightAware page shows a different inbound ({short(scraped)} vs AeroAPI {short(a['inbound_id'])})", None
    if not a.get("tail"):
        out_t = fa.best_time(f["times"], "out") or L["sched_out"]
        for h in AERO_RETRY_HOURS:
            if out_t - now <= h * HOUR and h not in (a.get("retries") or []):
                return f"no tail assigned yet ({h:g} h before departure)", h
    return None, None


def aero_check(L, f, now):
    """Re-ask AeroAPI when the free checks suggest a different airplane, within budget."""
    if not aero_on() or not L.get("ident") or fa.phase(f, now) != "scheduled":
        return
    reason, retry_h = aero_reason(L, f, now)
    if reason:
        aero_call(L, now, reason, retry_h,
                  observed_inbound=f.get("inbound_fa_id") if reason.startswith("FlightAware page") else None)


ROTATION_MAX = 6
ROTATION_REFRESH = 10 * 60
DONE = ("arrived", "cancelled", "diverted")


def _fill_airports(fl):
    for side in ("origin", "destination"):
        ap = fl.get(side) or {}
        ref = airports.get(ap.get("iata") or ap.get("icao"))
        for k in ("lat", "lon", "tz", "name", "city"):
            if ap.get(k) is None and ref.get(k) is not None:
                ap[k] = ref[k]
        fl[side] = ap
    return fl


def final(h, now):
    """A leg is frozen only once we've fetched it after it finished, so its track is complete."""
    ph = fa.phase(h, now)
    if h.get("track_v") != 2:
        return False  # saved before tracks were thinned evenly: fetch once more
    if ph in ("cancelled", "diverted"):
        return True
    if ph != "arrived":
        return False
    t = h.get("times") or {}
    done_at = (t.get("in") or {}).get("actual") or (t.get("on") or {}).get("actual")
    return bool(done_at) and (h.get("fetched") or 0) >= done_at


def update_rotation(browser, L, now):
    """The airplane's day before your flight: your inbound, its inbound, ... back to the
    first leg that has already arrived (nothing earlier matters). Finished legs are kept
    as they are; legs still to come are refreshed every ROTATION_REFRESH."""
    inb = L.get("inbound")
    if not inb or not inb.get("fa_id"):
        L["rotation"] = [inb] if inb else []
        return
    old = {h.get("fa_id"): h for h in (L.get("rotation") or []) if h and h.get("fa_id")}
    rot = [inb]
    cur = inb
    for hop in range(1, ROTATION_MAX):
        if fa.phase(cur, now) in DONE + ("taxi_out", "airborne", "landed") or not cur.get("inbound_fa_id"):
            break  # this leg has arrived, or the airplane is on it now: nothing earlier matters
        prev_id = cur["inbound_fa_id"]
        cached = old.get(prev_id)
        if cached and (final(cached, now) or now - (cached.get("fetched") or 0) < ROTATION_REFRESH):
            nxt = cached
        else:
            try:
                boot, _ = load(browser, fa.id_url(prev_id), f"{L['id']}-chain{hop}", L, f"aircraft's earlier leg #{hop}")
                nxt, _ = fa.flights_from_bootstrap(boot)
            except Exception as e:
                log.info("%s rotation hop %d: %s", L.get("display"), hop, e)
                nxt = cached
            if nxt is None:
                break
            if nxt is not cached:
                _fill_airports(nxt)
                nxt["fetched"] = now
                nxt["track_v"] = 2
                nxt["track"] = fa.thin(nxt.get("track") or [], 300)
        rot.append(nxt)
        cur = nxt
    L["rotation"] = rot


def live_src(live):
    """Where a live position came from, for the lookup log: the FAA radar that saw it, else adsb.lol."""
    return (live or {}).get("src") or "adsb.lol"


def live_all(callsign, quick=False):
    """Airplanes transmitting this callsign now: FAA radar first (approach and surface radar carry
    the transponder address, so the N-number), adsb.lol when the FAA has nothing fresh with an
    address (en route, outside the US, or before STDDS is set up)."""
    s = swim.live_all(callsign) if swim.STATE.get("status") != "off" else []
    if s and all(a.get("tail") for a in s):
        return s
    try:
        a = sources.fetch_live_all(callsign, quick=True) if quick else sources.fetch_live_all(callsign)
    except Exception:
        if s:
            return s
        raise
    return a or s


def pick_live(callsign, fl, accept=None, quick=False):
    """The airplane broadcasting this callsign that is *this* flight. Two can be up at once with the
    same number (yesterday's late one still flying, an earlier leg holding): prefer the one with the
    tail we know, then the one nearest where this flight should be (its last tracked position, or
    its route between the two airports)."""
    return choose_live([a for a in (live_all(callsign, quick=True) if quick else live_all(callsign)) if accept is None or accept(a)], fl)


def choose_live(cands, fl):
    if len(cands) <= 1:
        return cands[0] if cands else None
    fl = fl or {}
    tail = (fl.get("tail") or "").upper()
    same = [a for a in cands if tail and (a.get("tail") or "").upper() == tail]
    if len(same) == 1:
        return same[0]
    pts = []
    tr = [p for p in (fl.get("track") or []) if p and p[0] is not None]
    if tr:
        pts = [(tr[-1][0], tr[-1][1])]
    else:
        o, d = fl.get("origin") or {}, fl.get("destination") or {}
        oa, da = airports.get(o.get("iata") or o.get("icao")), airports.get(d.get("iata") or d.get("icao"))
        if oa.get("lat") is not None and da.get("lat") is not None:
            pts = [(oa["lat"] + (da["lat"] - oa["lat"]) * i / 20, oa["lon"] + (da["lon"] - oa["lon"]) * i / 20) for i in range(21)]
    if not pts:
        return cands[0]
    return min(cands, key=lambda a: min(sources.nm_between(p[0], p[1], a["lat"], a["lon"]) for p in pts))


LIVE_EVERY = 5          # s between position refreshes for flights moving now
ADSB_EVERY = 60        # s between adsb.lol asks, only when the FAA radar has nothing in the last 90 s


def live_tick(now):
    """Keep moving airplanes moving between flight checks: your flights (taxiing, in the air,
    landing) and inbounds in the air get their position from FAA radar every few seconds (no
    network: it's already here), else from adsb.lol every 30 s. The page polls /api/live."""
    with LOCK:
        todo = []
        for L in STATE["legs"].values():
            f = L.get("flight") or {}
            if not L.get("ident") or not f or not watched(L, now):
                continue
            ph = fa.phase(f, now)
            if ph in ("taxi_out", "airborne", "landed"):
                todo.append(("own", L["id"], L["ident"], ph, dict(f), L.get("dep"),
                             (f.get("destination") or {}).get("iata") or L.get("arr"), L.get("adsb_poll") or 0))
            inb = L.get("inbound") or {}
            if inb.get("ident") and ph == "scheduled" and fa.phase(inb, now) in ("taxi_out", "airborne"):
                todo.append(("inb", L["id"], inb["ident"], fa.phase(inb, now), dict(inb), None, None,
                             L.get("adsb_poll_inb") or 0))
    for who, lid, ident, ph, fl, dep, arr, polled in todo:
        if who == "own":
            def mine(ac, ph=ph, dep=dep, arr=arr):
                if ph == "airborne":
                    return not ac.get("ground")
                return _near(dep if ph == "taxi_out" else arr, ac, 12) is not False
        else:
            def mine(ac, ph=ph):
                return not ac.get("ground") or ph == "taxi_out"
        try:
            cands = [a for a in (swim.live_all(ident) if swim.STATE.get("status") != "off" else []) if mine(a)]
        except Exception as e:
            log.debug("live position %s: %s", ident, e)
            continue
        radar = cands and all(a.get("tail") for a in cands)   # approach / surface radar, every few seconds
        if radar:
            _set_live(lid, who, cands[0] if len(cands) == 1 else choose_live(cands, fl))
            continue
        if cands:
            _set_live(lid, who, choose_live(cands, fl))   # center radar meanwhile
        fresh = any(now - (a.get("at") or 0) < 90 for a in cands)
        if not fresh and now - polled >= ADSB_EVERY and (lid, who) not in ADSB_ASKING:
            # the FAA has nothing recent (outside the US, or a gap): ask adsb.lol, on its own thread
            # so a slow answer never holds up the radar positions of other flights. Between the
            # FAA's once-a-minute center radar fixes the page carries the airplane forward itself.
            with LOCK:
                L = STATE["legs"].get(lid)
                if L is not None:
                    L["adsb_poll" if who == "own" else "adsb_poll_inb"] = now
            ADSB_ASKING.add((lid, who))
            threading.Thread(target=_ask_adsb, args=(lid, who, ident, fl, mine), daemon=True).start()


ADSB_ASKING = set()


def _ask_adsb(lid, who, ident, fl, mine):
    try:
        _set_live(lid, who, pick_live(ident, fl, mine, quick=True))
    except Exception as e:
        log.debug("live position %s: %s", ident, e)
    finally:
        ADSB_ASKING.discard((lid, who))


def _set_live(lid, who, live):
    if not live:
        return
    with LOCK:
        L = STATE["legs"].get(lid)
        if L is None:
            return
        key = "live" if who == "own" else "inbound_live"
        if (L.get(key) or {}).get("at", 0) >= live.get("at", 0):
            return
        L[key] = live
        add_fix(L.get("flight") if who == "own" else L.get("inbound"), live)
        if who == "inb" and live.get("tail") and live.get("src"):
            # approach radar sees the inbound's transponder (so its tail) while it climbs out of its
            # origin: keep it, it's the airplane coming to fly your flight
            f, inb = L.get("flight") or {}, L.get("inbound") or {}
            tail = live["tail"].upper()
            prev = (L.get("adsb_inbound") or {}).get("tail")
            route = f"{(inb.get('origin') or {}).get('iata') or '?'}→{(inb.get('destination') or {}).get('iata') or '?'}"
            L["adsb_inbound"] = {"tail": tail, "at": live.get("at") or time.time(), "root": f.get("inbound_fa_id"),
                                 "source": f"inbound {inb.get('display_ident') or inb.get('ident')} {route} on {live['src']}"}
            if tail != (prev or "").upper() and tail != (f.get("tail") or "").upper():
                REFRESH_Q.put(lid)
                WAKE.set()


def live_loop():
    while True:
        try:
            live_tick(time.time())
        except Exception:
            log.exception("live positions")
        time.sleep(LIVE_EVERY)


def api_live(lid, since=0):
    """Position and the newest track points for the flight page between full refreshes."""
    with LOCK:
        L = STATE["legs"].get(lid)
        if not L:
            return None
        f, inb = L.get("flight") or {}, L.get("inbound") or {}
        tail = [p for p in (f.get("track") or [])[-60:] if p and len(p) > 4 and (p[4] or 0) > since]
        itail = [p for p in (inb.get("track") or [])[-60:] if p and len(p) > 4 and (p[4] or 0) > since]
        return {"live": L.get("live"), "inbound_live": L.get("inbound_live"), "track": tail, "inbound_track": itail,
                "phase": fa.phase(f, time.time()) if f else None}


def add_fix(fl, live):
    """Extend a flight's drawn track with a live ADS-B position, so the airplane sits on its line
    between FlightAware's (less frequent) track updates."""
    if not fl or not live or live.get("lat") is None:
        return
    tr = fl.get("track")
    if not isinstance(tr, list):
        return
    at = int(live.get("at") or time.time())
    # near the ground the airplane turns a lot (departures, approaches, taxiing) and approach and
    # surface radar report every few seconds: keep a point every 5 s there, every 15 s at cruise
    low = (live.get("alt_ft") or 0) < 18000 or (live.get("gs_kt") or 0) < 250
    if tr and tr[-1] and len(tr[-1]) > 4 and tr[-1][4] and at <= tr[-1][4] + (5 if low else 15):
        return
    tr.append([round(live["lat"], 5), round(live["lon"], 5), live.get("alt_ft"), live.get("gs_kt"), at])


def walk_chain(browser, L, now):
    """Read the registration from ADS-B on the first leg of the airplane's day that's transmitting."""
    update_rotation(browser, L, now)
    L["_rot_done"] = True
    hops, found = [], None
    for i, cur in enumerate(L.get("rotation") or []):
        if not cur:
            break
        ph = fa.phase(cur, now)
        route = f"{(cur.get('origin') or {}).get('iata') or '?'}→{(cur.get('destination') or {}).get('iata') or '?'}"
        name = cur.get("display_ident") or cur.get("ident") or "?"
        hops.append({"ident": name, "route": route, "phase": ph,
                     "out": fa.best_time(cur["times"], "out"), "in": fa.best_time(cur["times"], "in")})
        if found is None and ph in ("taxi_out", "airborne", "landed") and cur.get("ident"):
            try:
                live = pick_live(cur["ident"], cur)
            except Exception as e:
                log.info("%s chain ADS-B: %s", L.get("display"), e)
                live = None
            saw(L, f"{live_src(live) if live else 'Transponder'} · {'inbound ' if i == 0 else ''}{name} {route}", (live or {}).get("tail"),
                None if (live or {}).get("tail") else "not seen by FAA radar or ADS-B")
            if live and live.get("tail"):
                found = {"tail": live["tail"].upper(), "kind": "chain", "at": now,
                         "root": (L.get("flight") or {}).get("inbound_fa_id"),
                         "source": f"{'inbound ' if i == 0 else ''}{name} {route} in flight"}
    L["chain"] = hops
    return found


def inbound_pdc(L, f):
    """The airplane named in your inbound's datalink clearance at its departure airport."""
    inb = L.get("inbound") or {}
    if not inb.get("ident"):
        return None
    org = _iata((inb.get("origin") or {}).get("iata") or (inb.get("origin") or {}).get("icao"))
    so = fa.best_time(inb.get("times") or {}, "out") or 0
    best = None
    for ev in swim.events(inb["ident"]):
        pd = ev.get("pdc") or {}
        tail = (pd.get("tail") or "").upper().strip()
        if not tail or _iata(ev.get("dep")) != org:
            continue
        issued = _ts(pd.get("at")) or ev.get("got") or 0
        if so and so - 4 * HOUR <= issued <= so + 2 * HOUR and (not best or issued >= best["at"]):
            best = {"tail": ("N" + tail) if tail[0].isdigit() else tail, "at": issued, "root": f.get("inbound_fa_id"),
                    "source": f"inbound {inb.get('display_ident') or inb['ident']} datalink clearance"}
    return best


def pdc_tail(L, f):
    """The airplane named in your flight's datalink clearance, if one came for this departure."""
    pd = (L.get("swim") or {}).get("pdc") or {}
    tail = (pd.get("tail") or "").upper().strip()
    if not tail:
        return None
    if tail[0].isdigit():
        tail = "N" + tail
    so = fa.best_time(f.get("times") or {}, "out") or L.get("sched_out") or 0
    issued = _ts(pd.get("at")) or pd.get("got") or 0
    # a clearance from a different day's flight with the same number doesn't count
    if not so or not (so - 4 * HOUR <= issued <= so + 2 * HOUR):
        return None
    return {"tail": tail, "kind": "pdc", "at": issued, "source": "FAA datalink clearance"}


def own_inbound(L, f):
    """If the flight bringing this airplane is one of your own tracked flights (a turn, e.g.
    EWR-MIA-EWR), that leg. Its tail is the best answer for this one too."""
    ib = f.get("inbound_fa_id") or (L.get("aero") or {}).get("inbound_id")
    if not ib:
        return None
    with LOCK:
        for other in STATE["legs"].values():
            if other is L or other.get("id") == L.get("id"):
                continue
            ofa = other.get("fa_id") or (other.get("flight") or {}).get("fa_id")
            if ofa and fa.same_flight(ofa, ib):
                return other
    return None


def note_same_number(L, activity):
    """From the flight-number history FlightAware already sends with your flight's page (no extra
    lookup): does this flight number fly an earlier leg into your departure airport today?"""
    if not activity or L.get("extra"):
        return
    dep, so = (L.get("dep") or "").upper(), L.get("sched_out") or 0
    # only trust "no earlier leg" when the history actually covers today (your own departure is in it)
    if not any(abs((fa.best_time(a["times"], "out") or 0) - so) < 3 * 3600 for a in activity):
        return
    hit = None
    for a in activity:
        d = a.get("destination") or {}
        code = (d.get("iata") or (d.get("icao") or "")[-3:] or "").upper()
        t_in = fa.best_time(a["times"], "in") or fa.best_time(a["times"], "on")
        if code == dep and t_in and so - 12 * 3600 <= t_in <= so:
            o = a.get("origin") or {}
            hit = {"from": o.get("iata") or (o.get("icao") or "")[-3:], "in": t_in}
    L["same_number_earlier"] = {"checked": time.time(), "leg": hit}


def _near(code, ac, nm):
    ap = airports.get(code)
    if ap.get("lat") is None or ac.get("lat") is None:
        return None
    return sources.nm_between(ap["lat"], ap["lon"], ac["lat"], ac["lon"]) <= nm


def adsb_route(L, near=None):
    """adsb.lol's route for this flight number (['DFW', 'EWR', 'MCO']), cached for 6 h."""
    r = L.get("adsb_route") or {}
    if time.time() - (r.get("at") or 0) < 6 * 3600:
        return r.get("codes")
    try:
        codes = sources.fetch_route(L["ident"], *(near or (0, 0)))
        if not codes and near:
            codes = sources.fetch_route(L["ident"], 0, 0)
    except Exception as e:
        log.info("%s adsb.lol route: %s", L.get("display"), e)
        codes = r.get("codes")
    L["adsb_route"] = {"at": time.time(), "codes": codes}
    return codes


def inbound_tail(L, f):
    """The tail the airplane flying in is broadcasting, if we've seen it: (tail, from the inbound's
    own transponder rather than an earlier leg of its day)."""
    ai = L.get("adsb_inbound") or {}
    if ai.get("tail") and fa.same_flight(ai.get("root"), f.get("inbound_fa_id")):
        return ai["tail"].upper(), True
    ti = L.get("tail_info") or {}
    return (ti["tail"].upper(), False) if ti.get("kind") == "chain" and ti.get("tail") else (None, False)


def own_live(L, f, now):
    """Your flight's own transponder, only when it's really your departure. The same flight
    number can fly an earlier leg into your airport (UA1268 DFW→EWR, then yours EWR→MCO), and that
    airplane keeps broadcasting the number after it lands. So before pushback a match must be on the
    ground at your departure airport and, when adsb.lol says the number has an earlier leg into it,
    be the airplane your inbound is (by its own transponder). Without a known earlier leg, an airplane
    that isn't your inbound (by the inbound's own transponder) isn't taken either: it goes to AeroAPI
    as a possible swap instead. -> (aircraft or None, why not, tail worth checking)"""
    ph = fa.phase(f, now)
    pushed = bool(((f.get("times") or {}).get("out") or {}).get("actual")) or ph != "scheduled"
    cands = live_all(L["ident"])
    if not cands:
        return None, "not transmitting yet", None
    dep = L.get("dep")
    route = adsb_route(L, (cands[0]["lat"], cands[0]["lon"]))
    earlier = None
    # adsb.lol's routes are crowd-sourced and can be stale (UAL2328 listed as RSW-EWR-SFO when it
    # isn't): only believe one that contains this exact leg, dep→arr
    arr = (f.get("destination") or {}).get("iata") or L.get("arr")
    if route and dep in route and 0 < route.index(dep) < len(route) - 1 and route[route.index(dep) + 1] == arr:
        earlier = f"{route[route.index(dep) - 1]}→{dep}"
    # FlightAware's flight-number history (already loaded with your flight) settles it when it has
    # today's legs: an earlier leg it doesn't show is a stale route; one it shows counts even
    # without a route
    sn = L.get("same_number_earlier") or {}
    if sn.get("checked"):
        earlier = f"{sn['leg']['from']}→{dep}" if sn.get("leg") else None
    inb, strong = inbound_tail(L, f)
    why = odd = None
    for ac in sorted(cands, key=lambda a: -(a.get("gs_kt") or 0)):
        at_dep = _near(dep, ac, 6 if not pushed else 12)
        if at_dep is False or (not pushed and not ac.get("ground") and (ac.get("alt_ft") or 0) > 1500):
            why = f"{ac.get('tail') or 'an airplane'} with this number isn't at {dep}" + (f" (the earlier {earlier} leg)" if earlier else "")
            continue
        # Before pushback the airplane at the gate with your number must be the one your inbound's own
        # transponder shows. A different one is an earlier leg with the same number (or a swap that
        # FlightAware/AeroAPI will report on their own); your transponder takes over once you push.
        mismatch = inb and (ac.get("tail") or "").upper() != inb
        if not pushed and earlier and (mismatch or not inb):
            why = (f"{ac.get('tail')} is the earlier {earlier} leg, not your inbound {inb}" if inb else
                   f"{ac.get('tail')} at {dep} may be the earlier {earlier} leg; waiting for pushback or the inbound's transponder")
            continue
        if not pushed and mismatch and strong:
            why = f"{ac.get('tail')} has your number but your inbound is {inb}: an earlier leg with the same number, or a swap (asking AeroAPI)"
            odd = (ac.get("tail") or "").upper() or None
            continue
        return ac, None, None
    return None, why, odd


def resolve_tail(browser, L, f, now):
    """Which airplane is flying this leg. Trust order: your own flight's transponder; the airplane
    seen on ADS-B flying in (your inbound or an earlier leg of its day); then the newest of
    your own earlier leg on a turn and AeroAPI; FlightAware last."""
    info = dict(L.get("tail_info") or {})
    page_tail = f.get("tail") if not f.get("tail_source") else None
    if info.get("kind") == "chain" and not fa.same_flight(info.get("root"), f.get("inbound_fa_id")):
        info = {}  # a different inbound is a different airplane
    if info.get("kind") in ("page", "aeroapi", "turn", "chain", "pdc"):
        info = {}  # re-derived every time
    ph = fa.phase(f, now)
    odd_tail = None
    out_t = fa.best_time(f["times"], "out") or L["sched_out"]
    if L.get("ident") and (ph == "taxi_out" or (ph == "scheduled" and out_t - now < 90 * 60)):
        try:
            live, why, odd_tail = own_live(L, f, now)
            saw(L, f"{live_src(live) if live else 'Transponder'} · your flight {L.get('display') or L['ident']}", (live or {}).get("tail"),
                None if (live or {}).get("tail") else why or "not transmitting yet")
            if live and live.get("tail"):
                info = {"tail": live["tail"].upper(), "kind": "own", "at": now,
                        "source": "your flight's transponder"}
        except Exception as e:
            log.info("%s own ADS-B: %s", L.get("display"), e)
            saw(L, f"Transponder · your flight {L.get('display') or L['ident']}", None, sources.adsb_error_text(e))
    # The FAA datalink clearance (PDC) for your flight at your departure airport names the airplane
    # (/AN N12345), usually 30-60 min before departure: next best after your own transponder.
    # Not every airport sends PDCs; without one this simply doesn't apply.
    if info.get("kind") != "own":
        pt = pdc_tail(L, f)
        if pt:
            info = pt
            if L.get("pdc_logged") != pt["tail"]:   # once per airplane, not every check
                L["pdc_logged"] = pt["tail"]
                saw(L, "FAA datalink clearance (PDC)", pt["tail"], None)
    # Free signals first: the airplane flying in on ADS-B
    # and your own earlier leg on a turn. AeroAPI only to confirm what those can't explain.
    # Once the inbound has left its gate, the airplane can't change without a new inbound number
    # (FlightAware sees that): its own transponder is the answer, and the airline pages can rest.
    inb = L.get("inbound") or {}
    inb_gone = bool(inb.get("times")) and fa.phase(inb, now) in ("taxi_out", "airborne", "landed", "arrived")
    ai = L.get("adsb_inbound") or {}
    if not (ai.get("tail") and fa.same_flight(ai.get("root"), f.get("inbound_fa_id"))):
        ai = inbound_pdc(L, f) or {}
    # The inbound's own transponder (or its clearance): fresh, or seen since it left its gate. Once
    # it has left, its airplane can't change, so a tail seen then stays the answer even when nothing
    # can see the transponder later (en route the FAA's center radar carries no tail).
    inb_out = ((inb.get("times") or {}).get("out") or {}).get("actual") or 0
    fresh_ai = bool(ai.get("tail")) and fa.same_flight(ai.get("root"), f.get("inbound_fa_id")) and (
        now - (ai.get("at") or 0) < 20 * 60 or (inb_gone and (ai.get("at") or 0) >= inb_out - 10 * 60))
    chain = walk_chain(browser, L, now) if ph == "scheduled" and info.get("kind") not in ("own", "pdc") and not (inb_gone and fresh_ai) else None
    if not chain and fresh_ai:
        chain = {"tail": ai["tail"], "kind": "chain", "at": ai["at"], "root": ai["root"], "source": ai["source"]}
    mine = own_inbound(L, f) if info.get("kind") != "own" else None
    mi = (mine or {}).get("tail_info") or {}
    a = L.get("aero") or {}
    aero_tail = a.get("tail")
    known = {x.upper() for x in ((chain or {}).get("tail"), mi.get("tail")) if x}
    observed = info.get("tail") if info.get("kind") in ("own", "pdc") else (odd_tail or page_tail)
    if observed and observed.upper() in known:
        observed = None  # already explained for free; no AeroAPI call needed
    if aero_tail and observed and observed.upper() != aero_tail.upper() and aero_on() \
            and a.get("rechecked_tail") != observed and ph == "scheduled":
        aero_call(L, now, f"{observed} seen, AeroAPI had {aero_tail}", observed_tail=observed)
        a = L.get("aero") or {}
        aero_tail = a.get("tail")
    if info.get("kind") not in ("own", "pdc"):
        # The airplane actually flying your inbound (ADS-B) is the strongest evidence short of your
        # own transponder: that's how a swap without a new inbound number shows up. Otherwise the
        # newest of your earlier leg on a turn and AeroAPI.
        if chain:
            info = chain
        else:
            cands = []
            if mine and mi.get("tail") and mi.get("kind") in ("own", "aeroapi", "turn", "chain"):
                cands.append((mi.get("at") or 0, {"tail": mi["tail"].upper(), "kind": "turn", "at": mi.get("at") or now,
                                                  "source": f"your {mine.get('display') or 'earlier flight'} ({mi.get('source')})"}))
            if aero_tail:
                cands.append((a.get("at") or 0, {"tail": aero_tail.upper(), "kind": "aeroapi", "at": a.get("at") or now,
                                                 "source": "AeroAPI (airline assignment)"}))
            if cands:
                info = max(cands, key=lambda c: c[0])[1]  # the newest word on it wins
    if not info.get("tail") and f.get("tail"):
        info = {"tail": f["tail"], "kind": "page", "at": now, "source": "FlightAware"}
    if info.get("tail"):
        f["tail"] = info["tail"]
        f["tail_source"] = info["source"]
    L["tail_info"] = info


def snapshot(L):
    """The facts worth logging when they change, as display strings."""
    f = L.get("flight") or {}
    t = f.get("times") or {}
    o, d = f.get("origin") or {}, f.get("destination") or {}
    tz_a = alerting.tz_for(L.get("dep"), o)
    tz_b = alerting.tz_for(L.get("arr"), d)

    def tm(key, tz, kind):
        v = (t.get(key) or {}).get(kind)
        return alerting.hhmm(v, tz) if v else None
    inb = L.get("inbound") or {}
    def gate(ap):
        g, term = ap.get("gate"), ap.get("terminal")
        if not g:
            return None
        if not term or str(g).upper().startswith(str(term).upper()):
            return g
        return f"T{term} {g}"
    snap = {
        "Status": alerting.status_label(L, CFG) if f else None,
        "Gate": gate(o),
        "Scheduled out": tm("out", tz_a, "scheduled"),
        "Estimated out": tm("out", tz_a, "estimated"),
        "Actual out": tm("out", tz_a, "actual"),
        "Takeoff estimate": tm("off", tz_a, "estimated") if not (t.get("off") or {}).get("actual") else None,
        "Takeoff": tm("off", tz_a, "actual"),
        "Landing estimate": tm("on", tz_b, "estimated") if not (t.get("on") or {}).get("actual") else None,
        "Landed": tm("on", tz_b, "actual"),
        "Estimated in": tm("in", tz_b, "estimated"),
        "Arrival gate": gate(d),
        "Bags": d.get("baggage"),
        "Aircraft": f.get("aircraft_type"),
        "Tail": f.get("tail"),
        "Inbound": " ".join(x for x in (inb.get("display_ident"),
                                        (inb.get("origin") or {}).get("iata") and f"from {inb['origin']['iata']}") if x) or None,
        "Inbound due": alerting.hhmm(fa.best_time(inb["times"], "in"), tz_a) if inb.get("times") and fa.best_time(inb["times"], "in") else None,
    }
    return snap


def saw(L, src, tail, note=None):
    """Record what a tail source said on this check, for the lookup log (even when nothing changed)."""
    L.setdefault("_seen", []).append({"src": src, "tail": (tail or "").upper() or None, "note": note})


def _seen_key(seen):
    return [(x.get("src"), x.get("tail"), x.get("note")) for x in seen or []]


def log_lookup(L, prev, now, sources, seen=None):
    """Append what this check changed (or count it as a quiet check)."""
    seen = seen if seen is not None else L.pop("_seen", None) or []
    # A transponder / clearance line only when it says something new for that airplane: a tail
    # (or a reason it has none) different from the last one logged, whichever source saw it.
    last = L.setdefault("seen_last", {})
    keep = []
    for x in seen:
        if x["src"] == "FAA SWIM":
            # what the FAA, towers and the airline said: always logged. (It used to go through the
            # transponder dedupe below, which turns digits into '#', so "landing est. 19:48" then
            # "19:49" read as the same line and every update after the first was dropped.)
            keep.append(x)
            continue
        who = x["src"].split(" · ", 1)[-1]
        if x.get("tail"):
            # a tail: shown when it differs from the last tail seen for this airplane
            if last.get(who) != x["tail"]:
                last[who] = x["tail"]
                keep.append(x)
        else:
            # no tail (not transmitting, adsb.lol not answering): shown once per reason, and it
            # doesn't make the known tail show again on the next sighting
            note = re.sub(r"\d+", "#", x.get("note") or "")
            if last.get(who + " ·none") != note:
                last[who + " ·none"] = note
                keep.append(x)
    seen = keep
    cur = snapshot(L)
    changes = [{"field": k, "from": prev.get(k), "to": v} for k, v in cur.items() if v != prev.get(k) and v is not None]
    ts = (L.get("flight") or {}).get("time_src") or {}
    via = {"Actual out": ("out", "actual"), "Estimated out": ("out", "estimated"), "Takeoff": ("off", "actual"),
           "Takeoff estimate": ("off", "estimated"), "Landing estimate": ("on", "estimated"), "Landed": ("on", "actual"),
           "Estimated in": ("in", "estimated"), "Actual in": ("in", "actual")}
    for c in changes:
        if c["field"] in via:
            k, kind = via[c["field"]]
            c["via"] = (ts.get(k) or {}).get(kind)
        elif c["field"] == "Status":
            t = (L.get("flight") or {}).get("times") or {}
            for k in ("in", "on", "off", "out"):
                if (t.get(k) or {}).get("actual"):
                    c["via"] = f"{(ts.get(k) or {}).get('actual') or 'FlightAware'} ({ {'in': 'at gate', 'on': 'landing', 'off': 'takeoff', 'out': 'push'}[k]} time)"
                    break
    if L.get("tail_source") is None and (L.get("flight") or {}).get("tail_source"):
        for c in changes:
            if c["field"] == "Tail":
                c["via"] = L["flight"]["tail_source"]
    hist = L.setdefault("history", [])
    if not changes and hist and not hist[-1]["changes"] and hist[-1]["sources"] == sources \
            and (not seen or _seen_key(hist[-1].get("seen")) == _seen_key(seen)):
        hist[-1]["quiet"] = hist[-1].get("quiet", 1) + 1
        hist[-1]["last"] = now
    else:
        hist.append({"at": now, "sources": sources, "changes": changes, "error": L.get("error"),
                     "note": L.get("note"), "seen": seen})
    L["history"] = hist[-150:]


class _OutsideWindow(Exception):
    pass


def source_is_aero(L):
    return L.get("source") == "AeroAPI"


def update_leg(browser, lid):
    with LOCK:
        L = STATE["legs"].get(lid)
        if not L:
            return
        L = json.loads(json.dumps(L))  # work on a copy outside the lock
    now = time.time()
    prev_snap = snapshot(L)
    prev_aero_at = (L.get("aero") or {}).get("at")
    prev_tail_at = (L.get("tail_info") or {}).get("at")
    L["_seen"] = []
    used = []
    first = None
    fast = lid in SWIM_FAST
    SWIM_FAST.discard(lid)
    try:
        pre = PRESEED.pop(lid, None)
        if pre:
            # picked from a lookup: that AeroAPI answer is the first lookup (already paid for)
            first = pre
            a = L.setdefault("aero", {})
            a.update(at=now, calls=a.get("calls", 0) + 1, reason="flight lookup", found=True, error=None,
                     tail=first.get("tail") or a.get("tail"), inbound_id=first.get("inbound_fa_id"),
                     fa_id=first.get("fa_id"), aircraft_type=first.get("aircraft_type"))
            L["fa_id"] = first.get("fa_id") or L.get("fa_id")
        elif first_lookup_due(L, now):
            first = aero_call(L, now, "first lookup")
            if first:
                L["fa_id"] = first.get("fa_id") or L.get("fa_id")
        if first:
            used.append("AeroAPI")
            flight, source, shot, note = first, "AeroAPI", None, None
            if first.get("tail"):
                first["tail_source"] = "AeroAPI (airline assignment)"
                L["tail_info"] = {"tail": first["tail"].upper(), "kind": "aeroapi", "at": now,
                                  "source": "AeroAPI (airline assignment)"}
        elif not watched(L, now):
            flight, source, shot, note = None, None, None, (
                not_found_note(L) if (L.get("aero") or {}).get("at") else None)
        elif fast and L.get("flight"):
            # SWIM just reported takeoff/landing/diversion: apply it now (a second), not after a
            # FlightAware page load; the page comes on the regular check right after.
            flight, source, shot, note = None, L.get("source"), None, L.get("note")
        else:
            flight, source, shot, note = fetch_flight(browser, L)
            used.append(source or "FlightAware page (no flight data)")
        if flight:
            # Fill in airport details FlightAware left out.
            for side, code in (("origin", L.get("dep")), ("destination", L.get("arr"))):
                ap = flight.get(side) or {}
                ref = airports.get(ap.get("iata") or ap.get("icao") or code)
                for k in ("iata", "icao", "name", "city", "lat", "lon", "tz"):
                    if not ap.get(k) and ref.get(k) is not None:
                        ap[k] = ref[k]
                flight[side] = ap
            planned = planned_arr(L)
            sw = swim_check(L, flight, planned)
            if sw.get("new"):
                used.append("FAA SWIM")
            swim_overlay(L, flight, now)
            rotation_swim(L, now)
            fa.fix_diversion(flight, planned, sw.get("diverted_to") or route_end(flight.get("route")))
            if L.get("extra"):
                L["dep"] = flight["origin"].get("iata") or L["dep"]
                # a diversion keeps the airport you planned as the leg's arrival
                L["arr"] = planned or flight["destination"].get("iata") or L["arr"]
                L["sched_out"] = flight["times"]["out"]["scheduled"] or flight["times"]["off"]["scheduled"] or now
                L["sched_in"] = flight["times"]["in"]["scheduled"] or flight["times"]["on"]["scheduled"] or now
            # Keep the flown track across refreshes when a page only shows the recent part.
            old_track = (L.get("flight") or {}).get("track") or []
            if old_track and flight.get("track") and flight["track"][0][4] and old_track[0][4] \
                    and old_track[0][4] < flight["track"][0][4] and L.get("fa_id") == flight.get("fa_id"):
                flight["track"] = [p for p in old_track if p[4] and p[4] < flight["track"][0][4]] + flight["track"]
                flight["track"] = fa.thin(flight["track"], 1500)
            # A diversion (or any new flight record mid-trip) can come without the registration: the
            # airplane hasn't changed in the air, so keep the tail we already had.
            ti = L.get("tail_info") or {}
            if not flight.get("tail") and ti.get("tail") and fa.phase(flight, now) != "scheduled":
                flight["tail"], flight["tail_source"] = ti["tail"], ti.get("source")
            L["flight"], L["source"] = flight, source
        elif L.get("flight") and watched(L, now):
            # no fresh page this time: the FAA's messages still move the saved flight along
            if swim_check(L, L["flight"], planned_arr(L)).get("new"):
                used.append("FAA SWIM")
            swim_overlay(L, L["flight"], now)
            rotation_swim(L, now)
        if L.get("extra") and not L.get("flight") and (L.get("aero") or {}).get("at"):
            note = not_found_note(L)  # page check came up empty too: keep the clear "not found" story
        L["note"] = note
        if (shot or not first) and not fast:
            L["shot"] = bool(shot)
        ph = fa.phase(L.get("flight"), now)
        if first or not watched(L, now):
            # The AeroAPI lookup is this check; page loads (inbound, comparison) start on the next one,
            # and not at all while the flight is more than watch_hours out.
            L["updated"], L["error"] = now, None
            raise _OutsideWindow()

        # Inbound aircraft
        f = L.get("flight") or {}
        if f:
            aero_check(L, f, now)
            ai = (L.get("aero") or {}).get("inbound_id")
            if ai and not f.get("inbound_fa_id"):
                f["inbound_fa_id"] = ai  # the page didn't link one; AeroAPI did
        ti = L.get("tail_info") or {}
        if f and not f.get("tail") and ti.get("tail") and ti.get("kind") in ("own", "chain") \
                and (ti.get("kind") == "own" or fa.same_flight(ti.get("root"), f.get("inbound_fa_id"))):
            f["tail"], f["tail_source"] = ti["tail"], ti["source"]
        if f.get("inbound_fa_id") and ph == "scheduled" and inbound_needs_check(L, f):
            try:
                boot, _ = load(browser, fa.id_url(f["inbound_fa_id"]), lid + "-inbound", L, "inbound aircraft")
                inb, _ = fa.flights_from_bootstrap(boot)
                if inb:
                    for side in ("origin", "destination"):
                        ap = inb.get(side) or {}
                        ref = airports.get(ap.get("iata") or ap.get("icao"))
                        for k in ("lat", "lon", "tz", "name", "city"):
                            if ap.get(k) is None and ref.get(k) is not None:
                                ap[k] = ref[k]
                    inb["checked_for_tail"] = f.get("tail")
                    L["inbound"] = inb
            except Exception as e:
                log.warning("%s inbound: %s", L.get("display"), e)
        elif ph != "scheduled":
            pass  # keep the last inbound snapshot for the history
        elif f.get("inbound_ident") and not f.get("inbound_fa_id"):
            L["inbound"] = L.get("inbound") or {"display_ident": f["inbound_ident"], "times": {
                k: {"scheduled": None, "estimated": None, "actual": None} for k in fa.TIME_KEYS},
                "origin": {}, "destination": {}}

        # Which airplane, and the airplane's day before this flight
        if f and L.get("flight") and ph in ("scheduled", "taxi_out"):
            resolve_tail(browser, L, f, now)
        if ph == "scheduled" and L.get("inbound") and not L.get("_rot_done"):
            update_rotation(browser, L, now)
        L.pop("_rot_done", None)
        rotation_swim(L, now)   # freshly loaded inbound legs get the FAA's times straight away

        # Live positions
        if CFG.get("live_positions", True):
            if ph in ("taxi_out", "airborne", "landed") and L.get("ident"):
                def mine(ac, ph=ph):
                    if ph == "airborne":
                        return not ac.get("ground")
                    return _near(L.get("dep") if ph == "taxi_out" else (f.get("destination") or {}).get("iata") or L.get("arr"), ac, 12) is not False
                try:
                    L["live"] = pick_live(L["ident"], L.get("flight"), mine)
                    add_fix(L.get("flight"), L["live"])
                except Exception as e:
                    log.info("%s live position: %s", L.get("display"), e)
            else:
                L["live"] = None
            inb = L.get("inbound")
            if inb and ph == "scheduled" and fa.phase(inb, now) in ("taxi_out", "airborne") and inb.get("ident"):
                try:
                    L["inbound_live"] = pick_live(inb["ident"], inb, lambda ac: not ac.get("ground") or fa.phase(inb, now) == "taxi_out")
                    add_fix(inb, L["inbound_live"])
                except Exception as e:
                    log.info("inbound live position: %s", e)
            else:
                L["inbound_live"] = None

        L["updated"] = now
        L["error"] = None
    except _OutsideWindow:
        pass
    except Exception as e:
        log.error("%s: %s", L.get("display") or lid, e)
        L["error"] = str(e).splitlines()[0][:300]
    L["next_check"] = now + (90 if source_is_aero(L) and watched(L, now) and not L.get("compared")
                             else 20 if fast else poll_seconds(L, now))
    if L.get("source") and L["source"] != "AeroAPI":
        L["compared"] = True

    with LOCK:
        nas = list(STATE["nas"])
        new_alerts = alerting.evaluate(L, CFG, now, nas, rules=STATE["rules"], wx=STATE.get("wx")) \
            if not L.get("extra") or L.get("flight") else []
        live = STATE["legs"].get(lid)
        if live is None:
            return
        # Keep schedule fields that may have changed while we were fetching.
        for k in ("sched_out", "sched_in", "summary", "dh") if not L.get("extra") else ():
            L[k] = live.get(k, L.get(k))
        a_at = (L.get("aero") or {}).get("at")
        if a_at and a_at != prev_aero_at and "AeroAPI" not in used:
            used.append(f"AeroAPI ({(L.get('aero') or {}).get('reason')})")
        ti = L.get("tail_info") or {}
        for x in L.get("_seen") or []:
            if "adsb.lol" not in used and x["src"].startswith("adsb.lol"):
                used.append("adsb.lol")
            elif "FAA radar" not in used and " radar · " in x["src"]:
                used.append("FAA radar")
        if L.get("inbound") and any(d.get("leg_id") == lid and d.get("what") == "inbound aircraft"
                                    and d["at"] >= now for d in STATE["debug"][-6:]):
            used.append("FlightAware page (inbound)")
        log_lookup(L, prev_snap, time.time(), used or ["no lookup"])  # stamped when the check finished, after the page load
        L["alerts"] = (L.get("alerts") or []) + new_alerts
        L["alerts"] = L["alerts"][-60:]
        STATE["legs"][lid] = L
        for a in new_alerts:
            STATE["alerts"].append({**a, "leg": lid, "display": L.get("display"), "route": f"{L['dep']}→{L['arr']}"})
        STATE["alerts"] = STATE["alerts"][-ALERT_KEEP:]
    bump()
    for a in new_alerts:
        notify({**a, "leg": lid})
    log.info("%s %s→%s: %s%s", L.get("display"), L.get("dep"), L.get("arr"),
             alerting.status_label(L, CFG), f" ({L['note']})" if L.get("note") else "")
    # AeroAPI answers in a second; go straight on to the FlightAware pages (comparison and the
    # inbound aircraft) instead of waiting for the next cycle, so the inbound shows up right away.
    if first and watched(L, time.time()) and not finished(L, time.time()):
        update_leg(browser, lid)
    elif first and L.get("fa_id") and rules_mod.kind_of(L) == "manual" and finished(L, time.time()):
        past_fill(browser, lid)


def past_fill(browser, lid):
    """A past flight you added: AeroAPI found it; one FlightAware page load adds the flown track,
    gates and anything else the page has. It then goes to History like any finished flight."""
    with LOCK:
        L = STATE["legs"].get(lid)
        fid = L and L.get("fa_id")
    if not fid:
        return
    try:
        boot, _ = load(browser, fa.id_url(fid), lid, L, "past flight")
        cur, _ = fa.flights_from_bootstrap(boot)
    except Exception as e:
        log.info("%s past flight page: %s", L.get("display"), e)
        return
    if not cur:
        return
    _fill_airports(cur)
    with LOCK:
        live = STATE["legs"].get(lid)
        if live is None:
            return
        old = live.get("flight") or {}
        for k in ("tail", "aircraft_type", "aircraft_name"):
            if not cur.get(k) and old.get(k):
                cur[k] = old[k]
        for k in ("out", "off", "on", "in"):  # keep AeroAPI's times where the page has none
            for x in ("scheduled", "actual"):
                if not cur["times"][k].get(x) and ((old.get("times") or {}).get(k) or {}).get(x):
                    cur["times"][k][x] = old["times"][k][x]
        live["flight"], live["source"], live["updated"] = cur, "FlightAware page data", time.time()
    bump()


def inbound_adsb(now):
    """Between your flight's own checks: while your inbound is taxiing or flying, read its
    transponder every adsb_inbound_minutes. A different tail than the one we have (a swap that
    kept the same flight number) triggers a full check of your flight right away. Every read
    goes in the flight's lookup log, with the tail it saw."""
    every = float(CFG.get("adsb_inbound_minutes") or 0)
    if not every:
        return
    with LOCK:
        legs = [L for L in STATE["legs"].values() if L.get("inbound") and rules_mod.kind_of(L) != "friends"]
    for L in legs:
        f = L.get("flight") or {}
        inb = L["inbound"]
        iph = fa.phase(inb, now)
        t = inb.get("times") or {}
        # FlightAware's snapshot of the inbound can be a few minutes old: also count it as moving
        # from its expected pushback until its expected arrival
        out_e, in_e = (fa.best_time(t, "out") if t else None), (fa.best_time(t, "in") if t else None)
        moving = iph in ("taxi_out", "airborne", "landed") or (
            iph == "scheduled" and out_e and in_e and out_e - 5 * 60 <= now <= in_e + 20 * 60)
        if fa.phase(f, now) != "scheduled" or not moving or not inb.get("ident") \
                or now - (L.get("adsb_at") or 0) < every * 60 or not watched(L, now):
            continue
        try:
            # the inbound flight number can also fly an earlier leg: take the one moving toward you
            live = pick_live(inb["ident"], inb, lambda ac: not ac.get("ground") or iph in ("taxi_out", "landed")
                                      or _near((inb.get("origin") or {}).get("iata"), ac, 12) is not False)
            err = None
        except Exception as e:
            log.info("%s inbound ADS-B: %s", L.get("display"), e)
            live, err = None, sources.adsb_error_text(e)
        route = f"{(inb.get('origin') or {}).get('iata') or '?'}→{(inb.get('destination') or {}).get('iata') or '?'}"
        with LOCK:
            cur = STATE["legs"].get(L["id"])
            if cur is None:
                continue
            cur["adsb_at"] = now
            if live:
                cur["inbound_live"] = live
                add_fix(cur.get("inbound"), live)
                if live.get("tail"):
                    cur["adsb_inbound"] = {"tail": live["tail"].upper(), "at": now, "root": f.get("inbound_fa_id"),
                                           "source": f"inbound {inb.get('display_ident') or inb['ident']} {route} in flight"}
            seen = (live or {}).get("tail")
            have = ((cur.get("flight") or {}).get("tail") or "").upper()
            who = live_src(live) if live else "adsb.lol"
            log_lookup(cur, snapshot(cur), now, ["FAA radar" if live and live.get("src") else "adsb.lol"],
                       seen=[{"src": f"{who if live else 'Transponder'} · inbound {inb.get('display_ident') or inb['ident']} {route}",
                              "tail": (seen or "").upper() or None,
                              "note": None if seen else (err or "not seen by FAA radar or ADS-B")}])
            if seen and have and seen.upper() != have:
                log.info("%s: inbound %s is %s on ADS-B, we had %s; checking now", L.get("display"), inb["ident"], seen, have)
                REFRESH_Q.put(L["id"])
                WAKE.set()
        bump()


BACKUP = {"dirty": True, "tried": 0, "running": False, "changed": 0, "failed": False}
BACKUP_WAKE = threading.Event()


def backup_extra():
    with LOCK:
        return {"rules": STATE.get("rules"), "app_settings": STATE.get("app_settings"), "manual": STATE.get("manual")}


def backup_files():
    stamp = datetime.now(HOME_TZ).strftime("%Y-%m-%d")
    blob = history.make_backup(backup_extra())
    return [("Flight Tracker backup.zip", "application/zip", blob),
            ("Flight history.csv", "text/csv", history.export_csv())], stamp


def run_backup(force=False):
    """Upload the backup to Drive. Changes go up ~15 s after things settle (backup_loop); once a day regardless."""
    if BACKUP["running"] or not gdrive.state().get("refresh_token"):
        return False
    now = time.time()
    g = gdrive.state()
    if not force and not (BACKUP["dirty"] or now - (g.get("last_backup") or 0) > 86400):
        return False
    BACKUP.update(running=True, tried=now, dirty=False)
    bump()
    try:
        n = len(history.flights())
        before = g.get("last_count")
        if before and n < before - 5 and n < before * 0.9:
            # a big delete: keep the old backup next to the new one, in case it wasn't meant
            name = f"Flight Tracker backup before delete {datetime.now(HOME_TZ):%Y-%m-%d %H%M} ({before} flights).zip"
            try:
                if gdrive.copy("Flight Tracker backup.zip", name):
                    log.info("Drive: %d -> %d flights, kept the old backup as %s", before, n, name)
            except Exception as e:
                log.warning("Drive: couldn't keep the old backup: %s", e)
        files, _ = backup_files()
        ok = gdrive.upload(files)
        if ok:
            gdrive.note(last_count=n)
            log.info("Drive backup done (%d flights)", n)
        else:
            BACKUP["dirty"] = True
        return ok
    except Exception as e:
        BACKUP["dirty"] = True
        log.warning("Drive backup: %s", e)
        return False
    finally:
        BACKUP["running"] = False
        bump()


def backup_loop():
    """Uploads after any change once things have been quiet for 15 s (a burst of edits = one upload);
    retries a failed upload after 2 min; and the daily backup."""
    while True:
        BACKUP_WAKE.wait(60)
        BACKUP_WAKE.clear()
        try:
            if not gdrive.state().get("refresh_token"):
                continue
            while time.time() - BACKUP["changed"] < 15:
                time.sleep(3)
            now = time.time()
            if BACKUP["dirty"] and now - BACKUP["tried"] < 120 and BACKUP["failed"]:
                continue
            ran = run_backup()
            BACKUP["failed"] = bool(ran is False and BACKUP["dirty"])
        except Exception:
            log.exception("Backup loop")


def mark_dirty():
    BACKUP.update(dirty=True, changed=time.time(), failed=False)
    BACKUP_WAKE.set()
    bump()


def archive_finished(now):
    """Calendar and Manual flights go into History once they're done (friends' flights don't).
    Re-archived whenever a later check fills in more (e.g. the gate-arrival time)."""
    changed = False
    with LOCK:
        legs = list(STATE["legs"].values())
    for L in legs:
        kind = rules_mod.kind_of(L)
        f = L.get("flight")
        if kind == "friends" or not f:
            continue
        ph = fa.phase(f, now)
        if ph not in ("arrived", "cancelled", "diverted") and not finished(L, now):
            continue
        if (L.get("archived_at") or 0) >= (L.get("updated") or 0):
            # flights saved before the Lookup Log was kept with them: add it while the leg's still here
            if L.get("history_id") and L.get("history") and not history.has_log(L["history_id"]):
                try:
                    history.save_log(L["history_id"], list(L["history"]))
                    changed = True   # so the Drive backup picks it up
                except Exception as e:
                    log.info("History: couldn't save the lookup log of %s: %s", L.get("display"), e)
            continue
        try:
            rec = history.from_leg(L, kind)
            if not rec["date"]:
                continue
            history.upsert(rec, pts=f.get("track"))
            if L.get("history"):
                history.save_log(rec["id"], list(L["history"]))
            with LOCK:
                live = STATE["legs"].get(L["id"])
                if live is not None:
                    live["archived_at"], live["history_id"] = now, rec["id"]
            changed = True
        except Exception as e:
            log.warning("History: couldn't save %s: %s", L.get("display"), e)
    if changed:
        history.save()
        mark_dirty()
        bump()


def refresh_nas(now):
    try:
        items = sources.fetch_nas()
        with LOCK:
            STATE["nas_page"], STATE["nas_at"], STATE["nas_error"] = items, now, None
        rebuild_nas(now)
    except Exception as e:
        with LOCK:
            STATE["nas_error"] = str(e)[:200]
            STATE["nas_at"] = now
        log.warning("NAS status: %s", e)


NAS_IDS = {}   # (airport, kind) -> the key a running program got when first seen, from either source


def merge_nas(page, swim_items, cancelled, page_at, now):
    """The NAS status page and the FAA's own flow messages (SWIM), woven together: ground stops and
    ground delay programs come from SWIM the moment they're issued (with delays and scope), the
    page fills in what SWIM lacks (advisory link, flights included) and supplies everything SWIM
    doesn't carry (delay trends, closures, deicing). A program SWIM cancelled after the page was
    last read stays gone. Each program keeps one key whichever source reports it, so it alerts once."""
    out, seen = [], set()
    page_prog = {(n["airport"], n["kind"]): n for n in page if n["kind"] in ("gs", "gdp")}
    for k in list(dict.fromkeys(list(swim_items) + list(page_prog))):
        sw, pg = swim_items.get(k), page_prog.get(k)
        if sw and sw.get("end") and sw["end"] < now - 60:
            sw = None   # program over
        if not sw and pg and cancelled.get(k):
            # SWIM cancelled it. The NAS page often keeps showing a program for several minutes
            # after it ends, so the page doesn't bring it back: only a program that started
            # after the cancel (a new one) counts, and only once 45 minutes have passed do we
            # trust the page again without question.
            c = cancelled[k]
            if now - c < 45 * 60 and not ((pg.get("start") or 0) > c + 60):
                continue
        if not sw and not pg:
            continue
        m = dict(pg or {})
        if sw:
            m.update({x: v for x, v in sw.items() if v not in (None, "", [])})
            m["source"] = "FAA SWIM" + (" + NAS status" if pg else "")
        else:
            m["source"] = "NAS status"
        m["key"] = NAS_IDS.get(k) or (pg or sw)["key"]
        NAS_IDS[k] = m["key"]
        seen.add(k)
        out.append(m)
    for k in [k for k in NAS_IDS if k not in seen]:
        del NAS_IDS[k]   # program ended: a later one at that airport is new
    out += [dict(n, source="NAS status") for n in page if n["kind"] not in ("gs", "gdp")]
    return out


def rebuild_nas(now):
    with LOCK:
        items = merge_nas(STATE.get("nas_page") or [], dict(swim.NAS), dict(swim.NAS_CANCELLED),
                          STATE.get("nas_at"), now)
        changed = [(n["key"], n.get("end"), tuple(n.get("scope") or [])) for n in items] != \
                  [(n["key"], n.get("end"), tuple(n.get("scope") or [])) for n in STATE["nas"]]
        STATE["nas"] = items
    if changed:
        bump()
    return changed


NAS_NEWS = set()   # airports whose SWIM ground stop / delay program just changed


def nas_news(now):
    """A ground stop or delay program SWIM just reported: rebuild the NAS list and check the
    flights touching that airport now (no page load), so the alert goes out within seconds."""
    if not NAS_NEWS:
        return set()
    aps = set(NAS_NEWS)
    NAS_NEWS.difference_update(aps)
    rebuild_nas(now)
    out = set()
    with LOCK:
        for L in STATE["legs"].values():
            f = L.get("flight") or {}
            inb = (((L.get("inbound") or {}).get("origin") or {}).get("iata") or "").upper()
            if watched(L, now) and aps & {(L.get("dep") or "").upper(), (L.get("arr") or "").upper(), inb,
                                          ((f.get("destination") or {}).get("iata") or "").upper()}:
                out.add(L["id"])
                SWIM_FAST.add(L["id"])
    if out:
        log.info("FAA SWIM: ground stop / delay program change at %s", ", ".join(sorted(aps)))
    return out


def wx_codes(now):
    codes = set()
    for L in list(STATE["legs"].values()):
        if watched(L, now) or (L["sched_out"] - now < 36 * HOUR and not finished(L, now)):
            for c in (L.get("dep"), L.get("arr")):
                codes.add(airports.icao(c) or ("K" + c if c and len(c) == 3 else ""))
    codes.discard("")
    return codes


def wx_missing(now):
    """Airports on a current flight with no METAR yet (a flight added since the last fetch)."""
    have = STATE.get("wx") or {}
    return [c for c in wx_codes(now) if c not in have]


def refresh_wx(now):
    with LOCK:
        codes = wx_codes(now)
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
            forced |= swim_news(now)
            forced |= nas_news(now)
            if now - last_sched > 300 or "all" in forced or "schedule" in forced:
                reload_schedule(force="all" in forced or "schedule" in forced)
                check_schedule_stale(now)
                expire_manual(now)
                clean_screenshots(now)
                sync_extras()
                last_sched = now
            if now - (STATE["nas_at"] or 0) > CFG["nas_minutes"] * 60 or "all" in forced:
                refresh_nas(now)
            if now - STATE.get("wx_at", 0) > 600 or "all" in forced \
                    or (now - STATE.get("wx_at", 0) > 60 and wx_missing(now)):
                refresh_wx(now)
            with LOCK:
                due = sorted((L["next_check"] if L["id"] not in forced and "all" not in forced else 0, L["id"])
                             for L in STATE["legs"].values() if watched(L, now) or first_lookup_due(L, now))
            for when, lid in due:
                if when <= time.time():
                    STATE["worker"]["busy"] = lid
                    update_leg(browser, lid)
            inbound_adsb(time.time())
            archive_finished(time.time())
            auto_recovery(time.time())
            swim_watch(time.time())
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

def api_surface(lid, now, want=None, want_ident=None):
    """Live surface positions (FAA STDDS) at the airport where this flight is on the ground: the
    departure airport until takeoff, the arrival airport once it's landing. Only for flights
    you're watching, and only while STDDS is connected."""
    with LOCK:
        L = STATE["legs"].get(lid)
        if not L or swim.STDDS_STATE.get("status") != "connected" or not watched(L, now):
            return {"targets": []}
        f = L.get("flight") or {}
        ph = fa.phase(f, now) if f else "scheduled"
        t = f.get("times") or {}
        dest = f.get("diverted_to") or (f.get("destination") or {}).get("iata") or L.get("arr")
        on_e = fa.best_time(t, "on") if t else None
        # the airplane's earlier legs (its inbound and the inbound's inbound): their airports too
        rot_aps = set()
        for r in (L.get("rotation") or []) + ([L["inbound"]] if L.get("inbound") else []):
            for side in ("origin", "destination"):
                rot_aps.add(((r.get(side) or {}).get("iata") or "").upper())
        if want and want.upper() in {(c or "").upper() for c in (L.get("dep"), L.get("arr"), dest)} | (rot_aps - {""}):
            code = want.upper()   # one of this flight's (or its inbound legs') airports
        elif ph in ("scheduled", "taxi_out"):
            code = L.get("dep")
        elif ph == "landed" or (ph == "airborne" and on_e and on_e - now < 6 * 60):
            code = dest
        else:
            return {"targets": [], "phase": ph}
        ident = (L.get("ident") or "").upper()
        if want_ident:   # an inbound leg's page: highlight that flight
            ident = want_ident.upper()
    ap = airports.get(code) or {}
    icao = (ap.get("icao") or "").upper()
    allt = swim.surface(icao)
    # field elevation from the slow movers (airports in the airports list carry none)
    slow = sorted(x["alt"] for x in allt if x.get("alt") is not None and (x.get("kt") or 0) < 40)
    elev = slow[len(slow) // 2] if slow else 0
    targets = [x for x in allt if x.get("cs") == ident or x.get("alt") is None
               or x["alt"] <= elev + 500]   # aircraft still in the air on approach aren't "on the surface"
    for x in targets:
        x["mine"] = bool(ident) and x.get("cs") == ident
        # answering the radar with a transponder: a callsign, a squawk or a transponder address
        x["xpdr"] = bool(x.get("cs") or x.get("squawk") or x.get("hex"))
        x.pop("t", None)
    return {"airport": ap.get("iata") or code, "icao": icao, "phase": ph,
            "center": [ap["lat"], ap["lon"]] if ap.get("lat") is not None else None, "targets": targets}


# ---------------------------------------------------------------- airport layout
# Runways, taxiways, terminals and gates from OpenStreetMap (current: EWR's new Terminal A is
# there), fetched once per airport from the Overpass API and kept for a month, so the surface map
# can label everything when you zoom in.
LAYOUT_DIR = os.path.join(DATA_DIR, "airport_layouts")
LAYOUT_TTL = 30 * 86400
_layout_lock = threading.Lock()
_layout_locks = {}
_layout_tried = {}
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")


def _layout_from_osm(data):
    """Overpass JSON -> compact features: {k: runway|taxiway|terminal|apron|gate|stand|hold, ref, name, g}.
    Lines and areas carry [[lat, lon], …]; points a single [lat, lon]."""
    out = []
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        kind = tags.get("aeroway")
        if el.get("type") == "relation" or not kind:
            continue
        kind = {"taxilane": "taxiway", "parking_position": "stand", "holding_position": "hold",
                "hangar": "terminal"}.get(kind, kind)
        if kind not in ("runway", "taxiway", "terminal", "apron", "gate", "stand", "hold"):
            continue
        ref = tags.get("ref") or tags.get("name:short")
        name = tags.get("name")
        if el.get("type") == "node":
            if el.get("lat") is None or not (ref or name):
                continue
            g = [round(el["lat"], 6), round(el["lon"], 6)]
        else:
            g = [[round(p["lat"], 6), round(p["lon"], 6)] for p in el.get("geometry") or [] if p]
            if len(g) < 2:
                continue
        f = {"k": kind, "g": g}
        if ref:
            f["ref"] = ref
        if name and name != ref:
            f["name"] = name
        out.append(f)
    return out


def airport_layout(code):
    """The airport's layout (see above), from the cache or fetched now. -> {airport, features} or None."""
    ap = airports.get(code) or {}
    icao = (ap.get("icao") or "").upper()
    if not icao or ap.get("lat") is None:
        return None
    path = os.path.join(LAYOUT_DIR, f"{icao}.json")
    def cached():
        try:
            if time.time() - os.path.getmtime(path) < LAYOUT_TTL:
                with open(path) as fh:
                    return json.load(fh)
        except (OSError, ValueError):
            pass
        return None
    c = cached()          # saved copies are read without waiting on anything
    if c:
        return c
    with _layout_lock:
        lk = _layout_locks.setdefault(icao, threading.Lock())
    with lk:              # one download per airport at a time; other airports aren't held up
        c = cached()
        if c:
            return c
        _layout_tried[icao] = time.time()
        q = (f'[out:json][timeout:90];('
             f'way["aeroway"~"^(runway|taxiway|taxilane|terminal|apron)$"](around:5000,{ap["lat"]},{ap["lon"]});'
             f'node["aeroway"~"^(gate|parking_position|holding_position)$"](around:5000,{ap["lat"]},{ap["lon"]}););'
             f'out geom;')
        err = None
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=("data=" + urllib.parse.quote(q)).encode(),
                                             headers={"User-Agent": f"HA-Flight-Tracker/{VERSION}"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.load(r)
                feats = _layout_from_osm(data)
                out = {"airport": ap.get("iata") or code, "icao": icao, "at": time.time(), "features": feats}
                os.makedirs(LAYOUT_DIR, exist_ok=True)
                with open(path + ".tmp", "w") as fh:
                    json.dump(out, fh, separators=(",", ":"))
                os.replace(path + ".tmp", path)
                log.info("Airport layout for %s: %d runways, taxiways, terminals and gates from OpenStreetMap",
                         icao, len(feats))
                return out
            except Exception as e:
                err = e
        log.warning("Airport layout for %s: %s", icao, err)
        try:
            with open(path) as fh:  # an old copy beats none
                return json.load(fh)
        except OSError:
            return None


def layout_prefetch_loop():
    """Download the layout of every airport of an upcoming flight before anyone opens its map,
    one at a time, in the background, starting a minute after start-up. An airport that failed is
    tried again after an hour."""
    time.sleep(60)
    while True:
        try:
            with LOCK:
                codes = set()
                now = time.time()
                for L in STATE["legs"].values():
                    # every upcoming flight, not only the ones being watched now: the add-on does
                    # the slow download ahead of time, so the first open of a map is instant
                    if L.get("dep") and not finished(L, now):
                        f = L.get("flight") or {}
                        codes.update(c for c in (L.get("dep"), L.get("arr"), (f.get("destination") or {}).get("iata"),
                                                  ((L.get("inbound") or {}).get("origin") or {}).get("iata")) if c)
            for code in sorted(codes):
                icao = ((airports.get(code) or {}).get("icao") or "").upper()
                if not icao or time.time() - _layout_tried.get(icao, 0) < 3600:
                    continue
                p = os.path.join(LAYOUT_DIR, f"{icao}.json")
                try:
                    if time.time() - os.path.getmtime(p) < LAYOUT_TTL - 86400:
                        continue
                except OSError:
                    pass
                airport_layout(code)
                time.sleep(5)
        except Exception:
            log.exception("airport layout prefetch")
        time.sleep(300)


# ---------------------------------------------------------------- ARTCC boundaries
# The 20-odd en-route centers (ZNY, ZDC …) as polygons, from the FAA's own aeronautical data
# (AIS open data, public domain), fetched once and kept for 90 days. Used to draw a ground stop's
# or delay program's departure scope on a map.
ARTCC_URL = ("https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/Boundary_Airspace/FeatureServer/0/query")
ARTCC_PATH = os.path.join(DATA_DIR, "artcc_boundaries_v2.json")

# ---------- place names and borders for the satellite map ----------
# Esri's label layer is a set of pictures: soft on a phone, and the text jumps in size between tile
# zooms. Instead the app draws names itself, crisp at every zoom, from Natural Earth (public domain):
# countries, states/provinces, ~7,000 cities with the zoom each deserves, and country/state borders.
# Fetched once (about 10 MB), kept as a small file for 180 days.
NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
NE_FILES = {"cities": "ne_10m_populated_places_simple.geojson", "countries": "ne_50m_admin_0_countries.geojson",
            "states": "ne_50m_admin_1_states_provinces.geojson", "borders": "ne_50m_admin_0_boundary_lines_land.geojson",
            "state_lines": "ne_50m_admin_1_states_provinces_lines.geojson"}
PLACES_PATH = os.path.join(DATA_DIR, "places_v1.json")
_places_lock = threading.Lock()
_places_mem = {"d": None, "tag": None}


# map-friendly names for countries whose official ones are long
SHORT_NAMES = {"United States of America": "United States", "Dominican Rep.": "Dominican Republic", "Bosnia and Herz.": "Bosnia",
               "Central African Rep.": "C. African Rep.", "Dem. Rep. Congo": "DR Congo", "Democratic Republic of the Congo": "DR Congo",
               "Republic of the Congo": "Congo", "United Kingdom of Great Britain and Northern Ireland": "United Kingdom",
               "Russian Federation": "Russia", "The Bahamas": "Bahamas", "Turks and Caicos Is.": "Turks & Caicos",
               "Trinidad and Tobago": "Trinidad & Tobago", "Antigua and Barb.": "Antigua", "St. Kitts and Nevis": "St. Kitts",
               "St. Vin. and Gren.": "St. Vincent", "Cayman Is.": "Cayman Islands", "British Virgin Is.": "BVI",
               "U.S. Virgin Is.": "USVI", "Falkland Is.": "Falklands", "Solomon Is.": "Solomon Islands"}


def _ne_get(name):
    req = urllib.request.Request(NE_BASE + name, headers={"User-Agent": f"HA-Flight-Tracker/{VERSION}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def _lower(props):
    return {str(k).lower(): v for k, v in (props or {}).items()}


def _label_point(geom):
    """A point inside the biggest ring of a (multi)polygon: its vertex average (good enough for a label)."""
    polys = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        polys = [polys]
    best = max((p[0] for p in polys if p and p[0]), key=len, default=None)
    if not best:
        return None
    return sum(pt[1] for pt in best) / len(best), sum(pt[0] for pt in best) / len(best)


def _lines(fc, digits=2):
    out = []
    for ft in fc.get("features") or []:
        g = ft.get("geometry") or {}
        parts = [g.get("coordinates")] if g.get("type") == "LineString" else g.get("coordinates") or [] if g.get("type") == "MultiLineString" else []
        for line in parts:
            pts, last = [], None
            for x, y, *_ in line:
                q = [round(x, digits), round(y, digits)]
                if q != last:
                    pts.append(q)
                last = q
            if len(pts) > 1:
                out.append(pts)
    return out


def _build_places():
    out = {"v": 1, "countries": [], "states": [], "cities": [], "borders": [], "state_lines": [], "missing": []}
    got = {}
    for k, name in NE_FILES.items():
        try:
            got[k] = _ne_get(name)
        except Exception as e:
            log.warning("Place names: couldn't fetch %s (%s)", name, e)
            out["missing"].append(k)
    for ft in (got.get("countries") or {}).get("features") or []:
        p = _lower(ft.get("properties"))
        nm = p.get("name_en") or p.get("name")
        lat, lon = p.get("label_y"), p.get("label_x")
        if lat is None or lon is None:
            lp = _label_point(ft.get("geometry") or {})
            if not lp:
                continue
            lat, lon = lp
        nm = SHORT_NAMES.get(nm, nm)
        if nm:
            out["countries"].append([nm, round(lat, 3), round(lon, 3), float(p.get("min_label") or p.get("labelrank") or 3)])
    for ft in (got.get("states") or {}).get("features") or []:
        p = _lower(ft.get("properties"))
        nm = p.get("name_en") or p.get("name")
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None:
            lp = _label_point(ft.get("geometry") or {})
            if not lp:
                continue
            lat, lon = lp
        if nm:
            out["states"].append([nm, round(float(lat), 3), round(float(lon), 3), float(p.get("min_label") or 4)])
    for ft in (got.get("cities") or {}).get("features") or []:
        p = _lower(ft.get("properties"))
        nm = p.get("name_en") or p.get("name") or p.get("nameascii")
        lat, lon = p.get("latitude"), p.get("longitude")
        if (lat is None or lon is None) and (ft.get("geometry") or {}).get("type") == "Point":
            lon, lat = ft["geometry"]["coordinates"][:2]
        if not nm or lat is None:
            continue
        mz = p.get("min_zoom")
        mz = float(mz) if mz not in (None, "") else 4 + float(p.get("scalerank") or 6) / 2
        out["cities"].append([nm, round(float(lat), 3), round(float(lon), 3), mz, int(p.get("pop_max") or 0)])
    out["cities"].sort(key=lambda c: -c[4])
    out["borders"] = _lines(got.get("borders") or {})
    out["state_lines"] = _lines(got.get("state_lines") or {})
    log.info("Place names: %d countries, %d states, %d cities, %d border lines", len(out["countries"]), len(out["states"]),
             len(out["cities"]), len(out["borders"]) + len(out["state_lines"]))
    return out


def places_data():
    """The compact place file (and its ETag), built on first use."""
    with _places_lock:
        if _places_mem["d"] is not None:
            return _places_mem["d"], _places_mem["tag"]
        d = None
        try:
            if time.time() - os.path.getmtime(PLACES_PATH) < 180 * 86400:
                with open(PLACES_PATH, "rb") as fh:
                    d = fh.read()
        except OSError:
            pass
        if d is None:
            built = _build_places()
            if not built["cities"] and not built["countries"]:
                return None, None
            d = json.dumps(built, separators=(",", ":")).encode()
            if not built["missing"]:
                os.makedirs(os.path.dirname(PLACES_PATH), exist_ok=True)
                with open(PLACES_PATH + ".tmp", "wb") as fh:
                    fh.write(d)
                os.replace(PLACES_PATH + ".tmp", PLACES_PATH)
        _places_mem["d"], _places_mem["tag"] = d, '"p%s"' % hashlib.md5(d).hexdigest()[:12]
        return _places_mem["d"], _places_mem["tag"]
_artcc_lock = threading.Lock()


def _ring_area(r):
    return sum(r[i][1] * r[i + 1][0] - r[i + 1][1] * r[i][0] for i in range(len(r) - 1)) / 2


def _artcc_from_geojson(data):
    """FAA boundary features -> {ZNY: {name, rings: [[[lat, lon], …], …], parts: [...]}}: every ARTCC
    shape of a center (its low- and high-altitude boundaries differ: drawing only one left part of
    New York center uncovered), all wound the same way so the page can fill their union once."""
    out = {}
    for ft in data.get("features") or []:
        p = ft.get("properties") or {}
        ident = (p.get("IDENT") or "").upper()
        if len(ident) == 4 and ident.startswith("KZ"):
            ident = ident[1:]
        if not re.fullmatch(r"Z[A-Z]{2}", ident):
            continue
        g = ft.get("geometry") or {}
        polys = g.get("coordinates") or []
        if g.get("type") == "Polygon":
            polys = [polys]
        elif g.get("type") != "MultiPolygon":
            continue
        c = out.setdefault(ident, {"name": (p.get("NAME") or "").title() or ident, "rings": [], "parts": []})
        c["parts"].append(" ".join(str(x) for x in (p.get("TYPE_CODE"), p.get("LOCAL_TYPE"), p.get("LEVEL_")) if x))
        for poly in polys:
            if not poly or not poly[0]:
                continue
            ring = [[round(pt[1], 3), round(pt[0], 3)] for pt in poly[0]]
            if _ring_area(ring) < 0:
                ring.reverse()
            if ring not in c["rings"]:
                c["rings"].append(ring)
    return out


def artcc_boundaries():
    with _artcc_lock:
        try:
            if time.time() - os.path.getmtime(ARTCC_PATH) < 90 * 86400:
                with open(ARTCC_PATH) as fh:
                    return json.load(fh)
        except OSError:
            pass
        for where in ("TYPE_CODE LIKE 'ARTCC%' OR LOCAL_TYPE LIKE 'ARTCC%'", "TYPE_CODE = 'ARTCC'", "IDENT LIKE 'Z%'"):
            q = urllib.parse.urlencode({
                "where": where, "outFields": "*", "returnGeometry": "true", "outSR": "4326",
                "maxAllowableOffset": "0.02", "geometryPrecision": "3", "resultRecordCount": "2000", "f": "geojson"})
            try:
                req = urllib.request.Request(ARTCC_URL + "?" + q, headers={"User-Agent": f"HA-Flight-Tracker/{VERSION}"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    out = _artcc_from_geojson(json.load(r))
                if len(out) >= 15:
                    os.makedirs(os.path.dirname(ARTCC_PATH), exist_ok=True)
                    with open(ARTCC_PATH + ".tmp", "w") as fh:
                        json.dump(out, fh, separators=(",", ":"))
                    os.replace(ARTCC_PATH + ".tmp", ARTCC_PATH)
                    log.info("ARTCC boundaries: %d centers from the FAA", len(out))
                    return out
                log.info("ARTCC boundaries: only %d centers for %s", len(out), where)
            except Exception as e:
                log.warning("ARTCC boundaries (%s): %s", where, e)
        try:
            with open(ARTCC_PATH) as fh:
                return json.load(fh)
        except OSError:
            return None


LOG_SHOWN = 40   # lookup-log entries sent with a flight page; the rest on request


def rotation_edct(h, now):
    """An inbound leg's EDCT from the FAA's TFMS feed (it carries every controlled flight, not only
    yours): matched by callsign, departure airport and that day's departure time. None once it's
    taken off."""
    t = h.get("times") or {}
    if (t.get("off") or {}).get("actual") or not h.get("ident"):
        return None
    org = _iata((h.get("origin") or {}).get("iata") or (h.get("origin") or {}).get("icao"))
    so = fa.best_time(t, "out") or (t.get("out") or {}).get("scheduled") or 0
    best = None
    for ev in swim.events(h["ident"]):
        if ev.get("type") != "EDCT" or not ev.get("edct"):
            continue
        e = _ts(ev["edct"])
        if not e or (org and ev.get("dep") and _iata(ev["dep"]) != org):
            continue
        if so and not (so - 2 * HOUR <= e <= so + 10 * HOUR):
            continue   # another day's flight with the same number
        if not best or (ev.get("got") or 0) >= best["got"]:
            best = {"at": e, "cta": _ts(ev.get("cta")) if ev.get("cta") else None,
                    "for": _iata(ev.get("element")) if ev.get("element") else None, "got": ev.get("got") or 0}
    if best:
        best.pop("got", None)
    return best


def leg_summary(L, now, nas, full=False, rotation_tracks=False, log_all=False):
    f = L.get("flight")
    out = {k: L.get(k) for k in ("id", "ident", "display", "airline", "flight_no", "dep", "arr", "dh", "extra", "kind",
                                  "sched_out", "sched_in", "summary", "source", "note", "error", "updated",
                                  "next_check", "shot", "fa_id", "date", "manual", "tail_info", "chain")}
    if full:
        hist = list(reversed(L.get("history") or []))
        out["history"] = hist if log_all else hist[:LOG_SHOWN]
        out["history_more"] = max(0, len(hist) - len(out["history"]))
        out["rotation"] = []
        for i, h in enumerate(L.get("rotation") or []):
            if not h:
                continue
            # the airplane's earlier legs' tracks only for the aircraft page (they're big)
            r = {k: v for k, v in h.items() if k not in ("track", "waypoints")}
            if rotation_tracks:
                r["track"] = h.get("track") or []
                r["waypoints"] = h.get("waypoints") or []
            r["phase"] = fa.phase(h, now)
            r["status"] = alerting.PHASE_LABEL[r["phase"]]
            r["dep_delay"] = fa.delay_minutes(h)
            r["arr_delay"] = fa.arrival_delay_minutes(h)
            r["log"] = list(reversed(((L.get("rot_swim") or {}).get(h.get("rot_key") or "") or {}).get("log") or []))
            try:
                r["edct"] = rotation_edct(h, now)
            except Exception:
                r["edct"] = None
            out["rotation"].append(r)
    out["status"] = alerting.status_label(L, CFG)
    out["phase"] = fa.phase(f, now)
    out["watching"] = watched(L, now)
    out["finished"] = finished(L, now)
    if out["finished"] and out["phase"] == "scheduled":
        out["status"] = "Completed"
    elif out["finished"] and out["phase"] in ("airborne", "landed"):
        out["status"] = "Departed"
    out["problems"] = alerting.problems(L, CFG, nas if out["watching"] else [], rules=STATE['rules'])
    out["delay"] = fa.delay_minutes(f, L["sched_out"]) if f else None
    out["arr_delay"] = fa.arrival_delay_minutes(f, L["sched_in"]) if f else None
    t = alerting.turn_minutes(L)
    out["turn"] = round(t) if t is not None else None
    if f and f.get("diverted_to"):
        out["arr"], out["planned_arr"] = f["diverted_to"], f.get("planned_arr")
    for side, code in (("dep_ap", L.get("dep")), ("arr_ap", out.get("arr"))):
        ref = airports.get(code)
        fl = (f or {}).get("origin" if side == "dep_ap" else "destination") or {}
        out[side] = {**ref, **{k: v for k, v in fl.items() if v is not None}}
        if not out[side].get("tz"):
            out[side]["tz"] = TZNAME
    out["nas"] = alerting.nas_for(L, nas)
    if f:
        # the list only needs what its cards show: the route drawing, SWIM details and per-time
        # sources go with the flight page
        fl = {k: v for k, v in f.items() if k not in ("track", "waypoints") and
              (full or k not in ("swim_route", "swim", "time_src", "route_flightaware"))}
        if full:
            fl["track"] = f.get("track") or []
            fl["waypoints"] = f.get("waypoints") or []
        else:
            tr = f.get("track") or []
            fl["last_position"] = tr[-1] if tr else None
            # the EDCT is on the list too, so a flight's page opens with it (not after its details load)
            e = (f.get("swim") or {}).get("edct")
            if e:
                fl["swim"] = {"edct": e}
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
            "map_tiles": {"url": (CFG.get("map_tile_url") or "").strip(), "key": (CFG.get("map_api_key") or "").strip(),
                          "google": (CFG.get("google_maps_api_key") or "").strip(),
                          "mapbox": (CFG.get("mapbox_token") or "").strip()},
            "notify_target": CFG.get("notify_service") or "persistent notification",
            "gemini": bool(CFG.get("gemini_api_key")),
            "aeroapi": aero_on(),
            "swim": {k: swim.STATE.get(k) for k in ("status", "error", "rate", "matched", "last_message")},
            "tfdm": {k: swim.TFDM_STATE.get(k) for k in ("status", "error", "rate", "matched", "last_message")},
            "stdds": {k: swim.STDDS_STATE.get(k) for k in ("status", "error", "rate", "matched", "last_message")},
            "tbfm": {k: swim.TBFM_STATE.get(k) for k in ("status", "error", "rate", "matched", "last_message")},
        }
    data["backup"] = backup_brief()
    return data


def backup_brief():
    """The Drive pill in the header: off / synced / waiting / syncing / error."""
    try:
        g = gdrive.state()
    except Exception:
        g = {}
    if not gdrive.configured() and not g.get("refresh_token"):
        return {"state": "off"}
    st = ("syncing" if BACKUP["running"] else "error" if g.get("last_error") else
          "disconnected" if not g.get("refresh_token") else "waiting" if BACKUP["dirty"] else "synced")
    return {"state": st, "last": g.get("last_backup"), "error": g.get("last_error")}


def api_debug():
    now = time.time()
    with LOCK:
        path = CFG["ics_file_path"]
        legs = sorted((L for L in STATE["legs"].values() if not L.get("extra")), key=lambda L: L["sched_out"])
        return {
            "now": now, "version": VERSION,
            "worker": STATE["worker"],
            "fetches": list(reversed(STATE["debug"])),
            "schedule": {**STATE["schedule"], "path": path, "exists": os.path.exists(path),
                         "legs": [{"summary": L["summary"], "ident": L.get("ident"), "dep": L["dep"],
                                   "arr": L["arr"], "dh": L.get("dh"), "sched_out": L["sched_out"],
                                   "watching": watched(L, now), "finished": finished(L, now)}
                                  for L in legs[:40]]},
            "config": {"airline_icao": CFG["airline_icao"] or "(not set)",
                       "notify": f"notify.{CFG['notify_service']}" if CFG.get("notify_service")
                       else "persistent notification (notify_service not set)",
                       "ingress": f"{INGRESS['path']} (from {INGRESS.get('how', '?')})" if INGRESS.get("path") else "(unknown: set notification_url)", "gemini": bool(CFG.get("gemini_api_key")),
                       "gemini_model": CFG.get("gemini_model"), "watch_hours": CFG["watch_hours"],
                       "home_tz": TZNAME, "viewer_port": 6081},
            "manual": STATE["manual"],
            "faa": faa.status(),
            "screenshots": dict(zip(("count", "bytes"), shot_usage()), keep_hours=SHOT_KEEP_HOURS),
            "aeroapi": {"enabled": bool(CFG.get("aeroapi_key")),
                        "budget": float(CFG.get("aeroapi_monthly_budget") or 0),
                        "queries": aero_month(now)[0], "spent": round(aero_month(now)[1], 3),
                        "calls": list(reversed(STATE["aero_calls"][-30:]))},
            "gemini": {"enabled": bool(CFG.get("gemini_api_key")), "model": CFG.get("gemini_model"),
                       "last": STATE["gemini_calls"][-1] if STATE["gemini_calls"] else None,
                       "today": sum(1 for t in STATE["gemini_calls"]
                                    if datetime.fromtimestamp(t, HOME_TZ).date() == datetime.now(HOME_TZ).date()),
                       "month": sum(1 for t in STATE["gemini_calls"]
                                    if datetime.fromtimestamp(t, HOME_TZ).strftime("%Y-%m")
                                    == datetime.now(HOME_TZ).strftime("%Y-%m"))},
        }


def api_history(q):
    recs = history.flights()
    year = (q.get("year") or "").strip()
    sel = [r for r in recs if not year or (r.get("date") or "").startswith(year)]
    sel.sort(key=lambda r: (r.get("date") or "", ((r.get("times") or {}).get("out") or {}).get("s") or 0), reverse=True)
    lk = history.links(recs)
    # city names and positions for every airport in the list (the Passport globe and "Miami to Newark")
    aps = {}
    for r in sel:
        for c in (r.get("dep"), r.get("arr"), r.get("diverted_to")):
            if c and c not in aps:
                a = airports.get(c)
                aps[c] = {k: a.get(k) for k in ("city", "name", "lat", "lon")} if a else {}
    return {"year": year, "years": sorted({(r.get("date") or "")[:4] for r in recs if r.get("date")}, reverse=True),
            "total": len(recs), "flighty_count": sum(1 for r in recs if r.get("source") == "flighty"),
            "stats": history.stats(sel, lk), "flights": [history.summary(r, lk) for r in sel], "airports": aps}


def api_airports_status():
    """Every US airport the FAA lists right now (ground stops, delay programs, closures, delay
    trends, deicing), grouped by airport with its name and position. Built from the NAS data the
    tracker already fetches, so it costs no extra calls."""
    with LOCK:
        nas, at, err = list(STATE["nas"]), STATE.get("nas_at"), STATE.get("nas_error")
    mine = set()
    with LOCK:
        for L in STATE["legs"].values():
            mine.update(x for x in (L.get("dep"), L.get("arr")) if x)
    aps = {}
    for n in nas:
        c = n.get("airport")
        if not c:
            continue
        if c not in aps:
            a = airports.get(c)
            aps[c] = {"code": c, "name": a.get("name") or c, "city": a.get("city") or "", "lat": a.get("lat"),
                      "lon": a.get("lon"), "tz": a.get("tz") or "", "mine": c in mine, "items": []}
        aps[c]["items"].append(n)
    return {"at": at, "error": err, "airports": list(aps.values())}


def api_history_flight(fid):
    r = history.get(fid)
    if not r:
        return None
    all_ = history.flights()
    lk = history.links(all_)
    out = history.summary(r, lk)
    for k in ("continued_by", "continuation_of"):
        if out.get(k):
            o = history.get(out[k])
            out[k + "_label"] = f"{o.get('display')} {o.get('dep')}→{o.get('diverted_to') or o.get('arr')}" if o else ""
    out["track"] = history.track(fid)
    out["history"] = list(reversed(history.lookup_log(fid)))   # newest first, like a live flight's
    out["dep_ap"], out["arr_ap"] = airports.get(r.get("dep")), airports.get(r.get("diverted_to") or r.get("arr"))
    same = [x for x in all_ if x.get("dep") == r.get("dep") and x.get("arr") == r.get("arr")]
    st = history.stats(same, lk)
    out["route_stats"] = {k: st[k] for k in ("flights", "on_time_pct", "avg_arr_delay", "avg_dep_delay")}
    tails = [x for x in history.flights() if r.get("tail") and x.get("tail") == r.get("tail")]
    out["tail_count"] = len(tails)
    return out


def api_leg(lid, rotation_tracks=False, log_all=False):
    now = time.time()
    with LOCK:
        L = STATE["legs"].get(lid)
        out = leg_summary(L, now, list(STATE["nas"]), full=True, rotation_tracks=rotation_tracks,
                          log_all=log_all) if L else None
    if out:
        same = [x for x in history.flights() if x.get("dep") == L.get("dep") and x.get("arr") == L.get("arr")]
        if same:
            st = history.stats(same)
            out["route_stats"] = {k: st[k] for k in ("flights", "on_time_pct", "avg_arr_delay")}
        counts = {}
        for x in history.flights():
            t_ = (x.get("tail") or "").upper()
            if t_:
                counts[t_] = counts.get(t_, 0) + 1
        tail = ((L.get("flight") or {}).get("tail") or "").upper()
        if tail:
            out["tail_count"] = counts.get(tail, 0)
        for r in out.get("rotation") or []:   # the inbound legs' airplanes too
            if (r.get("tail") or "").strip():
                r["tail_count"] = counts.get(r["tail"].upper(), 0)
    return out


STATIC = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
          ".webmanifest": "application/manifest+json"}


# ---------------------------------------------------------------- fast loading
# Web files are read once and kept in memory (re-read when they change on disk), each with an ETag
# from its content. index.html links app.js, style.css and the icons with ?v=<that tag>, so the
# phone keeps those for a year and only re-checks index.html (a 304 when nothing changed).
_FILES = {}
_COMPRESSIBLE = ("text/", "application/json", "application/manifest+json", "image/svg+xml")
_ASSET_REF = re.compile(r'(?<=["\'])((?:app\.js|style\.css|leaflet\.(?:js|css)|icon[\w.-]*\.(?:png|svg)|manifest\.webmanifest))(?=["\'])')


def static_file(full):
    st = os.stat(full)
    hit = _FILES.get(full)
    if hit and hit["mtime"] == st.st_mtime and hit["size"] == st.st_size:
        return hit
    with open(full, "rb") as fh:
        body = fh.read()
    hit = {"mtime": st.st_mtime, "size": st.st_size, "body": body, "gz": None,
           "ctype": STATIC.get(os.path.splitext(full)[1], "application/octet-stream"),
           "etag": '"' + hashlib.sha1(body).hexdigest()[:16] + '"'}
    _FILES[full] = hit
    return hit


def web_file(name, www=WWW):
    """A web file's path."""
    return os.path.join(WWW, name)


def versioned_index(www=WWW):
    page = static_file(web_file("index.html", www))
    if page.get("versioned") and all(static_file(web_file(n, www))["etag"] == t for n, t in page["deps"].items()):
        return page["versioned"]
    deps = {}

    def add(m):
        full = web_file(m.group(1), www)
        if not os.path.isfile(full):
            return m.group(1)
        f = static_file(full)
        deps[m.group(1)] = f["etag"]
        return f'{m.group(1)}?v={f["etag"].strip(chr(34))[:10]}'
    body = _ASSET_REF.sub(add, page["body"].decode("utf-8")).encode("utf-8")
    page["versioned"] = {"body": body, "ctype": STATIC[".html"], "gz": None,
                         "etag": '"' + hashlib.sha1(body).hexdigest()[:16] + '"'}
    page["deps"] = deps
    return page["versioned"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", cache="no-store", etag=None, entry=None):
        """Send a response: 304 when the phone already has this exact version, gzip on the direct
        port (through Home Assistant's sidebar, its proxy compresses the connection to the phone)."""
        if isinstance(body, (dict, list)) or body is None:
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        if etag and code == 200 and etag in [t.strip() for t in (self.headers.get("If-None-Match") or "").split(",")]:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        gz = None
        direct = not self.headers.get("X-Ingress-Path")
        if direct and len(body) > 1024 and ctype.startswith(_COMPRESSIBLE) and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            if entry is not None:
                if entry.get("gz") is None:
                    entry["gz"] = gzip.compress(body, 6)
                gz = entry["gz"]
            else:
                gz = gzip.compress(body, 6)
        out = gz or body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
        if direct:
            self.send_header("Vary", "Accept-Encoding")
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        self.wfile.write(out)

    def _send_json_tagged(self, obj):
        """JSON the page polls often: an ETag so an unchanged answer is a tiny 304."""
        body = json.dumps(obj).encode()
        self._send(200, body, "application/json", "no-cache", '"' + hashlib.sha1(body).hexdigest()[:16] + '"')

    def _leaflet(self):
        """/leaflet/... is the same app (it was the Leaflet test copy): same files, same API."""
        p = self.path.split("?", 1)[0]
        if p == "/leaflet" or p.startswith("/leaflet/"):
            self.path = self.path[len("/leaflet"):] or "/"
            if not self.path.startswith("/"):
                self.path = "/" + self.path
        self.www = WWW

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/leaflet":   # the page needs the trailing slash for its relative links
            self.send_response(301)
            self.send_header("Location", "leaflet/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._leaflet()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            path = "/index.html"
        if path == "/api/state":
            d = api_state()
            d.pop("now", None)  # the page has its own clock; leaving it out lets an unchanged state be a 304
            return self._send_json_tagged(d)
        if path == "/api/events":
            return self._events()
        if path == "/api/debug":
            return self._send(200, api_debug())
        if path == "/api/rules":
            with LOCK:
                return self._send(200, {"rules": rules_mod.catalog_for_ui(STATE["rules"])})
        if path == "/api/settings":
            with LOCK:
                return self._send(200, {"settings": [{"key": k, "kind": v[0], "min": v[1], "max": v[2], "label": v[3],
                                                      "help": v[4], "group": v[5], "value": STATE["app_settings"].get(k)}
                                                     for k, v in APP_SETTINGS.items()]})
        if path == "/api/history":
            return self._send_json_tagged(api_history(self._query()))
        m = re.match(r"^/api/history/([\w-]+)$", path)
        if m:
            d = api_history_flight(m.group(1))
            return self._send(200 if d else 404, d or {"error": "not found"})
        if path == "/api/backup":
            return self._send(200, gdrive.status())
        if path == "/api/backup/brief":
            return self._send(200, backup_brief())
        if path == "/api/backup/download":
            blob = history.make_backup(backup_extra())
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="flight-tracker-backup-{datetime.now(HOME_TZ):%Y-%m-%d}.zip"')
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        if path == "/api/swim/samples.zip":
            blob = swim.samples_zip()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="swim-samples.zip"')
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        if path == "/api/history/export.csv":
            blob = history.export_csv()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="flight-history.csv"')
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        if path == "/api/alerts":
            with LOCK:
                return self._send(200, {"alerts": list(reversed(STATE["alerts"]))})
        if path == "/api/lookup":
            return self._send(200, api_lookup(self._query()))
        if path == "/api/airports/status":
            return self._send(200, api_airports_status())
        if path == "/api/push/key":
            return self._send(200, {"key": VAPID.public if VAPID else None, "count": len(push_subs())})
        if path == "/api/places":
            d, tag = places_data()
            return self._send(200, d, cache="private, max-age=604800", etag=tag) if d else self._send(503, {"error": "place names not available yet"})
        if path == "/api/artcc":
            d = artcc_boundaries()
            return self._send(200, d, cache="private, max-age=86400") if d else self._send(404, {"error": "no boundaries"})
        m = re.match(r"^/api/airport/([A-Za-z0-9]{3,4})/layout$", path)
        if m:
            d = airport_layout(m.group(1).upper())
            return self._send(200, d, cache="private, max-age=86400") if d else self._send(404, {"error": "no layout"})
        m = re.match(r"^/api/live/([\w-]+)$", path)
        if m:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                since = float((qs.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            d = api_live(m.group(1), since)
            return self._send(200, d) if d else self._send(404, {"error": "not found"})
        m = re.match(r"^/api/surface/([\w-]+)$", path)
        if m:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            want = (qs.get("ap") or [None])[0]
            return self._send(200, api_surface(m.group(1), time.time(), want, (qs.get("ident") or [None])[0]))
        m = re.match(r"^/api/leg/([\w-]+)$", path)
        if m:
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            d = api_leg(m.group(1), rotation_tracks="rotation" in qs, log_all="log" in qs)
            if d:
                d.pop("now", None)
                return self._send_json_tagged(d)
            return self._send(404, {"error": "not found"})
        if path == "/api/net":   # is the phone on the home network? (a private address, not Tailscale's 100.64/10)
            ip = self.client_address[0]
            try:
                import ipaddress
                a = ipaddress.ip_address(ip)
                a = getattr(a, "ipv4_mapped", None) or a
                home = a.is_private and not a.is_loopback and not any(a in ipaddress.ip_network(n) for n in ("100.64.0.0/10", "fd7a:115c:a1e0::/48")) \
                    and not self.headers.get("X-Ingress-Path")
            except ValueError:
                home = False
            return self._send(200, {"home": bool(home)})
        m = re.match(r"^/api/tail/(N[0-9A-Za-z]{1,5})$", path)
        if m:   # FAA registry: model, year built, airworthiness date
            q = self._query()
            d = faa.lookup(m.group(1), q.get("type"))
            return self._send(200, {"found": bool(d), **(d or {}), "registry": faa.status()}, cache="no-cache")
        if path == "/api/liveries":
            return self._send(200, livery.index(), cache="no-cache")
        m = re.match(r"^/api/livery/(N[0-9A-Z]{1,5})\.png$", path)
        if m:
            p = livery.path(LIVERY_DIR, m.group(1))
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/png", cache="public, max-age=31536000, immutable")
            return self._send(404, {"error": "no picture"})
        m = re.match(r"^/api/shot/([\w-]+)\.png$", path)
        if m:
            p = os.path.join(SHOT_DIR, m.group(1) + ".png")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            return self._send(404, {"error": "no screenshot"})
        name = os.path.basename(path)
        if name == "index.html":
            pg = versioned_index(self.www)
            return self._send(200, pg["body"], pg["ctype"], "no-cache", pg["etag"], pg)
        p = web_file(name, self.www)
        ext = os.path.splitext(name)[1]
        if ext in STATIC and os.path.isfile(p):
            f = static_file(p)
            # a ?v= link names this exact content, so the phone can keep it; otherwise it re-checks
            keep = "v=" in (self.path.split("?", 1)[1] if "?" in self.path else "")
            return self._send(200, f["body"], f["ctype"], "public, max-age=31536000, immutable" if keep else "no-cache",
                              f["etag"], f)
        return self._send(404, {"error": "not found"})

    def _json(self):
        try:
            raw = self._small if getattr(self, "_small", None) is not None else self._body()
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _events(self):
        """Server-sent events: one message per change, a comment every 20 s to keep it open."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        v = CHANGE_V[0]
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                with CHANGE:
                    CHANGE.wait_for(lambda: CHANGE_V[0] != v, timeout=15)
                if CHANGE_V[0] != v:
                    v = CHANGE_V[0]
                    self.wfile.write(f"data: {v}\n\n".encode())
                else:
                    # a named event (not a comment) so the page can tell the stream is still alive
                    self.wfile.write(f"event: ping\ndata: {v}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _query(self):
        from urllib.parse import parse_qs, urlsplit
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _body(self, limit=20 * 1024 * 1024):
        if getattr(self, "_small", None) is not None:
            return self._small
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("File too big")
        return self.rfile.read(n) if n else b""

    def do_POST(self):
        self._leaflet()
        path = self.path.split("?", 1)[0]
        # read a small body up front, whether or not the endpoint uses it: answering and closing with
        # the request's body still unread makes the connection end in a reset, which the iPhone reports
        # as "The network connection was lost" even though the action went through
        self._small = None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= 1024 * 1024:
            self._small = self.rfile.read(n)
        elif n == 0:
            self._small = b""
        if path == "/api/history/import":
            try:
                name = self.headers.get("X-Filename") or "import.xlsx"
                body = self._body()
                res = (history.import_tracks(body) if name.lower().endswith(".zip")
                       else history.import_flighty(name, body))
            except Exception as e:
                log.warning("History import: %s", e)
                return self._send(400, {"error": str(e)[:300]})
            log.info("History import %s: %s", name, res)
            mark_dirty()
            bump()
            return self._send(200, res)
        if path == "/api/history/clear":
            what = (self._json() or {}).get("what")
            n = history.clear("flighty" if what == "flighty" else None) if what in ("flighty", "all") else 0
            log.info("History: cleared %d flights (%s)", n, what)
            mark_dirty()
            bump()
            return self._send(200, {"removed": n})
        if path == "/api/backup/connect":
            try:
                return self._send(200, gdrive.start_device_flow(on_connected=lambda: run_backup(force=True)))
            except Exception as e:
                return self._send(400, {"error": str(e)[:300]})
        if path == "/api/backup/disconnect":
            gdrive.disconnect()
            return self._send(200, {"ok": True})
        if path == "/api/backup/now":
            ok = run_backup(force=True)
            st = gdrive.status()
            return self._send(200 if ok else 400, {"ok": ok, "error": None if ok else st.get("last_error") or "Not connected"})
        if path in ("/api/backup/restore", "/api/backup/upload"):
            try:
                blob = gdrive.download("Flight Tracker backup.zip") if path.endswith("restore") else self._body(200 * 1024 * 1024)
                if not blob:
                    return self._send(404, {"error": "No backup in the Drive folder yet."})
                res, extra = history.restore_backup(blob)
                with LOCK:
                    for k in ("manual",):
                        for x in extra.get(k) or []:
                            if x not in STATE["manual"]:
                                STATE["manual"].append(x)
                    if extra.get("rules") and not STATE.get("flags", {}).get("restored_rules"):
                        STATE["rules"] = rules_mod.merge(extra["rules"], CFG)
                        STATE["app_settings"] = clean_app_settings(extra.get("app_settings") or {})
                        CFG.update(STATE["app_settings"])
                        sync_rule_cfg()
                        STATE.setdefault("flags", {})["restored_rules"] = True
                save_state()
                mark_dirty()
                return self._send(200, res)
            except Exception as e:
                log.warning("Restore: %s", e)
                return self._send(400, {"error": str(e)[:300]})
        if path == "/api/history/delete":
            ok = history.delete((self._json() or {}).get("id") or "")
            mark_dirty()
            bump()
            return self._send(200 if ok else 404, {"ok": ok})
        if path == "/api/flights":
            body = self._json()
            try:
                lid = add_picked(body) if body.get("fa_id") else add_manual(body.get("ident"), body.get("dep"), body.get("arr"),
                                                                            body.get("date"), body.get("kind") or "manual")
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            save_state()
            bump()
            mark_dirty()
            REFRESH_Q.put(lid)
            WAKE.set()
            return self._send(200, {"ok": True, "id": lid})
        if path == "/api/flights/kind":
            body = self._json()
            ok = set_kind(body.get("id") or "", body.get("kind"))
            save_state()
            mark_dirty()
            bump()
            return self._send(200 if ok else 404, {"ok": ok})
        if path == "/api/flights/remove":
            remove_manual(self._json().get("id") or "")
            save_state()
            mark_dirty()
            bump()
            return self._send(200, {"ok": True})
        if path == "/api/settings":
            body = self._json().get("settings") or {}
            with LOCK:
                old_days = STATE["app_settings"].get("schedule_days")
                STATE["app_settings"] = clean_app_settings(body)
                CFG.update(STATE["app_settings"])
                if STATE["app_settings"].get("schedule_days") != old_days:
                    REFRESH_Q.put("schedule")
            save_state()
            mark_dirty()
            WAKE.set()
            bump()
            return self._send(200, {"ok": True})
        if path == "/api/rules":
            with LOCK:
                STATE["rules"] = rules_mod.clean(self._json().get("rules"), STATE["rules"])
                sync_rule_cfg()
            save_state()
            mark_dirty()
            with LOCK:
                return self._send(200, {"ok": True, "rules": rules_mod.catalog_for_ui(STATE["rules"])})
        if path == "/api/push/subscribe":
            b = self._json() or {}
            sub = b.get("sub") or {}
            if not VAPID or not (sub.get("endpoint") or "").startswith("https://") or not (sub.get("keys") or {}).get("p256dh"):
                return self._send(400, {"error": "not a push subscription"})
            subs = [x for x in push_subs() if x.get("endpoint") != sub["endpoint"]]
            subs.append({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]},
                         "label": (b.get("label") or "")[:60], "added": time.time()})
            push_save(subs)
            log.info("Phone subscribed to notifications (%s); %d in all", b.get("label") or "phone", len(subs))
            return self._send(200, {"ok": True, "count": len(subs)})
        if path == "/api/push/unsubscribe":
            ep = (self._json() or {}).get("endpoint")
            push_save([x for x in push_subs() if x.get("endpoint") != ep])
            return self._send(200, {"ok": True})
        if path == "/api/push/test":
            ep = (self._json() or {}).get("endpoint")
            sent, bad = push_send({"title": "Flighty", "message": f"Notifications on this phone work ({datetime.now(HOME_TZ):%H:%M}).",
                                   "level": "warn", "tag": "test", "key": "push"}, only=ep)
            return self._send(200 if sent else 502, {"ok": bool(sent), "sent": sent, "failed": bad})
        if path == "/api/test-notify":
            ok, detail = notify({"title": "Flight Tracker Flighty test", "level": "warn", "tag": "test", "key": "notify",
                                 "message": f"Notifications work. Sent {datetime.now(HOME_TZ):%H:%M} via "
                                            f"{CFG.get('notify_service') or 'persistent notification'}."})
            return self._send(200 if ok else 502, {"ok": ok, "detail": detail})
        m = re.match(r"^/api/aero/([\w-]+)$", path)
        if m:
            return self._send(*manual_aero(m.group(1)))
        m = re.match(r"^/api/refresh(?:/([\w-]+))?$", path)
        if m:
            REFRESH_Q.put(m.group(1) or "all")
            WAKE.set()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def _shutdown(signum, frame):
    """Home Assistant stops the add-on with SIGTERM (updates, restarts): save first."""
    try:
        save_state()
        # EDCTs and the messages about your flights are written at most every 2 min while running:
        # write what came in since, or an EDCT that arrived just before an update is lost
        for save in (swim.save_edcts, swim.save_yours):
            try:
                save(force=True)
            except Exception as e:
                log.info("SWIM save on stop: %s", e)
        log.info("State saved; stopping")
    finally:
        os._exit(0)


def main():
    import signal
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    load_state()
    history.init(DATA_DIR)
    gdrive.init(DATA_DIR, CFG)
    init_app_settings()
    push_init()
    livery.start(LIVERY_DIR)
    faa.start(DATA_DIR)
    init_rules()
    if not CFG["airline_icao"]:
        log.warning("airline_icao is empty: titles without an airline code (e.g. '2204 EWR-IAH') can't be tracked")
    find_ingress()
    threading.Thread(target=worker, daemon=True, name="tracker").start()
    threading.Thread(target=backup_loop, daemon=True, name="backup").start()
    threading.Thread(target=live_loop, daemon=True, name="live").start()
    threading.Thread(target=layout_prefetch_loop, daemon=True, name="layouts").start()
    threading.Thread(target=lambda: (time.sleep(20), places_data()), daemon=True, name="places").start()
    swim.ON_NEWS = lambda acid: WAKE.set()
    swim.ON_NAS = lambda ap: (NAS_NEWS.add(ap), WAKE.set())
    swim.load_edcts(os.path.join(DATA_DIR, "swim_edcts.json"))
    swim.load_yours(os.path.join(DATA_DIR, "swim_yours.json"))
    swim.start(CFG)
    log.info("Flight Tracker Flighty %s on port %d (watch window %sh, home time zone %s)", VERSION, PORT,
             CFG["watch_hours"], TZNAME)
    srv = DualServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    if srv.tls():
        log.info("HTTPS on port %d with /ssl/%s (plain http still answers, for Home Assistant's ingress)", PORT, CFG.get("certfile") or "fullchain.pem")
    srv.serve_forever()


class DualServer(ThreadingHTTPServer):
    """One port, both protocols: Home Assistant's ingress (the HA app) talks plain http to this port,
    and so does http://<box>:<port>/. With ssl on, a browser opening https://<name>:<port>/ starts
    with a TLS handshake (first byte 0x16): that connection is wrapped with the certificate from /ssl
    (e.g. the Duck DNS / Let's Encrypt add-on's fullchain.pem + privkey.pem). The certificate is
    re-read when its file changes (renewals), so no restart is needed."""
    _ctx = None
    _ctx_mtime = 0
    _ctx_err = ""

    def tls(self):
        if not CFG.get("ssl"):
            return None
        cert = os.path.join("/ssl", (CFG.get("certfile") or "fullchain.pem").lstrip("/"))
        key = os.path.join("/ssl", (CFG.get("keyfile") or "privkey.pem").lstrip("/"))
        try:
            m = max(os.path.getmtime(cert), os.path.getmtime(key))
        except OSError as e:
            if self._ctx_err != str(e):
                self._ctx_err = str(e)
                log.warning("ssl is on but the certificate can't be read (%s): serving http only", e)
            return self._ctx
        if self._ctx is None or m != self._ctx_mtime:
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(cert, key)
                if self._ctx is not None:
                    log.info("HTTPS certificate reloaded (%s)", cert)
                self._ctx, self._ctx_mtime, self._ctx_err = ctx, m, ""
            except Exception as e:
                if self._ctx_err != str(e):
                    self._ctx_err = str(e)
                    log.warning("Couldn't load the HTTPS certificate %s / %s: %s", cert, key, e)
        return self._ctx

    def finish_request(self, request, client_address):
        ctx = self.tls()
        if ctx is not None:
            try:
                request.settimeout(15)
                first = request.recv(1, socket.MSG_PEEK)
                if not first:
                    return
                if first == b"\x16":   # TLS ClientHello
                    request = ctx.wrap_socket(request, server_side=True)
                request.settimeout(None)
            except (OSError, ssl.SSLError) as e:
                log.debug("TLS handshake from %s failed: %s", client_address[0], e)
                return
        super().finish_request(request, client_address)

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        except Exception:
            pass


if __name__ == "__main__":
    main()
