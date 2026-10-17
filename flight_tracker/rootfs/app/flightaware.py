"""Read FlightAware flight pages.

A FlightAware flight page carries the data it draws from in a page variable
(`trackpollBootstrap`): gates, times, aircraft, inbound flight, filed route
waypoints and the flown track. The browser reads that variable from the
loaded page; this module turns it into a plain dict. The screenshot of the
same page is the fallback when the variable is missing or the layout changes.

Everything here is pure and tolerant: unknown or missing keys give None.
"""

import re

BASE = "https://www.flightaware.com/live/flight/"

TIME_KEYS = {
    "out": "gateDepartureTimes",
    "off": "takeoffTimes",
    "on": "landingTimes",
    "in": "gateArrivalTimes",
}


def ident_url(ident):
    return BASE + ident.upper()


def id_url(fa_id):
    return BASE + "id/" + fa_id


def _get(d, *path):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and -len(d) <= p < len(d):
            d = d[p]
        else:
            return None
    return d


def _first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _airport(a):
    a = a or {}
    coord = a.get("coord") or []
    tz = (a.get("TZ") or a.get("tz") or "").lstrip(":")
    return {
        "iata": _first(a.get("iata"), a.get("altIdent")),
        "icao": _first(a.get("icao"), a.get("identifier")),
        "name": a.get("friendlyName") or a.get("name"),
        "city": a.get("friendlyLocation") or a.get("city"),
        "lat": _num(coord[1]) if len(coord) > 1 else None,
        "lon": _num(coord[0]) if len(coord) > 1 else None,
        "tz": tz or None,
        "gate": _first(a.get("gate")),
        "terminal": _first(a.get("terminal")),
        "baggage": _first(a.get("baggage"), a.get("bagClaim"), a.get("baggageClaim")),
        "delays": a.get("delays") or None,
    }


def _times(f, key):
    t = f.get(TIME_KEYS[key]) or {}
    return {k: _num(t.get(k)) for k in ("scheduled", "estimated", "actual")}


def _alt_ft(v):
    v = _num(v)
    if v is None:
        return None
    # The track gives altitude in hundreds of feet (FL style).
    return int(v * 100) if v < 1000 else int(v)


def same_flight(a, b, slack=6 * 3600):
    """Do two FlightAware flight IDs mean the same flight? IDs look like
    UAL147-1760398500-schedule-0001 and change their tail end (schedule -> airline) as
    FlightAware's data firms up, so compare the flight number and scheduled time instead."""
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = str(a).split("-"), str(b).split("-")
    if len(pa) < 2 or len(pb) < 2 or pa[0].upper() != pb[0].upper():
        return False
    try:
        return abs(int(pa[1]) - int(pb[1])) <= slack
    except ValueError:
        return False


def thin(track, n):
    """Keep at most n points spread over the whole flight (first and last always kept),
    instead of dropping the start."""
    if not track or len(track) <= n:
        return list(track or [])
    step = (len(track) - 1) / (n - 1)
    return [track[round(i * step)] for i in range(n)]


def normalize(f):
    """One FlightAware flight object -> our flight dict."""
    if not isinstance(f, dict):
        return None
    aircraft = f.get("aircraft") or {}
    plan = f.get("flightPlan") or {}
    inbound = f.get("inboundFlight") or {}
    track = []
    for p in f.get("track") or []:
        c = p.get("coord") or []
        if len(c) < 2:
            continue
        track.append([_num(c[1]), _num(c[0]), _alt_ft(p.get("alt")), _num(p.get("gs")), _num(p.get("timestamp"))])
    waypoints = []
    for w in f.get("waypoints") or []:
        if isinstance(w, (list, tuple)) and len(w) >= 2:
            waypoints.append([_num(w[1]), _num(w[0])])
    out = {
        "fa_id": _first(f.get("flightId"), f.get("fa_flight_id"), f.get("id")),
        "ident": _first(f.get("ident"), f.get("displayIdent")),
        "display_ident": _first(f.get("displayIdent"), f.get("iataIdent"), f.get("ident")),
        "status_text": _first(f.get("flightStatus"), f.get("status")),
        "cancelled": bool(f.get("cancelled")),
        "diverted": bool(f.get("diverted")),
        "origin": _airport(f.get("origin")),
        "destination": _airport(f.get("destination")),
        "times": {k: _times(f, k) for k in TIME_KEYS},
        "aircraft_type": _first(aircraft.get("type"), f.get("aircraftType")),
        "aircraft_name": _first(aircraft.get("friendlyType"), aircraft.get("typeDescription")),
        "tail": _first(aircraft.get("tail"), aircraft.get("registration"), f.get("tail"), f.get("registration")),
        "route": _first(plan.get("route")),
        "filed_altitude": _num(plan.get("altitude")),
        "filed_speed": _num(plan.get("speed")),
        "distance_nm": _num(_first(plan.get("directDistance"), plan.get("plannedDistance"))),
        "ete": _num(plan.get("ete")),
        "waypoints": waypoints[:2000],
        "track": thin(track, 800),
        "inbound_fa_id": _first(inbound.get("fa_flight_id"), inbound.get("flightId"), inbound.get("id")),
        "inbound_ident": _first(inbound.get("ident"), inbound.get("displayIdent")),
    }
    return out


