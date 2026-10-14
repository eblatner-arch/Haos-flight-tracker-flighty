"""Free public feeds: FAA NAS status, aviationweather.gov METARs, adsb.lol live positions."""

import logging
import time
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("flight_tracker")

UA = {"User-Agent": "haos-flight-tracker (+https://github.com/eblatner-arch/haos-flight-tracker)"}
NAS_URL = "https://nasstatus.faa.gov/api/airport-status-information"
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


def fetch_nas():
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


def fetch_live(callsign):
    """Live ADS-B position by callsign, or None."""
    r = requests.get(ADSB_URL.format(callsign.upper()), headers=UA, timeout=15)
    r.raise_for_status()
    for ac in (r.json() or {}).get("ac") or []:
        if ac.get("lat") is None or ac.get("lon") is None:
            continue
        alt = ac.get("alt_baro")
        return {
            "lat": ac["lat"], "lon": ac["lon"],
            "alt_ft": 0 if alt == "ground" else alt,
            "gs_kt": ac.get("gs"), "heading": ac.get("track") or ac.get("true_heading"),
            "tail": ac.get("r"), "type": ac.get("t"), "squawk": ac.get("squawk"),
            "at": time.time() - float(ac.get("seen_pos") or 0),
        }
    return None
