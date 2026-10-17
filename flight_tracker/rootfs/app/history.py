"""Your flight history: every Calendar and Manual flight once it's done, plus imports (Flighty).

Stored apart from the live state so it can grow for years:
  <data>/history.json        {"flights": {id: record}}
  <data>/tracks/<id>.json    the flown track ([lat, lon, alt_ft, gs_kt, epoch], ...)

A record:
  id, date (YYYY-MM-DD, local at the origin), airline (ICAO), flight_no, ident, display,
  dep, arr, diverted_to, cancelled, kind (calendar | manual), source (tracker | flighty | ...),
  times {out, off, on, in: {s, a}} (epoch seconds), dep_terminal, dep_gate, arr_terminal, arr_gate,
  aircraft (friendly name), type (ICAO code), tail, distance_nm, route, has_track, added, updated
"""

import csv
import io
import json
import logging
import math
import os
import threading
import time
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import airports

log = logging.getLogger("flight_tracker")

LOCK = threading.RLock()
_DIR = None
_DATA = {"flights": {}}

TYPE_NAMES = {
    "B737": "Boeing 737-700", "B738": "Boeing 737-800", "B739": "Boeing 737-900ER", "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8", "B39M": "Boeing 737 MAX 9", "B3XM": "Boeing 737 MAX 10",
    "B752": "Boeing 757-200", "B753": "Boeing 757-300", "B762": "Boeing 767-200", "B763": "Boeing 767-300",
    "B764": "Boeing 767-400ER", "B772": "Boeing 777-200", "B77W": "Boeing 777-300ER", "B788": "Boeing 787-8",
    "B789": "Boeing 787-9", "B78X": "Boeing 787-10", "A319": "Airbus A319", "A320": "Airbus A320",
    "A20N": "Airbus A320neo", "A321": "Airbus A321", "A21N": "Airbus A321neo", "A333": "Airbus A330-300",
    "A359": "Airbus A350-900", "E145": "Embraer RJ145", "E170": "Embraer 170", "E75L": "Embraer 175",
    "E175": "Embraer 175", "CRJ2": "Bombardier CRJ200", "CRJ7": "Bombardier CRJ700", "CRJ9": "Bombardier CRJ900",
}
NAME_TYPES = {v.lower(): k for k, v in TYPE_NAMES.items()}
IATA_AIRLINE = {"UA": "UAL", "AA": "AAL", "DL": "DAL", "WN": "SWA", "B6": "JBU", "AS": "ASA", "NK": "NKS",
                "F9": "FFT", "G4": "AAY", "HA": "HAL", "AC": "ACA", "BA": "BAW", "LH": "DLH", "AF": "AFR"}
ICAO_IATA = {v: k for k, v in IATA_AIRLINE.items()}


# ---------------------------------------------------------------- storage

def init(data_dir):
    global _DIR, _DATA
    _DIR = data_dir
    os.makedirs(os.path.join(data_dir, "tracks"), exist_ok=True)
    try:
        with open(os.path.join(data_dir, "history.json")) as f:
            _DATA = json.load(f)
        _DATA.setdefault("flights", {})
    except FileNotFoundError:
        _DATA = {"flights": {}}
    except Exception as e:
        log.error("History file unreadable (%s); keeping it aside and starting empty", e)
        try:
            os.replace(os.path.join(data_dir, "history.json"), os.path.join(data_dir, f"history.bad-{int(time.time())}.json"))
        except Exception:
            pass
        _DATA = {"flights": {}}
    for r in _DATA["flights"].values():  # 3.2.2 and earlier counted a diversion's distance to the planned airport
        if r.get("diverted_to"):
            r["distance_nm"] = distance_nm(r.get("dep"), r["diverted_to"]) or r.get("distance_nm")
    gone = [k for k, r in _DATA["flights"].items() if r.get("source") == "flighty" and _is_future(r)]
    for k in gone:  # planned flights an earlier import brought in
        del _DATA["flights"][k]
    if gone:
        log.info("History: removed %d future flights from the Flighty import", len(gone))
        save()
    log.info("History: %d flights", len(_DATA["flights"]))


