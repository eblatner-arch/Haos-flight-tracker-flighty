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
         "worker": {"busy": None, "last_cycle": None, "error": None},
         "manual": [], "debug": []}
PERSIST = ("legs", "alerts", "nas", "nas_at", "wx", "manual")
INGRESS = {"path": None}

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
        mine = []
        for L in upcoming:
            if watched(L, now):
                mine += [n for n in alerting.nas_for(L, nas) if n not in mine]
        post_state("sensor.flight_tracker_nas_programs", len(mine), {
            "friendly_name": "NAS programs at my airports", "icon": "mdi:radar", "unit_of_measurement": "programs",
            "airports": sorted({c for L in upcoming if watched(L, now) for c in (L["dep"], L["arr"]) if c}),
            "programs": [f"{n['airport']} {n['label']}" for n in mine]})
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
        matched = {L["id"] for L in legs}
        unmatched = sorted({e["summary"] for e in events if not e.get("allday") and e["oid"] not in matched
                            and e["start"] > now - 2 * HOUR})
        STATE["schedule"].update(mtime=mtime, error=None, count=len(legs), checked=now, path=path,
                                 events=len(events), unmatched=unmatched[:25])
    log.info("Schedule: %d flights in the next 3 weeks", len(legs))


IDENT_RE = re.compile(r"^([A-Z]{2,3}|[A-Z]\d|\d[A-Z])(\d{1,4})[A-Z]?$")


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


def sync_extras():
    """Followed flights: track_flights from the config plus flights added in the app."""
    wanted = {}
    for raw in CFG.get("track_flights") or []:
        x = _x(raw)
        if x["ident"]:
            wanted[extra_id(**x)] = (x, False)
    now = time.time()
    with LOCK:
        for raw in STATE["manual"]:
            x = _x(raw)
            wanted[extra_id(**x)] = (x, True)
        for lid in [k for k, L in STATE["legs"].items() if L.get("extra") and k not in wanted]:
            del STATE["legs"][lid]
        for lid, (x, manual) in wanted.items():
            if lid in STATE["legs"]:
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
                "id": lid, "extra": True, "manual": manual, "date": x["date"],
                "ident": (airline + m.group(2)) if m else x["ident"],
                "display": display_ident(airline, m.group(2)) if m else x["ident"], "airline": airline,
                "flight_no": m.group(2) if m else "", "dep": x["dep"], "arr": x["arr"], "dh": False,
                "sched_out": start, "sched_in": start, "summary": "Added in the app" if manual else "track_flights",
                "notified": {"watch": True}, "alerts": [], "next_check": 0}


def add_manual(ident, dep, arr, date):
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
            STATE["manual"].append({"ident": ident, "dep": dep, "arr": arr, "date": date, "added": time.time()})
    sync_extras()
    return lid


def remove_manual(lid):
    with LOCK:
        STATE["manual"] = [x for x in STATE["manual"] if extra_id(**_x(x)) != lid]
        STATE["legs"].pop(lid, None)


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
    """Tracking stops once the flight is off the ground (or long cancelled / long past)."""
    f = L.get("flight")
    ph = fa.phase(f, now)
    if ph in ("airborne", "landed", "arrived", "diverted"):
        return True
    if ph == "cancelled":
        return now > L["sched_out"] + 2 * HOUR
    if L.get("extra"):
        return False
    return now > max(L["sched_in"], (fa.best_time(f["times"], "in") if f else 0) or 0) + 2 * HOUR


def watched(L, now):
    if L.get("extra"):
        return not finished(L, now)
    return now >= L["sched_out"] - float(CFG["watch_hours"]) * HOUR and not finished(L, now)


def poll_seconds(L, now):
    f = L.get("flight")
    ph = fa.phase(f, now)
    out_t = (fa.best_time(f["times"], "out") if f else None) or L["sched_out"]
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


def fetch_flight(browser, L):
    """Load the leg's FlightAware page(s). Returns (flight, source, shot_path, note)."""
    ident, lid = L.get("ident"), L["id"]
    if not ident:
        return None, None, None, "No flight number found in the calendar title"
    shot = None
    if L.get("fa_id"):
        boot, shot = load(browser, fa.id_url(L["fa_id"]), lid, L, "flight (pinned)")
        cur, _ = fa.flights_from_bootstrap(boot)
        if cur:
            return cur, "FlightAware page data", shot, None
        if boot is not None:
            L.pop("fa_id", None)  # stale instance; look it up again
    if not L.get("fa_id"):
        boot, shot = load(browser, fa.ident_url(ident), lid, L, "flight lookup")
        cur, activity = fa.flights_from_bootstrap(boot)
        if L.get("extra") and boot is not None:
            inst = fa.pick_followed(cur, activity, L.get("dep"), L.get("arr"), L.get("date"), time.time())
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
    if not inb or inb.get("fa_id") != f.get("inbound_fa_id"):
        return True
    if f.get("tail") and inb.get("checked_for_tail") and f["tail"] != inb["checked_for_tail"]:
        return True
    return fa.phase(inb, time.time()) not in ("arrived", "cancelled", "diverted")


CHAIN_HOPS = 3


