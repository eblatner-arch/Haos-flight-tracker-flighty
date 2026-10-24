"""FlightAware AeroAPI (paid, per query) for the airline's tail assignment and inbound flight.

Only a few calls per leg: one when tracking starts, a few retries if no tail is assigned
yet, and one whenever the free page check sees a different inbound airplane.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("flight_tracker_flighty")

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
        "atc_ident": f.get("atc_ident") or None,
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


def query(key, ident, start, end):
    """All instances of ident scheduled between start and end (epoch). Returns (flights, pages billed)."""
    now = datetime.now(timezone.utc).timestamp()
    start = max(start, now - 9 * 86400)
    end = min(end, now + 2 * 86400 - 60)
    if end <= start:
        return [], 0
    r = requests.get(f"{BASE}/flights/{ident}", headers={"x-apikey": key, "Accept": "application/json"},
                     params={"start": _iso(start), "end": _iso(end), "max_pages": 1}, timeout=20)
    if r.status_code in (401, 403):
        raise RuntimeError("AeroAPI rejected the key (check aeroapi_key)")
    if r.status_code == 429:
        raise RuntimeError("AeroAPI rate limit hit; will retry later")
    r.raise_for_status()
    data = r.json() or {}
    return [normalize(raw) for raw in data.get("flights") or []], max(1, int(data.get("num_pages") or 1))


def schedules(key, airline, number, origin, destination, day_start, day_end):
    """The airline's published timetable for one flight number on a date (AeroAPI /schedules, which
    reaches months ahead, unlike /flights' two days). day_start/day_end are 'YYYY-MM-DD'.
    Returns ([{ident, origin, destination, scheduled_out, scheduled_in, aircraft_type}], pages billed)."""
    params = {"airline": airline, "flight_number": int(number), "max_pages": 1,
              "include_codeshares": "false", "include_regional": "true"}
    if origin:
        params["origin"] = origin
    if destination:
        params["destination"] = destination
    r = requests.get(f"{BASE}/schedules/{day_start}/{day_end}", headers={"x-apikey": key, "Accept": "application/json"},
                     params=params, timeout=20)
    if r.status_code in (401, 403):
        raise RuntimeError("AeroAPI rejected the key (check aeroapi_key)")
    if r.status_code == 429:
        raise RuntimeError("AeroAPI rate limit hit; will retry later")
    if r.status_code == 400:
        raise RuntimeError("AeroAPI schedules: " + (r.text or "bad request")[:160])
    r.raise_for_status()
    data = r.json() or {}
    out = []
    for x in data.get("scheduled") or []:
        out.append({"ident": x.get("ident_icao") or x.get("ident"),
                    "origin": [x.get(k) for k in ("origin", "origin_icao", "origin_iata", "origin_lid") if x.get(k)],
                    "destination": [x.get(k) for k in ("destination", "destination_icao", "destination_iata", "destination_lid") if x.get(k)],
                    "scheduled_out": _ts(x.get("scheduled_out")), "scheduled_in": _ts(x.get("scheduled_in")),
                    "aircraft_type": x.get("aircraft_type")})
    return out, max(1, int(data.get("num_pages") or 1))