def _is_future(r, now=None):
    t = r.get("times") or {}
    if any((t.get(k) or {}).get("a") for k in ("out", "off", "on", "in")):
        return False
    return ((t.get("out") or {}).get("s") or (t.get("in") or {}).get("s") or 0) > (now or time.time()) - 3600


def save():
    if not _DIR:
        return
    with LOCK:
        data = json.dumps(_DATA)
    tmp = os.path.join(_DIR, "history.json.tmp")
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, os.path.join(_DIR, "history.json"))


def flights():
    with LOCK:
        return list(_DATA["flights"].values())


def get(fid):
    with LOCK:
        r = _DATA["flights"].get(fid)
        return dict(r) if r else None


def delete(fid):
    with LOCK:
        gone = _DATA["flights"].pop(fid, None)
    if gone:
        try:
            os.remove(_track_path(fid))
        except FileNotFoundError:
            pass
        save()
    return bool(gone)


def _track_path(fid):
    return os.path.join(_DIR or ".", "tracks", "".join(c for c in fid if c.isalnum() or c in "-_") + ".json")


def track(fid):
    try:
        with open(_track_path(fid)) as f:
            return json.load(f)
    except Exception:
        return []


def _save_track(fid, pts):
    if not _DIR or not pts:
        return
    with open(_track_path(fid), "w") as f:
        json.dump(pts, f)


# ---------------------------------------------------------------- records

def make_id(date, ident, dep):
    return f"{date}_{(ident or '').upper()}_{(dep or '').upper()}"


def _tz(code):
    try:
        return ZoneInfo(airports.get(code).get("tz") or "UTC")
    except Exception:
        return timezone.utc


def local_date(ts, code):
    return datetime.fromtimestamp(ts, _tz(code)).strftime("%Y-%m-%d") if ts else ""


