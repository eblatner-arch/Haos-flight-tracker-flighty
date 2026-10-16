"""Airport lookup (name, city, ICAO, coordinates, time zone) from the airportsdata package."""

import logging

log = logging.getLogger("flight_tracker")

_IATA = {}
_ICAO = {}

try:
    import airportsdata
    _IATA = airportsdata.load("IATA")
    _ICAO = airportsdata.load("ICAO")
except Exception as e:  # keep working without it; FlightAware supplies most of this
    log.warning("airportsdata not available (%s); airport details will come from FlightAware only", e)
    _IATA = {
        "EWR": {"icao": "KEWR", "iata": "EWR", "name": "Newark Liberty International Airport",
                "city": "Newark", "lat": 40.6925, "lon": -74.1687, "tz": "America/New_York"},
        "SFO": {"icao": "KSFO", "iata": "SFO", "name": "San Francisco International Airport",
                "city": "San Francisco", "lat": 37.619, "lon": -122.375, "tz": "America/Los_Angeles"},
        "IAH": {"icao": "KIAH", "iata": "IAH", "name": "George Bush Intercontinental Airport",
                "city": "Houston", "lat": 29.9844, "lon": -95.3414, "tz": "America/Chicago"},
        "ORD": {"icao": "KORD", "iata": "ORD", "name": "Chicago O'Hare International Airport",
                "city": "Chicago", "lat": 41.9786, "lon": -87.9048, "tz": "America/Chicago"},
        "DEN": {"icao": "KDEN", "iata": "DEN", "name": "Denver International Airport",
                "city": "Denver", "lat": 39.8617, "lon": -104.673, "tz": "America/Denver"},
    }
    _ICAO = {v["icao"]: v for v in _IATA.values()}


def get(code):
    """Return {iata, icao, name, city, lat, lon, tz} for an IATA or ICAO code, or {}."""
    code = (code or "").strip().upper()
    a = _IATA.get(code) if len(code) == 3 else _ICAO.get(code)
    if not a and len(code) == 3:
        # renamed or re-coded airports (PBI is now DJT in some apps; airportsdata files it as KDJT)
        a = _ICAO.get("K" + code)
    if not a:
        return {}
    return {"iata": a.get("iata") or "", "icao": a.get("icao") or "", "name": a.get("name") or "",
            "city": a.get("city") or "", "lat": a.get("lat"), "lon": a.get("lon"), "tz": a.get("tz") or ""}


def icao(code):
    return get(code).get("icao") or ""


def iata(code):
    return get(code).get("iata") or (code or "").upper()
