"""Free public feeds: FAA NAS status, aviationweather.gov METARs, adsb.lol live positions."""

import logging
import re
import time
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("flight_tracker")

UA = {"User-Agent": "haos-flight-tracker (+https://github.com/eblatner-arch/haos-flight-tracker)"}
NAS_URL = "https://nasstatus.faa.gov/api/airport-status-information"
EVENTS_URL = "https://nasstatus.faa.gov/api/airport-events"
METAR_URL = "https://aviationweather.gov/api/data/metar"
ADSB_URL = "https://api.adsb.lol/v2/callsign/{}"

NAS_KINDS = {
    "ground delay": ("gdp", "Ground delay program"),
    "ground stop": ("gs", "Ground stop"),
    "closure": ("closure", "Airport closure"),
    "deicing": ("deice", "Deicing"),
    "de-icing": ("deice", "Deicing"),
    "arrival/departure": ("delay", "Delays"),
}


def _txt(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_nas(xml_text):
    """-> list of {airport, kind, label, reason, detail, key}"""
    root = ET.fromstring(xml_text)
    out = []
    for dt in root.iter("Delay_type"):
        name = _txt(dt, "Name")
        kind, label = "other", name or "NAS"
        for k, v in NAS_KINDS.items():
            if k in name.lower():
                kind, label = v
                break
        for el in dt.iter():
            arpt = _txt(el, "ARPT")
            if not arpt or el.tag == "ARPT":
                continue
            reason = _txt(el, "Reason")
            detail = ""
            if kind == "gdp":
                avg, mx = _txt(el, "Avg"), _txt(el, "Max")
                detail = ", ".join(x for x in (f"avg {avg}" if avg else "", f"max {mx}" if mx else "") if x)
            elif kind == "gs":
                end = _txt(el, "End_Time")
                detail = f"until {end}" if end else ""
            elif kind == "closure":
                reopen = _txt(el, "Reopen")
                detail = f"reopens {reopen.rstrip('.')}" if reopen else ""
            elif kind == "delay":
                parts = []
                for ad in el.findall("Arrival_Departure"):
                    t = ad.get("Type") or ""
                    mn, mx, tr = _txt(ad, "Min"), _txt(ad, "Max"), _txt(ad, "Trend")
                    parts.append(f"{t.lower()} {mn}–{mx}".strip() + (f", {tr.lower()}" if tr else ""))
                    if t:
                        label = f"{t.title()} delays"
                detail = "; ".join(parts)
            else:
                detail = " ".join(_txt(el, t) for t in ("Start", "End_Time", "Avg", "Max") if _txt(el, t))
            out.append({"airport": arpt.upper(), "kind": kind, "label": label, "reason": reason,
                        "detail": detail, "key": f"{arpt.upper()}|{kind}|{label}|{reason}"})
    return out


def _epoch(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _list(v):
    if isinstance(v, list):
        return [str(x).strip().upper() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.upper() for x in v.replace(",", " ").split() if x.strip()]
    return []


def parse_events(data):
    """FAA airport-events JSON (what nasstatus.faa.gov itself shows) -> NAS items with scope."""
    out = []
    for a in data or []:
        arpt = (a.get("airportId") or "").upper()
        if not arpt:
            continue
        gs = a.get("groundStop")
        if gs:
            prob = (gs.get("probabilityOfExtension") or "").title()
            out.append({"airport": arpt, "kind": "gs", "label": "Ground stop",
                        "reason": gs.get("impactingCondition") or "", "detail": "",
                        "start": _epoch(gs.get("startTime")), "end": _epoch(gs.get("endTime")),
                        "prob": prob, "center": gs.get("center") or "", "scope": _list(gs.get("includedFacilities")),
                        "flights": (gs.get("includedFlights") or "").strip(" :"), "advisory": gs.get("advisoryUrl") or "",
                        "key": f"{arpt}|gs|{gs.get('id') or gs.get('startTime')}"})
        gd = a.get("groundDelay")
        if gd:
            avg, mx = gd.get("avgDelay"), gd.get("maxDelay")
            det = ", ".join(x for x in (f"avg {round(float(avg))} min" if avg not in (None, "") else "",
                                        f"max {mx} min" if mx not in (None, "") else "") if x)
            out.append({"airport": arpt, "kind": "gdp", "label": "Ground delay program",
                        "reason": gd.get("impactingCondition") or "", "detail": det,
                        "start": _epoch(gd.get("startTime")), "end": _epoch(gd.get("endTime")),
                        "avg": avg, "max": mx, "center": gd.get("center") or "",
                        "scope": _list(gd.get("includedFacilities")),
                        "flights": (gd.get("includedFlights") or "").strip(" :"), "advisory": gd.get("advisoryUrl") or "",
                        "key": f"{arpt}|gdp|{gd.get('id') or gd.get('startTime')}"})
        for fld in ("arrivalDelay", "departureDelay"):
            d = a.get(fld)
            if not d:
                continue
            ad = d.get("arrivalDeparture") or {}
            typ = ad.get("type") or ("Arrival" if fld == "arrivalDelay" else "Departure")
            avg = d.get("averageDelay")
            det = f"avg {avg} min" if avg not in (None, "") else \
                f"{ad.get('min') or '?'}–{ad.get('max') or '?'}".replace(" minutes", "") + " min"
            if ad.get("trend") or d.get("trend"):
                det += f" and {(ad.get('trend') or d.get('trend')).lower()}"
            reason = d.get("reason") or ""
            out.append({"airport": arpt, "kind": "delay", "label": f"{typ.title()} delay trend",
                        "dir": "dep" if typ.lower().startswith("dep") else "arr",
                        "reason": reason.split(":", 1)[-1] if ":" in reason else reason, "detail": det,
                        "key": f"{arpt}|delay|{typ}"})
        cl = a.get("airportClosure")
        if cl:
            out.append({"airport": arpt, "kind": "closure", "label": "Airport closure",
                        "reason": cl.get("text") or "", "detail": "",
                        "start": _epoch(cl.get("startTime")), "end": _epoch(cl.get("endTime")),
                        "key": f"{arpt}|closure|{cl.get('id') or cl.get('startTime')}"})
        di = a.get("deicing")
        if di:
            out.append({"airport": arpt, "kind": "deice", "label": "Deicing", "reason": "", "detail": "",
                        "key": f"{arpt}|deice"})
    return out


def fetch_nas():
    """FAA's current airport events (with ground-stop scope); the older XML feed if that fails."""
    try:
        r = requests.get(EVENTS_URL, headers=UA, timeout=20)
        r.raise_for_status()
        return parse_events(r.json())
    except Exception as e:
        log.warning("NAS events feed: %s; using the status XML", e)
    r = requests.get(NAS_URL, headers=UA, timeout=20)
    r.raise_for_status()
    return parse_nas(r.text)


def fetch_metars(icaos):
    icaos = sorted({i for i in icaos if i})
    if not icaos:
        return {}
    r = requests.get(METAR_URL, params={"ids": ",".join(icaos), "format": "json"}, headers=UA, timeout=20)
    r.raise_for_status()
    out = {}
    for m in r.json() if r.text.strip() else []:
        icao = m.get("icaoId")
        if not icao or icao in out:
            continue
        clouds = m.get("clouds") or []
        ceiling = None
        for c in clouds:
            if c.get("cover") in ("BKN", "OVC", "OVX") and c.get("base") is not None:
                ceiling = c["base"] if ceiling is None else min(ceiling, c["base"])
        out[icao] = {
            "raw": m.get("rawOb"), "category": m.get("fltCat"), "time": m.get("obsTime"),
            "wind_dir": m.get("wdir"), "wind_kt": m.get("wspd"), "gust_kt": m.get("wgst"),
            "visibility": m.get("visib"), "temp_c": m.get("temp"), "ceiling_ft": ceiling,
        }
    return out


def fetch_live_all(callsign):
    """Every airplane transmitting this callsign right now (an earlier leg with the same flight
    number can still be on it after landing)."""
    # adsb.lol is sometimes slow to answer: one more try with a longer wait before giving up
    for i, wait in enumerate((12, 25)):
        try:
            r = requests.get(ADSB_URL.format(callsign.upper()), headers=UA, timeout=(6, wait))
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if i:
                raise
            time.sleep(2)
    r.raise_for_status()
    out = []
    for ac in (r.json() or {}).get("ac") or []:
        if ac.get("lat") is None or ac.get("lon") is None:
            continue
        alt = ac.get("alt_baro")
        out.append({
            "lat": ac["lat"], "lon": ac["lon"],
            "alt_ft": 0 if alt == "ground" else alt, "ground": alt == "ground",
            "gs_kt": ac.get("gs"), "heading": ac.get("track") or ac.get("true_heading"),
            "tail": ac.get("r"), "type": ac.get("t"), "squawk": ac.get("squawk"), "hex": ac.get("hex"),
            "at": time.time() - float(ac.get("seen_pos") or 0),
        })
    return out


def fetch_live(callsign, accept=None):
    """Live ADS-B position by callsign, or None. accept(ac) -> bool picks among several."""
    for ac in fetch_live_all(callsign):
        if accept is None or accept(ac):
            return ac
    return None


ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"
ROUTE_HEADERS = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
                               "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
                 "Origin": "https://api.adsb.lol", "Referer": "https://api.adsb.lol/docs", "Accept": "*/*"}


def fetch_route(callsign, lat=0, lon=0):
    """adsb.lol's route for a callsign: ['DFW', 'EWR', 'MCO'] for a flight number that flies two legs."""
    # adsb.lol answers this one only for requests that look like its own docs page (a bare client
    # gets "201" with an empty body), so send what that page sends
    r = requests.post(ROUTESET_URL, json={"planes": [{"callsign": callsign.upper(), "lat": lat or 0, "lng": lon or 0}]},
                      headers=ROUTE_HEADERS, timeout=15)
    r.raise_for_status()
    for x in r.json() or []:
        codes = x.get("_airport_codes_iata") or ""
        if codes and codes.lower() != "unknown":
            return [c for c in codes.split("-") if c]
    return None


def nm_between(a_lat, a_lon, b_lat, b_lon):
    import math
    R = math.pi / 180
    d = 2 * math.asin(math.sqrt(math.sin((b_lat - a_lat) * R / 2) ** 2 +
                                math.cos(a_lat * R) * math.cos(b_lat * R) * math.sin((b_lon - a_lon) * R / 2) ** 2))
    return d * 3440.065


# ---------------------------------------------------------------- Flightradar24 schedule pages

FR24_URL = "https://www.flightradar24.com/data/flights/{}"
_REG = re.compile(r"\b(N[0-9][0-9A-Z]{1,4}|[A-Z]{1,2}-[A-Z0-9]{3,5}|C-[A-Z]{4})\b")
_DATE = re.compile(r"\b(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})\b")
_MON = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def parse_fr24_rows(rows):
    """[{date, dep, arr, type, reg, ts}] from the rows of a FR24 flight-history page."""
    out = []
    for r in rows or []:
        text = r.get("text") or " ".join(r.get("cells") or [])
        codes = re.findall(r"\(([A-Z0-9]{3})\)", text)
        if len(codes) < 2:
            continue
        m = _DATE.search(text)
        date = f"{m.group(3)}-{_MON.get(m.group(2), 0):02d}-{int(m.group(1)):02d}" if m else ""
        regs = [x for x in (r.get("regs") or []) if _REG.fullmatch(x.strip().upper() or "-")]
        reg = regs[0].upper() if regs else None
        typ = None
        cells = r.get("cells") or []
        for c in cells:  # the aircraft cell: "B39M (N77579)" or "7M9"
            mt = re.match(r"^([A-Z0-9]{3,4})\b(?:\s*\(([^)]+)\))?", c.strip())
            if mt and not re.fullmatch(r"\d{1,2}:\d{2}", c.strip()) and c.strip() not in codes:
                if mt.group(2) and _REG.fullmatch(mt.group(2).strip().upper()):
                    typ, reg = mt.group(1), reg or mt.group(2).strip().upper()
                    break
                if re.search(r"[A-Z]", mt.group(1)) and len(c.strip()) <= 12 and not _DATE.search(c):
                    typ = typ or mt.group(1)
        ts = []
        for x in (r.get("ts") or []) + [r.get("rowts")]:
            try:
                v = int(float(x))
                if v > 1e9:
                    ts.append(v)
            except (TypeError, ValueError):
                pass
        out.append({"date": date, "dep": codes[0], "arr": codes[1], "type": typ, "reg": reg, "ts": min(ts) if ts else None})
    return out


def pick_fr24(items, dep, arr, sched_out, dep_tz=None):
    """The row for this departure: same route, and closest scheduled time (or the same date)."""
    from datetime import datetime, timezone
    cands = [x for x in items if x["dep"] == (dep or "").upper() and x["arr"] == (arr or "").upper()]
    timed = [x for x in cands if x["ts"] and abs(x["ts"] - sched_out) < 4 * 3600]
    if timed:
        return min(timed, key=lambda x: abs(x["ts"] - sched_out))
    dates = {datetime.fromtimestamp(sched_out, timezone.utc).strftime("%Y-%m-%d")}
    if dep_tz:
        dates.add(datetime.fromtimestamp(sched_out, dep_tz).strftime("%Y-%m-%d"))
    same = [x for x in cands if x["date"] in dates and not x["ts"]]
    return same[0] if len(same) == 1 else None