def distance_nm(dep, arr):
    a, b = airports.get(dep), airports.get(arr)
    if not a or not b or a.get("lat") is None or b.get("lat") is None:
        return None
    r = math.pi / 180
    la1, lo1, la2, lo2 = a["lat"] * r, a["lon"] * r, b["lat"] * r, b["lon"] * r
    d = 2 * math.asin(math.sqrt(math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    return round(d * 3440.065)


def upsert(rec, pts=None, prefer_new=True):
    """Add or merge one record. Fields already known are kept unless the new value is set and
    prefer_new (live tracking beats an import for the same flight)."""
    rec = {k: v for k, v in rec.items() if v not in (None, "", [], {})}
    fid = rec["id"]
    with LOCK:
        old = _DATA["flights"].get(fid)
        if old:
            merged = dict(old)
            for k, v in rec.items():
                if k == "times":
                    t = {kk: dict(vv) for kk, vv in (old.get("times") or {}).items()}
                    for kk, vv in v.items():
                        cur = t.setdefault(kk, {})
                        for x in ("s", "a"):
                            if vv.get(x) and (prefer_new or not cur.get(x)):
                                cur[x] = vv[x]
                    merged["times"] = t
                elif prefer_new or k not in merged:
                    merged[k] = v
            merged["added"] = old.get("added") or rec.get("added")
            rec = merged
        rec["updated"] = time.time()
        rec.setdefault("added", rec["updated"])
        if pts and len(pts) >= len(track(fid)):
            _save_track(fid, pts)
            rec["has_track"] = True
        _DATA["flights"][fid] = rec
    return fid


def from_leg(L, kind):
    """A history record from a tracked flight (calendar or manual)."""
    f = L.get("flight") or {}
    t = f.get("times") or {}
    o, d = f.get("origin") or {}, f.get("destination") or {}
    dep = o.get("iata") or L.get("dep")
    arr = L.get("arr") or d.get("iata")
    sched_out = (t.get("out") or {}).get("scheduled") or L.get("sched_out")
    times = {}
    for k, fallback in (("out", L.get("sched_out")), ("off", None), ("on", None), ("in", L.get("sched_in"))):
        x = t.get(k) or {}
        times[k] = {"s": x.get("scheduled") or fallback, "a": x.get("actual")}
    diverted_to = f.get("diverted_to") or (d.get("iata") if f.get("diverted") and d.get("iata") and d.get("iata") != arr else None)
    typ = f.get("aircraft_type")
    return {
        "id": make_id(local_date(sched_out, dep), L.get("ident"), dep),
        "date": local_date(sched_out, dep), "airline": L.get("airline") or (L.get("ident") or "")[:3],
        "flight_no": str(L.get("flight_no") or "").lstrip("0") or None, "ident": L.get("ident"),
        "display": L.get("display"), "dep": dep, "arr": arr, "diverted_to": diverted_to,
        "cancelled": bool(f.get("cancelled")), "kind": kind, "source": "tracker",
        "times": times, "dep_terminal": o.get("terminal"), "dep_gate": o.get("gate"),
        "arr_terminal": d.get("terminal"), "arr_gate": d.get("gate"),
        "aircraft": TYPE_NAMES.get(typ) or f.get("aircraft_name"), "type": typ, "tail": f.get("tail"),
        "distance_nm": distance_nm(dep, diverted_to) if diverted_to else (f.get("distance_nm") or distance_nm(dep, arr)),
        "route": f.get("route"), "dh": bool(L.get("dh")),
    }


# ---------------------------------------------------------------- Flighty import

FLIGHTY_COLS = {
    "date": "Date", "airline": "Airline", "flight": "Flight", "from": "From", "to": "To",
    "dep_terminal": "Dep Terminal", "dep_gate": "Dep Gate", "arr_terminal": "Arr Terminal", "arr_gate": "Arr Gate",
    "cancelled": "Canceled", "diverted_to": "Diverted To",
    "out_s": "Gate Departure (Scheduled)", "out_a": "Gate Departure (Actual)",
    "off_s": "Take off (Scheduled)", "off_a": "Take off (Actual)",
    "on_s": "Landing (Scheduled)", "on_a": "Landing (Actual)",
    "in_s": "Gate Arrival (Scheduled)", "in_a": "Gate Arrival (Actual)",
    "aircraft": "Aircraft Type Name", "tail": "Tail Number",
}


def _rows_from_file(name, data):
    """[{header: value}] from a Flighty .xlsx or .csv export."""
    if name.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        hdr = [str(h).strip() if h is not None else "" for h in rows[0]]
        return [dict(zip(hdr, r)) for r in rows[1:] if any(v not in (None, "") for v in r)]
    text = data.decode("utf-8-sig", errors="replace")
    return [r for r in csv.DictReader(io.StringIO(text)) if any((v or "").strip() for v in r.values())]


def _str(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _local_ts(v, code):
    """Flighty times are local to the airport, like 2026-10-10T08:56."""
    if v in (None, ""):
        return None
    try:
        dt = v if isinstance(v, datetime) else datetime.fromisoformat(_str(v).replace("Z", ""))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz(code))
    return dt.timestamp()


def _truthy(v):
    return _str(v).lower() in ("true", "yes", "1", "y")


def parse_flighty(name, data):
    """-> (records, problems)"""
    rows = _rows_from_file(name, data)
    if rows and FLIGHTY_COLS["from"] not in rows[0]:
        raise ValueError("This doesn't look like a Flighty export (no From column).")
    out, problems = [], []
    for i, r in enumerate(rows, start=2):
        g = lambda k: r.get(FLIGHTY_COLS[k])  # noqa: E731
        dep, arr = _str(g("from")).upper(), _str(g("to")).upper()
        al = _str(g("airline")).upper()
        al = IATA_AIRLINE.get(al, al)
        num = _str(g("flight")).lstrip("0")
        if not dep or not arr or not num:
            problems.append(f"row {i}: missing airline, flight or airports")
            continue
        div = _str(g("diverted_to")).upper() or None
        land = div or arr
        times = {"out": {"s": _local_ts(g("out_s"), dep), "a": _local_ts(g("out_a"), dep)},
                 "off": {"s": _local_ts(g("off_s"), dep), "a": _local_ts(g("off_a"), dep)},
                 "on": {"s": _local_ts(g("on_s"), arr), "a": _local_ts(g("on_a"), land)},
                 "in": {"s": _local_ts(g("in_s"), arr), "a": _local_ts(g("in_a"), land)}}
        dv = g("date")
        date = dv.strftime("%Y-%m-%d") if isinstance(dv, datetime) else _str(dv)[:10]
        if times["out"]["s"]:
            date = local_date(times["out"]["s"], dep)
        aircraft = _str(g("aircraft"))
        ident = f"{al}{num}"
        iata = ICAO_IATA.get(al, al[:2])
        out.append({
            "id": make_id(date, ident, dep), "date": date, "airline": al, "flight_no": num, "ident": ident,
            "display": f"{iata} {num}", "dep": dep, "arr": arr, "diverted_to": div,
            "cancelled": _truthy(g("cancelled")), "kind": "manual", "source": "flighty", "times": times,
            "dep_terminal": _str(g("dep_terminal")), "dep_gate": _str(g("dep_gate")),
            "arr_terminal": _str(g("arr_terminal")), "arr_gate": _str(g("arr_gate")),
            "aircraft": aircraft, "type": NAME_TYPES.get(aircraft.lower()), "tail": _str(g("tail")).upper(),
            "distance_nm": distance_nm(dep, div or arr),
        })
    return out, problems


def import_flighty(name, data):
    recs, problems = parse_flighty(name, data)
    # Flighty also exports flights you've planned: only what has already flown (or was cancelled
    # and is in the past) belongs in History; the tracker picks up the rest when you fly them.
    future = _is_future
    ahead = sum(1 for r in recs if future(r))
    recs = [r for r in recs if not future(r)]
    added = updated = 0
    for rec in recs:
        existed = get(rec["id"]) is not None
        upsert(rec, prefer_new=False)  # a flight already recorded by the tracker keeps its own data
        updated += existed
        added += not existed
    save()
    return {"rows": len(recs), "added": added, "already": updated, "future": ahead, "problems": problems[:20]}


def import_tracks(blob):
    """tracks.zip from the flight-track-history backfill (or the GitHub artifact zip around it):
    tracks/<id>.json for flights here that don't have a track yet."""
    entries, partial = _zip_entries(blob)
    inner = [n for n in entries if n.lower().endswith(".zip")]
    if inner and not any(n.startswith("tracks/") for n in entries):
        entries, p2 = _zip_entries(entries[inner[0]])
        partial = partial or p2
    names = [n for n in entries if n.startswith("tracks/") and n.endswith(".json")]
    if not names:
        raise ValueError("No tracks in that file. Use the tracks.zip from the Backfill tracks run.")
    added = kept = unknown = 0
    with LOCK:
        for n in names:
            fid = os.path.basename(n)[:-5]
            if get(fid) is None:
                unknown += 1
                continue
            if track(fid):
                kept += 1
                continue
            try:
                pts = json.loads(entries[n])
            except ValueError:
                continue
            if pts:
                _save_track(fid, pts)
                _DATA["flights"][fid]["has_track"] = True
                added += 1
        if added:
            save()
    return {"tracks": len(names), "added": added, "kept": kept, "unknown": unknown, "partial": partial}


def _zip_entries(blob):
    """{name: bytes} of a zip. A download that got cut off (no directory at the end) is read
    entry by entry from the front, keeping every complete file: (entries, partial)."""
    import struct
    import zlib
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
        return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}, False
    except zipfile.BadZipFile:
        pass
    out, i = {}, 0
    while True:
        i = blob.find(b"PK\x03\x04", i)
        if i < 0 or i + 30 > len(blob):
            break
        _, flag, meth, _, _, _, csize, usize, nl, el = struct.unpack("<HHHHHIIIHH", blob[i + 4:i + 30])
        name = blob[i + 30:i + 30 + nl].decode("utf-8", "replace")
        start = i + 30 + nl + el
        data = blob[start:start + csize] if csize and not flag & 8 else blob[start:]
        try:
            if meth == 8:
                d = zlib.decompressobj(-15)
                raw = d.decompress(data)
                if not d.eof:
                    raise ValueError("cut off")
            elif meth == 0 and csize and len(data) == csize:
                raw = data
            elif meth == 0 and name.lower().endswith(".zip"):
                raw = data  # a stored zip inside, maybe cut off too: read what's there
            else:
                raise ValueError("unsupported")
            if not name.endswith("/"):
                out[name] = raw
        except Exception:
            pass
        i = start if meth == 0 and name.lower().endswith(".zip") else i + 4
    if not out:
        raise ValueError("That file isn't a zip, or it was cut off before the first track. Download it again.")
    return out, True


# ---------------------------------------------------------------- stats

def _t(rec, k, x):
    return ((rec.get("times") or {}).get(k) or {}).get(x)


def links(recs=None):
    """Diversions as journeys: a flight that diverted (FLL→EWR, landed at IAD) and the same flight
    number continuing from the diversion airport to the original destination (IAD→EWR) the same
    or next day. -> ({diverted id: continuation record}, {continuation id: diverted record})"""
    recs = flights() if recs is None else recs
    by = {}
    for r in recs:
        by.setdefault(r.get("ident"), []).append(r)
    cont, of = {}, {}
    for r in recs:
        dv = r.get("diverted_to")
        if not dv:
            continue
        landed = _t(r, "in", "a") or _t(r, "on", "a") or _t(r, "out", "s") or 0
        best = None
        for c in by.get(r.get("ident"), []):
            out = _t(c, "out", "a") or _t(c, "out", "s") or 0
            if c is r or c.get("dep") != dv or c.get("arr") != r.get("arr") or not (landed <= out <= landed + 36 * 3600):
                continue
            if best is None or out < (_t(best, "out", "a") or _t(best, "out", "s") or 0):
                best = c
        if best:
            cont[r["id"]], of[best["id"]] = best, r
    return cont, of


def delays(rec, lk=None):
    """(departure delay, arrival delay) in minutes, from gate times; None when unknown.
    A diverted flight's arrival delay is when you finally reached the planned destination (the
    continuation leg) against the original schedule; the continuation leg itself has none, since
    its 'schedule' is the original flight's."""
    cont, of = lk or ({}, {})
    if rec.get("id") in of:
        return None, None
    dd = (_t(rec, "out", "a") - _t(rec, "out", "s")) / 60 if _t(rec, "out", "a") and _t(rec, "out", "s") else None
    ad = (_t(rec, "in", "a") - _t(rec, "in", "s")) / 60 if _t(rec, "in", "a") and _t(rec, "in", "s") else None
    if rec.get("cancelled"):
        ad = None
    elif rec.get("diverted_to"):
        c = cont.get(rec.get("id"))
        ad = (_t(c, "in", "a") - _t(rec, "in", "s")) / 60 if c and _t(c, "in", "a") and _t(rec, "in", "s") else None
    return dd, ad


def summary(rec, lk=None):
    lk = lk or links()
    dd, ad = delays(rec, lk)
    blk = (_t(rec, "in", "a") - _t(rec, "out", "a")) / 60 if _t(rec, "in", "a") and _t(rec, "out", "a") else None
    air = (_t(rec, "on", "a") - _t(rec, "off", "a")) / 60 if _t(rec, "on", "a") and _t(rec, "off", "a") else None
    s = {k: rec.get(k) for k in ("id", "date", "display", "ident", "dep", "arr", "diverted_to", "cancelled", "kind",
                                  "source", "aircraft", "type", "tail", "distance_nm", "has_track", "dh",
                                  "dep_gate", "arr_gate", "dep_terminal", "arr_terminal", "route")}
    c, o = lk[0].get(rec.get("id")), lk[1].get(rec.get("id"))
    if c:
        s.update(continued_by=c["id"], final_arrival=_t(c, "in", "a"), final_ap=c.get("arr"))
    if o:
        s.update(continuation_of=o["id"], continuation_from=o.get("dep"))
    s.update(times=rec.get("times") or {}, dep_delay=dd, arr_delay=ad, block_min=blk, air_min=air,
             dep_tz=airports.get(rec.get("dep")).get("tz"), arr_tz=airports.get(rec.get("diverted_to") or rec.get("arr")).get("tz"))
    return s


ON_TIME = 15  # minutes late at the gate still counted on time (the usual A14 measure)


def stats(recs, lk=None):
    lk = lk or links()
    flown = [r for r in recs if not r.get("cancelled")]

    def dl(r):  # journey-aware delays (diversions)
        return delays(r, lk)
    n = len(flown)
    dist = sum(r.get("distance_nm") or 0 for r in flown)
    air = sum(((_t(r, "on", "a") or 0) - (_t(r, "off", "a") or 0)) / 60 for r in flown if _t(r, "on", "a") and _t(r, "off", "a"))
    blk = sum(((_t(r, "in", "a") or 0) - (_t(r, "out", "a") or 0)) / 60 for r in flown if _t(r, "in", "a") and _t(r, "out", "a"))
    arrd = [dl(r)[1] for r in flown if dl(r)[1] is not None]
    depd = [dl(r)[0] for r in flown if dl(r)[0] is not None]

    def top(key, k=8):
        c = {}
        for r in flown:
            v = key(r)
            if v:
                c[v] = c.get(v, 0) + 1
        return sorted(c.items(), key=lambda x: (-x[1], x[0]))[:k]

    def delay_by(key, minimum=3, k=6):
        g = {}
        for r in flown:
            d = dl(r)[1]
            v = key(r)
            if d is not None and v:
                g.setdefault(v, []).append(d)
        rows = [(v, len(ds), sum(ds) / len(ds), sum(1 for x in ds if x < ON_TIME) / len(ds)) for v, ds in g.items()
                if len(ds) >= minimum]
        return sorted(rows, key=lambda x: -x[2])[:k]

    hours = {}
    for r in flown:
        if _t(r, "out", "s") and dl(r)[1] is not None:
            h = datetime.fromtimestamp(_t(r, "out", "s"), _tz(r.get("dep"))).hour
            b = f"{(h // 3) * 3:02d}–{(h // 3) * 3 + 3:02d}"
            hours.setdefault(b, []).append(dl(r)[1])
    longest = max(flown, key=lambda r: r.get("distance_nm") or 0, default=None)
    worst = max((r for r in flown if dl(r)[1] is not None), key=lambda r: dl(r)[1], default=None)
    apts = set()
    for r in flown:
        apts.update(x for x in (r.get("dep"), r.get("arr"), r.get("diverted_to")) if x)
    return {
        "flights": n, "cancelled": len(recs) - n, "diverted": sum(1 for r in flown if r.get("diverted_to")),
        "distance_nm": round(dist), "air_hours": round(air / 60, 1), "block_hours": round(blk / 60, 1),
        "airports": len(apts), "tails": len({r.get("tail") for r in flown if r.get("tail")}),
        "routes": len({tuple(sorted((r.get("dep"), r.get("arr")))) for r in flown}),
        "on_time_pct": round(100 * sum(1 for d in arrd if d < ON_TIME) / len(arrd)) if arrd else None,
        "avg_arr_delay": round(sum(arrd) / len(arrd), 1) if arrd else None,
        "avg_dep_delay": round(sum(depd) / len(depd), 1) if depd else None,
        "early_pct": round(100 * sum(1 for d in arrd if d <= -5) / len(arrd)) if arrd else None,
        "late_30_pct": round(100 * sum(1 for d in arrd if d >= 30) / len(arrd)) if arrd else None,
        "top_routes": top(lambda r: f"{r.get('dep')}–{r.get('arr')}"),
        "top_airports": sorted({a: sum(1 for r in flown if a in (r.get("dep"), r.get("arr"))) for a in apts}.items(),
                               key=lambda x: (-x[1], x[0]))[:8],
        "top_aircraft": top(lambda r: r.get("aircraft") or TYPE_NAMES.get(r.get("type") or "")),
        "top_tails": top(lambda r: r.get("tail"), 6),
        "late_routes": delay_by(lambda r: f"{r.get('dep')}–{r.get('arr')}"),
        "late_airports": delay_by(lambda r: r.get("dep"), minimum=4),
        "by_hour": sorted((b, len(v), round(sum(v) / len(v), 1)) for b, v in hours.items()),
        "longest": summary(longest, lk) if longest else None, "worst_delay": summary(worst, lk) if worst else None,
        "years": sorted({(r.get("date") or "")[:4] for r in recs if r.get("date")}, reverse=True),
    }


# ---------------------------------------------------------------- clearing, export, backup

def clear(source=None):
    """Delete every record (source=None) or only those from one source ('flighty'). -> count"""
    with LOCK:
        gone = [fid for fid, r in _DATA["flights"].items() if source is None or r.get("source") == source]
        for fid in gone:
            _DATA["flights"].pop(fid, None)
    for fid in gone:
        try:
            os.remove(_track_path(fid))
        except FileNotFoundError:
            pass
    save()
    return len(gone)


def _local_iso(ts, code):
    return datetime.fromtimestamp(ts, _tz(code)).strftime("%Y-%m-%dT%H:%M") if ts else ""


def export_csv():
    """All flights in Flighty's column layout (local airport times), so the file re-imports here or elsewhere."""
    cols = list(FLIGHTY_COLS.values())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in sorted(flights(), key=lambda x: (x.get("date") or "", _t(x, "out", "s") or 0), reverse=True):
        dep, arr, land = r.get("dep"), r.get("arr"), r.get("diverted_to") or r.get("arr")
        iata = ICAO_IATA.get(r.get("airline") or "", r.get("airline") or "")
        w.writerow([r.get("date"), r.get("airline") or iata, r.get("flight_no"), dep, arr,
                    r.get("dep_terminal") or "", r.get("dep_gate") or "", r.get("arr_terminal") or "", r.get("arr_gate") or "",
                    "true" if r.get("cancelled") else "false", r.get("diverted_to") or "",
                    _local_iso(_t(r, "out", "s"), dep), _local_iso(_t(r, "out", "a"), dep),
                    _local_iso(_t(r, "off", "s"), dep), _local_iso(_t(r, "off", "a"), dep),
                    _local_iso(_t(r, "on", "s"), arr), _local_iso(_t(r, "on", "a"), land),
                    _local_iso(_t(r, "in", "s"), arr), _local_iso(_t(r, "in", "a"), land),
                    r.get("aircraft") or TYPE_NAMES.get(r.get("type") or "", ""), r.get("tail") or ""])
    return buf.getvalue().encode("utf-8")


def make_backup(extra=None):
    """A zip with everything needed to rebuild History: history.json, every track, plus `extra`
    (settings worth keeping) as state.json."""
    import zipfile
    out = io.BytesIO()
    with LOCK:
        data = json.dumps(_DATA)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("history.json", data)
        z.writestr("state.json", json.dumps({"saved_at": time.time(), **(extra or {})}))
        tdir = os.path.join(_DIR or ".", "tracks")
        for name in sorted(os.listdir(tdir)) if os.path.isdir(tdir) else []:
            if name.endswith(".json"):
                z.write(os.path.join(tdir, name), f"tracks/{name}")
    return out.getvalue()


def restore_backup(blob):
    """Merge a backup zip into History (flights already here are kept). -> (result, extra state)"""
    import zipfile
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = set(z.namelist())
    if "history.json" not in names:
        raise ValueError("This isn't a Flight Tracker backup (no history.json inside).")
    data = json.loads(z.read("history.json"))
    added = 0
    for fid, rec in (data.get("flights") or {}).items():
        if get(fid) is None:
            added += 1
        pts = None
        tn = f"tracks/{os.path.basename(_track_path(fid))}"
        if tn in names:
            try:
                pts = json.loads(z.read(tn))
            except Exception:
                pts = None
        upsert(dict(rec, id=fid), pts=pts, prefer_new=False)
    save()
    extra = json.loads(z.read("state.json")) if "state.json" in names else {}
    return {"flights": len(data.get("flights") or {}), "added": added}, extra