def walk_chain(browser, L, now):
    """Follow the airplane's day backwards (inbound, its inbound, ...) to the first leg
    that's transmitting, and read the registration from its ADS-B. Returns tail info or None."""
    cur = L.get("inbound")
    hops = []
    found = None
    for hop in range(CHAIN_HOPS + 1):
        if not cur:
            break
        ph = fa.phase(cur, now)
        route = f"{(cur.get('origin') or {}).get('iata') or '?'}→{(cur.get('destination') or {}).get('iata') or '?'}"
        name = cur.get("display_ident") or cur.get("ident") or "?"
        hops.append({"ident": name, "route": route, "phase": ph,
                     "out": fa.best_time(cur["times"], "out"), "in": fa.best_time(cur["times"], "in")})
        if ph in ("taxi_out", "airborne", "landed") and cur.get("ident"):
            try:
                live = sources.fetch_live(cur["ident"])
            except Exception as e:
                log.info("%s chain ADS-B: %s", L.get("display"), e)
                live = None
            if live and live.get("tail"):
                found = {"tail": live["tail"].upper(), "kind": "chain", "at": now,
                         "root": (L.get("flight") or {}).get("inbound_fa_id"),
                         "source": f"{'inbound ' if hop == 0 else ''}{name} {route} in flight"}
            break
        if ph != "scheduled" or not cur.get("inbound_fa_id") or hop == CHAIN_HOPS:
            break  # on the ground (no signal to read), or the chain isn't linked further back
        try:
            boot, _ = load(browser, fa.id_url(cur["inbound_fa_id"]), f"{L['id']}-chain{hop + 1}", L,
                           f"aircraft's earlier leg #{hop + 1}")
            cur, _ = fa.flights_from_bootstrap(boot)
        except Exception as e:
            log.info("%s chain hop %d: %s", L.get("display"), hop + 1, e)
            break
    L["chain"] = hops
    return found


def resolve_tail(browser, L, f, now):
    """Which airplane is flying this leg. Trust order: our own transponder, then an earlier
    leg of the same airplane seen in flight, then whatever FlightAware's page says."""
    info = dict(L.get("tail_info") or {})
    if info.get("kind") == "chain" and info.get("root") != f.get("inbound_fa_id"):
        info = {}  # a different inbound is a different airplane
    if info.get("kind") == "page":
        info = {}  # re-read every time
    ph = fa.phase(f, now)
    out_t = fa.best_time(f["times"], "out") or L["sched_out"]
    if L.get("ident") and (ph == "taxi_out" or (ph == "scheduled" and out_t - now < 90 * 60)):
        try:
            live = sources.fetch_live(L["ident"])
            if live and live.get("tail"):
                info = {"tail": live["tail"].upper(), "kind": "own", "at": now,
                        "source": "your flight's transponder"}
        except Exception as e:
            log.info("%s own ADS-B: %s", L.get("display"), e)
    if info.get("kind") not in ("own", "chain") and ph == "scheduled":
        found = walk_chain(browser, L, now)
        if found:
            info = found
    if not info.get("tail") and f.get("tail"):
        info = {"tail": f["tail"], "kind": "page", "at": now, "source": "FlightAware"}
    if info.get("tail"):
        f["tail"] = info["tail"]
        f["tail_source"] = info["source"]
    L["tail_info"] = info


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
        ti = L.get("tail_info") or {}
        if f and not f.get("tail") and ti.get("tail") and ti.get("kind") in ("own", "chain") \
                and (ti.get("kind") == "own" or ti.get("root") == f.get("inbound_fa_id")):
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

        # Which airplane
        if f and L.get("flight") and not L.get("extra") and ph in ("scheduled", "taxi_out"):
            resolve_tail(browser, L, f, now)

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
                expire_manual(now)
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
                                  "next_check", "shot", "fa_id", "date", "manual", "tail_info", "chain")}
    out["status"] = alerting.status_label(L, CFG)
    out["phase"] = fa.phase(f, now)
    out["watching"] = watched(L, now)
    out["finished"] = finished(L, now)
    if out["finished"] and out["phase"] == "scheduled":
        out["status"] = "Completed"
    elif out["finished"] and out["phase"] in ("airborne", "landed"):
        out["status"] = "Departed"
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
                       "ingress": INGRESS["path"] or "(unknown)", "gemini": bool(CFG.get("gemini_api_key")),
                       "gemini_model": CFG.get("gemini_model"), "watch_hours": CFG["watch_hours"],
                       "home_tz": TZNAME, "viewer_port": 6080},
            "manual": STATE["manual"],
        }


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
        if path == "/api/debug":
            return self._send(200, api_debug())
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

    def _json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/flights":
            body = self._json()
            try:
                lid = add_manual(body.get("ident"), body.get("dep"), body.get("arr"), body.get("date"))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            REFRESH_Q.put(lid)
            WAKE.set()
            return self._send(200, {"ok": True, "id": lid})
        if path == "/api/flights/remove":
            remove_manual(self._json().get("id") or "")
            return self._send(200, {"ok": True})
        if path == "/api/test-notify":
            ok, detail = notify({"title": "Flight Tracker test", "level": "warn", "tag": "test", "key": "notify",
                                 "message": f"Notifications work. Sent {datetime.now(HOME_TZ):%H:%M} via "
                                            f"{CFG.get('notify_service') or 'persistent notification'}."})
            return self._send(200 if ok else 502, {"ok": ok, "detail": detail})
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
