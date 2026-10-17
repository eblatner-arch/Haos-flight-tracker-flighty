"""Read the crew schedule from the feed copy ICS2GCAL saves, and turn it into legs.

Title patterns match the FAR 117 Tracker, so anything that add-on understands
works here too, e.g. "2204 EWR-IAH", "UA2204 EWR - IAH",
"2204 EWR 27Sep 12:01 - IAH 27Sep 14:50", "DH 1873 IAH-EWR".
"""

import bisect
import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("flight_tracker_flighty")

HOUR = 3600
MAX_LEG_SECONDS = 20 * HOUR

ROUTE_RE = r"\b(?P<dep>[A-Z]{3})\s*(?:-|–|—|>|/|\bto\b)\s*(?P<arr>[A-Z]{3})\b"
DEFAULT_FLIGHT_RE = r"(?:\b(?:[A-Z]{2,3}\s?)?(?P<flt>\d{1,4})[A-Z]?\b.*?)?" + ROUTE_RE
TIMED_FLIGHT_RE = (r"(?:\b(?:[A-Z]{2,3}\s?)?(?P<flt>\d{1,4})[A-Z]?\s+)?\b(?P<dep>[A-Z]{3})\b"
                   r"(?:\s+\d{1,2}[A-Za-z]{3}(?:\d{2,4})?)?\s+\d{1,2}:?\d{2}Z?\s*(?:-|–|—|>)\s*(?P<arr>[A-Z]{3})\b")
DEADHEAD_RE = re.compile(r"\b(?:DH|DHD|DEADHEAD|DEAD\s?HEAD)\b", re.I)
# An airline code directly in front of the flight number: "UA2204", "UAL 2204", "B6 123"
AIRLINE_RE = r"\b(?P<al>[A-Z]{3}|[A-Z]{2}|[A-Z]\d|\d[A-Z])\s?{flt}\b"
NOT_AIRLINES = {"DH", "DHD", "CA", "FO", "RON", "SBY", "RSV", "TRIP"}

# IATA -> ICAO for carriers a US crewmember is likely to ride or fly.
IATA_TO_ICAO = {
    "UA": "UAL", "AA": "AAL", "DL": "DAL", "WN": "SWA", "AS": "ASA", "B6": "JBU", "NK": "NKS",
    "F9": "FFT", "G4": "AAY", "HA": "HAL", "SY": "SCX", "YX": "RPA", "OO": "SKW", "9E": "EDV",
    "MQ": "ENY", "OH": "JIA", "ZW": "AWI", "YV": "ASH", "G7": "GJS", "PT": "PDT", "C5": "UCA",
    "QX": "QXE", "AC": "ACA", "WS": "WJA", "MX": "MXY", "XP": "CXP", "BA": "BAW", "LH": "DLH",
    "AF": "AFR", "KL": "KLM", "NH": "ANA", "EI": "EIN", "AM": "AMX", "CM": "CMP",
}


def load_file(path):
    if not os.path.exists(path):
        raise RuntimeError(
            f"No schedule file at {path}. Turn on save_feed_copy in the ICS2GCAL add-on "
            "(version 1.4.0 or later); it writes your feed there.")
    with open(path, "rb") as f:
        data = f.read()
    if not data.strip():
        raise RuntimeError(f"{path} is empty")
    return data, os.path.getmtime(path)


def _ts(value, default_tz):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=default_tz)
        return value.timestamp(), False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=default_tz).timestamp(), True
    raise ValueError(f"unsupported date value {value!r}")


def parse_ics(ics_bytes, default_tz, now, days=60):
    """Expand the feed into plain events with UTC epoch start/end (next `days` days, last 2)."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics_bytes)
    recurring = set()
    for comp in cal.walk("VEVENT"):
        if comp.get("RRULE") is not None or comp.get("RDATE") is not None or comp.get("RECURRENCE-ID") is not None:
            recurring.add(str(comp.get("UID") or ""))
    start = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=2)
    end = datetime.fromtimestamp(now, timezone.utc) + timedelta(days=days)
    out = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        try:
            s, allday = _ts(comp.get("DTSTART").dt, default_tz)
            if comp.get("DTEND") is not None:
                e, _ = _ts(comp.get("DTEND").dt, default_tz)
            elif comp.get("DURATION") is not None:
                e = s + comp.get("DURATION").dt.total_seconds()
            else:
                e = s + (86400 if allday else 0)
        except Exception as ex:
            log.warning("Skipping event with unreadable time: %s", ex)
            continue
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        uid = str(comp.get("UID") or "")
        rid = comp.get("RECURRENCE-ID")
        slot = rid.dt.isoformat() if uid in recurring and rid is not None and hasattr(rid, "dt") else ""
        if not uid:
            uid = f"{comp.get('SUMMARY', '')}|{s}"
        out.append({
            "oid": hashlib.sha1(f"{uid}|{slot}".encode()).hexdigest()[:16],
            "start": s, "end": e, "allday": allday,
            "summary": str(comp.get("SUMMARY", "") or "").strip(),
            "location": str(comp.get("LOCATION", "") or "").strip(),
        })
    return out


def airline_for(summary, flt, default_icao):
    if flt:
        m = re.search(AIRLINE_RE.replace("{flt}", re.escape(flt)), summary)
        if m and m.group("al").upper() not in NOT_AIRLINES:
            al = m.group("al").upper()
            return IATA_TO_ICAO.get(al, al) if len(al) == 2 else al
    return (default_icao or "").upper()


def legs_from_events(events, cfg):
    """Pick out flights. Each leg: id, ident, flight_no, airline, dep, arr, sched_out, sched_in, dh, summary."""
    custom = (cfg.get("flight_regex") or "").strip()
    patterns = [re.compile(p) for p in ([custom] if custom else [DEFAULT_FLIGHT_RE, TIMED_FLIGHT_RE])]
    found = []
    for e in events:
        if e.get("allday"):
            continue
        dur = e["end"] - e["start"]
        if dur <= 0 or dur > MAX_LEG_SECONDS:
            continue
        summ, loc = e.get("summary") or "", e.get("location") or ""
        m = None
        for pat in patterns:
            m = pat.search(summ) or pat.search(loc)
            if m:
                break
        if not m:
            continue
        gd = m.groupdict()
        flt = (gd.get("flt") or "").lstrip("0")
        airline = airline_for(summ, gd.get("flt") or "", cfg.get("airline_icao"))
        found.append({
            "id": e["oid"],
            "flight_no": flt,
            "airline": airline,
            "ident": f"{airline}{flt}" if airline and flt else "",
            "dep": gd.get("dep") or "",
            "arr": gd.get("arr") or "",
            "sched_out": e["start"],
            "sched_in": e["end"],
            "dh": bool(DEADHEAD_RE.search(summ)),
            "summary": summ,
        })
    # A whole-trip block that spans other flights is not a flight itself.
    found.sort(key=lambda L: L["sched_out"])
    starts = [L["sched_out"] for L in found]
    keep = []
    for L in found:
        lo = bisect.bisect_left(starts, L["sched_out"])
        hi = bisect.bisect_right(starts, L["sched_in"])
        inside = [o for o in found[lo:hi] if o is not L and o["sched_in"] <= L["sched_in"]
                  and (o["sched_in"] - o["sched_out"]) < (L["sched_in"] - L["sched_out"])]
        if not inside:
            keep.append(L)
    return keep
