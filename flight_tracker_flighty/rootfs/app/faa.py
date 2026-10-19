"""The FAA aircraft registry (registry.faa.gov ReleasableAircraft.zip), for each tail's model, year
built and airworthiness date (an airliner's is within days of its first flight and delivery).

The zip (about 70 MB, updated by the FAA every day) is downloaded into /data, and the parts we use
are loaded into a small SQLite database (/data/faa.sqlite, about 300,000 aircraft). It's downloaded
again when a tail isn't in it (newly registered), or is but as a different type than the flight
says (the N-number was reused), at most once a day, and every 30 days anyway.

An airplane that came from abroad or changed hands can carry a recent airworthiness date: the
deregistered file is searched for the same serial number's earliest one, and when none is older,
the age comes from the year it was built instead.

If the FAA site won't answer the add-on, put ReleasableAircraft.zip in /share and it's used.
"""
import csv
import io
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request
import zipfile

log = logging.getLogger("faa")

URL = "https://registry.faa.gov/database/ReleasableAircraft.zip"
SHARE = ("/share/ReleasableAircraft.zip", "/share/flight_tracker/ReleasableAircraft.zip")
REFRESH_DAYS = 30
RETRY_HOURS = 24

_dir = "/data"
_lock = threading.Lock()
_busy = threading.Lock()
_state = {"rows": 0, "built": 0, "file_date": None, "last_try": 0, "error": None, "busy": False}


def _db_path():
    return os.path.join(_dir, "faa.sqlite")


def _connect():
    return sqlite3.connect(_db_path(), timeout=10, check_same_thread=False)


# ---------------------------------------------------------------- does the model match the flight's type?
_TYPE_RE = {
    "B737": r"^737-7\w\w$|^737-700", "B738": r"^737-8\w\w$|^737-800", "B739": r"^737-9\w\w(ER)?$|^737-900",
    "B37M": r"^737-7$", "B38M": r"^737-8$", "B39M": r"^737-9$", "B3XM": r"^737-10$",
    "B752": r"^757-2", "B753": r"^757-3", "B762": r"^767-2", "B763": r"^767-3", "B764": r"^767-4",
    "B772": r"^777-2", "B77W": r"^777-3", "B77L": r"^777-2", "B788": r"^787-8", "B789": r"^787-9", "B78X": r"^787-10",
    "A319": r"^A319", "A320": r"^A320-2\d\d$", "A20N": r"^A320-2\d\dN", "A321": r"^A321-[12]\d\d$",
    "A21N": r"^A321-2\d\dN", "A332": r"^A330-2", "A333": r"^A330-3", "A339": r"^A330-9", "A359": r"^A350-9",
    "E170": r"ERJ 170-100|EMB-170", "E175": r"ERJ 170-200|EMB-175", "E75L": r"ERJ 170-200", "E75S": r"ERJ 170-200",
    "E145": r"EMB-145", "E45X": r"EMB-145XR", "E135": r"EMB-135", "E190": r"ERJ 190-100", "E195": r"ERJ 190-200",
    "CRJ2": r"CL-600-2B19", "CRJ7": r"CL-600-2C1[01]", "CRJ9": r"CL-600-2D24", "CRJX": r"CL-600-2E25",
    "BCS1": r"BD-500-1A10", "BCS3": r"BD-500-1A11",
}


def type_ok(icao_type, model):
    rx = _TYPE_RE.get((icao_type or "").upper())
    if not rx or not model:
        return True   # a type we don't know how to check
    return bool(re.search(rx, model.strip().upper()))


# ---------------------------------------------------------------- building the database
def _rows(z, name):
    with z.open(name) as f:
        r = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace"))
        head = [h.strip().upper().replace("-", " ") for h in next(r)]
        idx = {h: i for i, h in enumerate(head)}
        for row in r:
            yield idx, row


def _col(idx, row, name):
    i = idx.get(name)
    return row[i].strip() if i is not None and i < len(row) else ""