def flights_from_bootstrap(b):
    """Return (current_flight_dict_or_None, [activity entries]) from trackpollBootstrap."""
    if not isinstance(b, dict):
        return None, []
    flights = b.get("flights") or {}
    raw = next(iter(flights.values()), None) if isinstance(flights, dict) else None
    cur = normalize(raw) if raw else None
    activity = []
    for a in _get(raw or {}, "activityLog", "flights") or []:
        n = normalize(a)
        if n:
            activity.append(n)
    if cur and not cur["fa_id"]:
        key = next(iter(flights.keys()), None)
        if key and re.match(r"^[A-Z0-9]+-\d+-", key):
            cur["fa_id"] = key
    return cur, activity


def pick_instance(cur, activity, dep, sched_out, window=6 * 3600):
    """The flight instance (fa_id) that matches our leg: same origin, departure close to schedule."""
    best, best_gap = None, None
    for f in ([cur] if cur else []) + list(activity):
        if not f or not f.get("fa_id"):
            continue
        o = f["origin"]
        if dep and dep.upper() not in {(o.get("iata") or "").upper(), (o.get("icao") or "").upper()[-3:]}:
            continue
        t = f["times"]["out"]["scheduled"] or f["times"]["off"]["scheduled"]
        if t is None or sched_out is None:
            continue
        gap = abs(t - sched_out)
        if gap <= window and (best_gap is None or gap < best_gap):
            best, best_gap = f, gap
    return best


def best_time(times, key):
    t = times.get(key) or {}
    return t.get("actual") or t.get("estimated") or t.get("scheduled")


def fix_diversion(f, planned_arr=None):
    """A diverted flight shows up two ways: a flight 'cancelled' after it took off (AeroAPI marks the
    original EWR flight that way), or a flight headed somewhere other than where you planned (LAX→MDT
    instead of LAX→EWR). Both mean diverted: keep following it, don't call it cancelled."""
    if not f:
        return f
    t = f.get("times") or {}
    flew = (t.get("off") or {}).get("actual") or (t.get("on") or {}).get("actual")
    if f.get("cancelled") and flew:
        f["cancelled"] = False
    dest = ((f.get("destination") or {}).get("iata") or "").upper()
    planned = (planned_arr or "").upper()
    if planned and dest and dest != planned:
        f["diverted_to"], f["planned_arr"] = dest, planned
    return f


def phase(f, now):
    """scheduled | taxi_out | airborne | landed | arrived | cancelled | diverted"""
    if not f:
        return "scheduled"
    if f.get("cancelled"):
        return "cancelled"
    t = f["times"]
    if f.get("diverted"):
        return "diverted"
    if t["in"]["actual"]:
        return "arrived"
    if t["on"]["actual"]:
        return "landed"
    if t["off"]["actual"]:
        return "airborne"
    if t["out"]["actual"]:
        return "taxi_out"
    st = (f.get("status_text") or "").lower().strip()
    if st.startswith(("arrived", "landed")):
        return "arrived"
    if "en route" in st or "airborne" in st:
        return "airborne"
    return "scheduled"


def delay_minutes(f, sched_out=None):
    """Departure delay: (actual or estimated gate out) - scheduled gate out."""
    if not f:
        return None
    t = f["times"]["out"]
    s = t["scheduled"] or sched_out
    e = t["actual"] or t["estimated"]
    if s is None or e is None:
        return None
    return int(round((e - s) / 60))


def arrival_delay_minutes(f, sched_in=None):
    if not f:
        return None
    t = f["times"]["in"]
    s = t["scheduled"] or sched_in
    e = t["actual"] or t["estimated"]
    if s is None or e is None:
        return None
    return int(round((e - s) / 60))


def pick_by_date(cur, activity, date_str):
    """The instance departing on date_str (YYYY-MM-DD, origin local time), or None."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    for f in ([cur] if cur else []) + list(activity):
        if not f or not f.get("fa_id"):
            continue
        t = f["times"]["out"]["scheduled"] or f["times"]["off"]["scheduled"]
        if t is None:
            continue
        try:
            tz = ZoneInfo(f["origin"].get("tz") or "UTC")
        except Exception:
            tz = timezone.utc
        if datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d") == date_str:
            return f
    return None


def _code_match(ap, code):
    if not code:
        return True
    code = code.upper()
    return code in {(ap.get("iata") or "").upper(), (ap.get("icao") or "").upper()}


def pick_followed(cur, activity, dep, arr, date_str, now):
    """For a flight added by hand: the instance with this origin/destination, on date_str
    if given (origin local date), otherwise the next one that hasn't left yet."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    best = None
    for f in ([cur] if cur else []) + list(activity):
        if not f or not f.get("fa_id"):
            continue
        if not _code_match(f.get("origin") or {}, dep) or not _code_match(f.get("destination") or {}, arr):
            continue
        t = f["times"]["out"]["scheduled"] or f["times"]["off"]["scheduled"]
        if t is None:
            continue
        if date_str:
            try:
                tz = ZoneInfo(f["origin"].get("tz") or "UTC")
            except Exception:
                tz = timezone.utc
            if datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d") != date_str:
                continue
        elif f["times"]["off"]["actual"] or t < now - 3 * 3600:
            continue  # already gone
        if best is None or t < (best["times"]["out"]["scheduled"] or best["times"]["off"]["scheduled"]):
            best = f
    return best
