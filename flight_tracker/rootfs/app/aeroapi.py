"""FlightAware AeroAPI (paid, per query) for the airline's tail assignment and inbound flight.

Only a few calls per leg: one when tracking starts, a few retries if no tail is assigned
yet, and one whenever the free page check sees a different inbound airplane.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("flight_tracker")

BASE = "https://aeroapi.flightaware.com/aeroapi"


def _ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ap(a, gate, term, bag=None):
    a = a or {}
    return {"iata": a.get("code_iata") or None, "icao": a.get("code_icao") or a.get("code") or None,
            "name": a.get("name"), "city": a.get("city"), "lat": None, "lon": None,
            "tz": a.get("timezone"), "gate": gate or None, "terminal": term or None, "baggage": bag or None,
            "delays": None}


def normalize(f):
    """AeroAPI flight -> the same shape flightaware.normalize returns."""
    def t(kind):
        return {"scheduled": _ts(f.get(f"scheduled_{kind}")), "estimated": _ts(f.get(f"estimated_{kind}")),
                "actual": _ts(f.get(f"actual_{kind}"))}
    return {
        "fa_id": f.get("fa_flight_id"),
        "ident": f.get("ident_icao") or f.get("ident"),
        "display_ident": f.get("ident_iata") or f.get("ident"),
        "status_text": f.get("status"),
        "cancelled": bool(f.get("cancelled")),
        "diverted": bool(f.get("diverted")),
        "origin": _ap(f.get("origin"), f.get("gate_origin"), f.get("terminal_origin")),
        "destination": _ap(f.get("destination"), f.get("gate_destination"), f.get("terminal_destination"),
                           f.get("baggage_claim")),
        "times": {"out": t("out"), "off": t("off"), "on": t("on"), "in": t("in")},
        "aircraft_type": f.get("aircraft_type"), "aircraft_name": None,
        "tail": f.get("registration") or None,
        "route": f.get("route"), "filed_altitude": f.get("filed_altitude"), "filed_speed": f.get("filed_airspeed"),
        "distance_nm": f.get("route_distance"), "ete": f.get("filed_ete"),
        "waypoints": [], "track": [],
        "inbound_fa_id": f.get("inbound_fa_flight_id") or None, "inbound_ident": None,
    }


def lookup(key, ident, dep, sched_out):
    """The flight instance departing dep near sched_out. Returns (flight or None, number of result pages billed)."""
    now = datetime.now(timezone.utc).timestamp()
    start = max(sched_out - 6 * 3600, now - 9 * 86400)
    end = min(sched_out + 6 * 3600, now + 2 * 86400 - 60)
    r = requests.get(f"{BASE}/flights/{ident}", headers={"x-apikey": key, "Accept": "application/json"},
                     params={"start": _iso(start), "end": _iso(end), "max_pages": 1}, timeout=20)
    if r.status_code in (401, 403):
        raise RuntimeError("AeroAPI rejected the key (check aeroapi_key)")
    if r.status_code == 429:
        raise RuntimeError("AeroAPI rate limit hit; will retry later")
    r.raise_for_status()
    data = r.json() or {}
    best, gap = None, None
    for raw in data.get("flights") or []:
        f = normalize(raw)
        o = f["origin"]
        if dep and dep.upper() not in {(o.get("iata") or "").upper(), (o.get("icao") or "").upper()}:
            continue
        t = f["times"]["out"]["scheduled"] or f["times"]["off"]["scheduled"]
        if t is None:
            continue
        g = abs(t - sched_out)
        if g <= 4 * 3600 and (gap is None or g < gap):
            best, gap = f, g
    return best, max(1, int(data.get("num_pages") or 1))