def build(zpath):
    t0 = time.time()
    z = zipfile.ZipFile(zpath)
    names = {n.upper(): n for n in z.namelist()}
    ref = {}
    for idx, row in _rows(z, names["ACFTREF.TXT"]):
        code = _col(idx, row, "CODE")
        if code:
            ref[code] = (_col(idx, row, "MFR"), _col(idx, row, "MODEL"))
    out, by_serial = [], {}
    for idx, row in _rows(z, names["MASTER.TXT"]):
        n = _col(idx, row, "N NUMBER").upper()
        if not n:
            continue
        code = _col(idx, row, "MFR MDL CODE")
        mfr, model = ref.get(code, ("", ""))
        aw = _col(idx, row, "AIR WORTH DATE")
        aw = aw if re.fullmatch(r"\d{8}", aw) else ""
        yr = _col(idx, row, "YEAR MFR")
        yr = int(yr) if yr.isdigit() else None
        serial = _col(idx, row, "SERIAL NUMBER")
        out.append(["N" + n, serial, code, mfr, model, yr, aw, ""])
        if serial and code:
            by_serial[(serial, code)] = len(out) - 1
    # the same airframe's earlier registrations: its first airworthiness date
    if "DEREG.TXT" in names:
        for idx, row in _rows(z, names["DEREG.TXT"]):
            k = (_col(idx, row, "SERIAL NUMBER"), _col(idx, row, "MFR MDL CODE"))
            i = by_serial.get(k)
            if i is None:
                continue
            aw = _col(idx, row, "AIR WORTH DATE")
            if re.fullmatch(r"\d{8}", aw) and aw < (out[i][7] or out[i][6] or "99999999"):
                out[i][7] = aw
    tmp = _db_path() + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.execute("CREATE TABLE ac (n TEXT PRIMARY KEY, serial TEXT, code TEXT, mfr TEXT, model TEXT, year INT, aw TEXT, first_aw TEXT)")
    db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    db.executemany("INSERT OR REPLACE INTO ac VALUES (?,?,?,?,?,?,?,?)", out)
    info = z.getinfo(names["MASTER.TXT"])
    fdate = "%04d-%02d-%02d" % info.date_time[:3]
    db.executemany("INSERT INTO meta VALUES (?,?)", [("built", str(time.time())), ("file_date", fdate), ("rows", str(len(out)))])
    db.commit()
    db.close()
    with _lock:
        os.replace(tmp, _db_path())
        _state.update(rows=len(out), built=time.time(), file_date=fdate, error=None)
    log.info("FAA registry: %d aircraft loaded (file of %s) in %.0f s", len(out), fdate, time.time() - t0)


def _download():
    zpath = os.path.join(_dir, "ReleasableAircraft.zip")
    req = urllib.request.Request(URL, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Accept": "application/zip,*/*"})
    with urllib.request.urlopen(req, timeout=120) as r, open(zpath + ".tmp", "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    zipfile.ZipFile(zpath + ".tmp").testzip()
    os.replace(zpath + ".tmp", zpath)
    return zpath


def refresh(reason=""):
    """Download the newest file (or use one in /share) and rebuild. One at a time, at most once a day."""
    if not _busy.acquire(blocking=False):
        return
    try:
        _state["busy"] = True
        _state["last_try"] = time.time()
        log.info("FAA registry: downloading%s", f" ({reason})" if reason else "")
        zpath = None
        try:
            zpath = _download()
        except Exception as e:  # noqa: BLE001
            _state["error"] = f"download failed: {e}"
            log.warning("FAA registry: %s", _state["error"])
            for p in SHARE:   # a copy you put in /share
                if os.path.exists(p) and os.path.getmtime(p) > (_state["built"] or 0):
                    zpath = p
                    break
        if zpath:
            build(zpath)
            if zpath.startswith(_dir):
                os.remove(zpath)   # the database has what we need: don't keep 70 MB around
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)
        log.warning("FAA registry: %s", e)
    finally:
        _state["busy"] = False
        _busy.release()


def _maybe_refresh(reason):
    if _busy.locked() or time.time() - _state["last_try"] < RETRY_HOURS * 3600:
        return
    _state["last_try"] = time.time()
    threading.Thread(target=refresh, args=(reason,), daemon=True, name="faa").start()


# ---------------------------------------------------------------- lookups
def lookup(tail, icao_type=None):
    """-> {model, year, aw, first_aw, since} or None (and fetch a newer file if it should be there)."""
    n = (tail or "").strip().upper()
    if not re.fullmatch(r"N[0-9A-Z]{1,5}", n):
        return None   # FAA registry: US aircraft only
    if not os.path.exists(_db_path()):
        _maybe_refresh("first time")
        return None
    with _lock:
        db = _connect()
        try:
            r = db.execute("SELECT serial, mfr, model, year, aw, first_aw FROM ac WHERE n=?", (n,)).fetchone()
        finally:
            db.close()
    if not r:
        _maybe_refresh(f"{n} not in it yet")
        return None
    serial, mfr, model, year, aw, first_aw = r
    if not type_ok(icao_type, model):
        _maybe_refresh(f"{n} is a {model} there, the flight says {icao_type}")
        return None
    since = first_aw or aw
    # a recent date on an older airframe (came from abroad, no earlier US record): use the year built
    if since and year and int(since[:4]) > year + 1:
        since = ""
    return {"tail": n, "mfr": mfr, "model": model, "serial": serial, "year": year,
            "aw": since and f"{since[:4]}-{since[4:6]}-{since[6:]}", "registered_aw": aw and f"{aw[:4]}-{aw[4:6]}-{aw[6:]}"}


def status():
    return {k: _state[k] for k in ("rows", "built", "file_date", "last_try", "error", "busy")}


def start(d):
    global _dir
    _dir = d
    try:
        db = _connect()
        m = dict(db.execute("SELECT k, v FROM meta").fetchall())
        db.close()
        _state.update(rows=int(m.get("rows", 0)), built=float(m.get("built", 0)), file_date=m.get("file_date"))
    except sqlite3.Error:
        pass

    def loop():
        time.sleep(60)
        while True:
            if not _state["built"] or time.time() - _state["built"] > REFRESH_DAYS * 86400:
                refresh("monthly" if _state["built"] else "first time")
            time.sleep(6 * 3600)
    threading.Thread(target=loop, daemon=True, name="faa-monthly").start()
