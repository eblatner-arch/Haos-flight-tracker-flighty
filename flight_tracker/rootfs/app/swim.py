"""FAA SWIM (TFMS) flight data: a live feed of every flight plan, amendment, departure and arrival
in the US airspace system, straight from the FAA. Used here for diversions (a flight plan amended
to a new destination) the moment ATC files them, before FlightAware shows them.

The feed is a queue on the FAA's Solace message broker (your SWIFT portal subscription: host,
VPN, username, password, queue). One background thread stays connected, reads every message,
keeps only those for flights we care about (WATCH), and acknowledges the rest so the queue
never backs up.
"""

import logging
import threading
import time
import xml.etree.ElementTree as ET

log = logging.getLogger("flight_tracker")

STATE = {"status": "off", "error": None, "connected_at": None, "messages": 0, "matched": 0,
         "last_message": None, "rate": 0.0}
WATCH = set()        # callsigns to keep (UAL2608 …), set by the server each cycle
EVENTS = {}          # callsign -> [event, …] newest last
LOCK = threading.Lock()
KEEP = 40            # events per callsign
SAMPLES = {}         # message type -> a few raw messages, to check the parser against the real feed
MATCHED_RAW = []     # the last raw messages that matched your flights
TYPE_COUNTS = {}
import re as _re
_TYPE_RE = _re.compile(r'msgType="([^"]+)"')
_SRC_RE = _re.compile(r'sourceFacility="([A-Z0-9]{2,3})"')  # airline-sent (CDM) messages: AAL, SKW, UAL…
SOURCE_COUNTS = {}
_cfg = {}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _apt(code):
    """TFMS airports are ICAO (KEWR) or FAA codes (ATL): -> as given, upper case."""
    return (code or "").strip().upper() or None


_AIRLINE_ATTRS = {"out": "airlineOutTime", "off": "airlineOffTime", "on": "airlineOnTime", "in": "airlineInTime",
                  "gate_out": "gateDeparture", "runway_off": "runwayDeparture",
                  "gate_in": "gateArrival", "runway_on": "runwayArrival",
                  "sched_out": "originalDeparture", "sched_in": "originalArrival"}


def route_text(legacy):
    """FAA legacy route -> readable tokens: 'KMDT./.LVZ245023..RACKI.FQM3.KEWR/0258' ->
    'KMDT LVZ245023 RACKI FQM3 KEWR' (the '/0258' is the arrival estimate; './.' is 'from here')."""
    s = _re.sub(r"/\d{4}$", "", (legacy or "").strip())
    return " ".join(t for t in _re.split(r"\.+", s) if t and t != "/") or None


def _dms_position(el):
    """TFMS <position> with latitudeDMS/longitudeDMS -> (lat, lon)."""
    out = {}
    for c in el.iter():
        n = _local(c.tag)
        if n in ("latitudeDMS", "longitudeDMS"):
            try:
                v = float(c.get("degrees") or 0) + float(c.get("minutes") or 0) / 60 + float(c.get("seconds") or 0) / 3600
            except ValueError:
                return None
            if c.get("direction") in ("SOUTH", "WEST"):
                v = -v
            out["lat" if n == "latitudeDMS" else "lon"] = round(v, 5)
    return (out["lat"], out["lon"]) if len(out) == 2 else None


# US civil registrations map one-to-one onto ICAO addresses A00001–ADF7C7, so a transponder
# address from FAA radar gives the N-number with no lookup.
_NCH, _NDG = "ABCDEFGHJKLMNPQRSTUVWXYZ", "0123456789"
_NS = 1 + len(_NCH) * (1 + len(_NCH))
_NB4 = 1 + len(_NCH) + len(_NDG)
_NB3 = len(_NDG) * _NB4 + _NS
_NB2 = len(_NDG) * _NB3 + _NS
_NB1 = len(_NDG) * _NB2 + _NS


def _nsuffix(o):
    if o == 0:
        return ""
    c = _NCH[(o - 1) // (len(_NCH) + 1)]
    r = (o - 1) % (len(_NCH) + 1)
    return c if r == 0 else c + _NCH[r - 1]


def n_number(hex_addr):
    """ICAO 24-bit address -> US registration ('aaf006' -> 'N80347'), None for non-US."""
    try:
        i = int(hex_addr, 16) - 0xA00001
    except (TypeError, ValueError):
        return None
    if i < 0 or i > 0xADF7C7 - 0xA00001:
        return None
    out, r = "N" + str(i // _NB1 + 1), i % _NB1
    for b in (_NB2, _NB3):
        if r < _NS:
            return out + _nsuffix(r)
        r -= _NS
        out, r = out + str(r // b), r % b
    if r < _NS:
        return out + _nsuffix(r)
    r -= _NS
    out, r = out + str(r // _NB4), r % _NB4
    return out if r == 0 else out + (_NCH + _NDG)[r - 1]


def parse(xml_text):
    """Flight-data messages in one TFMS payload -> [{acid, dep, arr, type, at, igtd, gufi, off, on, eta, diverted,
    src, trigger, status, airline}]. airline = {out, off, on, in} as the airline itself filed them (CDM).
    off/on are actual wheels-up/down (UTC ISO), eta the FAA estimate, diverted the diversionIndicator flag.
    A payload can hold many messages; every element called fltdMessage (attributes acid, depArpt,
    arrArpt, msgType, sourceTimeStamp) or a flight block with aircraftId/departurePoint/arrivalPoint
    is read, whatever its namespace."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "fltdMessage":
            a = el.attrib
            ev = {"acid": (a.get("acid") or "").upper(), "dep": _apt(a.get("depArpt")), "arr": _apt(a.get("arrArpt")),
                  "type": a.get("msgType"), "at": a.get("sourceTimeStamp"), "igtd": None, "gufi": None,
                  "off": None, "on": None, "eta": None, "diverted": False,
                  "src": a.get("sourceFacility"), "trigger": a.get("fdTrigger"), "status": None, "airline": None}
            for sub in el.iter():
                n = _local(sub.tag)
                txt = (sub.text or "").strip()
                if n == "timeOfDeparture" and txt and sub.get("estimated") == "false":
                    ev["off"] = txt
                elif n == "timeOfArrival" and txt and sub.get("estimated") == "false":
                    ev["on"] = txt
                elif n == "etd" and sub.get("etdType") == "ACTUAL" and not ev["off"]:
                    ev["off"] = sub.get("timeValue")
                elif n == "eta" and sub.get("etaType") in ("ESTIMATED", "ACTUAL"):
                    if sub.get("etaType") == "ACTUAL":
                        ev["on"] = ev["on"] or sub.get("timeValue")
                    else:
                        ev["eta"] = sub.get("timeValue")
                elif n == "flightTimeData" and any(sub.get(a) for a in _AIRLINE_ATTRS.values()):
                    # The airline's own times (CDM). Seen from United: gateDeparture (actual push),
                    # runwayDeparture, airlineInTime (its gate-arrival estimate), originalDeparture/Arrival
                    # (schedule). Only trusted from messages the airline itself sent (src UAL, AAL…).
                    ev["airline"] = {k: sub.get(a) for k, a in _AIRLINE_ATTRS.items() if sub.get(a)}
                elif n in ("newRouteOfFlight", "routeOfFlight") and sub.get("legacyFormat"):
                    ev["route"] = sub.get("legacyFormat")
                elif n == "remarksKeywords" and txt:
                    ev["remarks"] = txt
                elif n == "position" and ev["type"] == "trackInformation":
                    pos = _dms_position(sub)
                    if pos:
                        ev["pos"] = pos
                elif n == "speed" and ev["type"] == "trackInformation" and txt.isdigit():
                    ev["gs"] = int(txt)
                elif n == "simpleAltitude" and ev["type"] == "trackInformation" and txt:
                    m = _re.match(r"(\d+)", txt)
                    if m:
                        ev["alt"] = int(m.group(1)) * 100
                elif n == "flightStatus" and txt:
                    ev["status"] = txt
                elif n == "diversionIndicator" and txt and txt != "NO_DIVERSION":
                    ev["diverted"] = True
                elif n == "igtd" and sub.text and not ev["igtd"]:
                    ev["igtd"] = sub.text.strip()
                elif n == "gufi" and sub.text and not ev["gufi"]:
                    ev["gufi"] = sub.text.strip()
                elif n in ("newArrivalPoint", "arrivalPoint", "arrArpt") and not ev["arr"]:
                    ev["arr"] = _apt("".join(sub.itertext()))
            if ev["acid"]:
                out.append(ev)
        elif name == "flight":
            kids = {_local(c.tag): c for c in el}
            if "aircraftId" in kids and ("departurePoint" in kids or "arrivalPoint" in kids):
                ev = {"acid": (kids["aircraftId"].text or "").strip().upper(),
                      "dep": _apt("".join(kids["departurePoint"].itertext())) if "departurePoint" in kids else None,
                      "arr": _apt("".join(kids["arrivalPoint"].itertext())) if "arrivalPoint" in kids else None,
                      "type": "flight", "at": None,
                      "igtd": (kids["igtd"].text or "").strip() if "igtd" in kids else None,
                      "gufi": (kids["gufi"].text or "").strip() if "gufi" in kids else None}
                if ev["acid"]:
                    out.append(ev)
    return out


def _sample(xml_text):
    types = _TYPE_RE.findall(xml_text[:200000]) or ["unknown"]
    for t in set(types):
        TYPE_COUNTS[t] = TYPE_COUNTS.get(t, 0) + 1
        lst = SAMPLES.setdefault(t, [])
        if len(lst) < 3:
            lst.append(xml_text[:200000])
    for src in set(_SRC_RE.findall(xml_text[:200000])):
        SOURCE_COUNTS[src] = SOURCE_COUNTS.get(src, 0) + 1
        if src == "UAL":
            lst = SAMPLES.setdefault("from_UAL", [])
            if len(lst) < 5:
                lst.append(xml_text[:200000])


def handle(xml_text):
    """Keep the events for watched callsigns."""
    STATE["messages"] += 1
    STATE["last_message"] = time.time()
    try:
        _sample(xml_text)
    except Exception:
        pass
    if not WATCH:
        return
    if not any(cs in xml_text for cs in WATCH):  # cheap test before parsing
        return
    hit = False
    for ev in parse(xml_text):
        if ev["acid"] in WATCH:
            hit = True
            ev["got"] = time.time()
            _keep(ev)
            if ev.get("pos"):
                _live(ev["acid"], "TFMS:" + (ev.get("gufi") or ev.get("dep") or ""), {
                    "lat": ev["pos"][0], "lon": ev["pos"][1], "alt_ft": ev.get("alt"), "gs_kt": ev.get("gs"),
                    "src": "FAA center radar", "max_age": 100})
            if ev.get("trigger") != "UPDATE_DEPARTURE_TIMEOUT":
                _news(ev)
            STATE["matched"] += 1
    if hit:
        MATCHED_RAW.append({"at": time.time(), "xml": xml_text[:200000]})
        del MATCHED_RAW[:-20]


TFDM_STATE = {"status": "off", "error": None, "connected_at": None, "messages": 0, "matched": 0,
              "last_message": None, "rate": 0.0}
TFDM_TYPES = {}
STDDS_STATE = {"status": "off", "error": None, "connected_at": None, "messages": 0, "matched": 0,
               "last_message": None, "rate": 0.0}
STDDS_TYPES = {}
STDDS_RAW = []           # last few surface messages for your airports
WATCH_AIRPORTS = set()   # ICAO codes of your watched flights' airports (KEWR …), set by the server
SURFACE = {}             # airport -> track id -> latest surface position
PENDING = set()          # callsigns with news worth an immediate refresh (takeoff, landing, diversion)
ON_NEWS = None           # set by the server: called when PENDING gets a callsign
_ROOT_RE = _re.compile(r'<(?![?!])(?:[\w.-]+:)?([\w.-]+)')
_DUR_RE = _re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")


def _minutes(iso):
    """ISO duration -> minutes: PT8M -> 8, PT1H2M30S -> 62.5."""
    m = _DUR_RE.fullmatch((iso or "").strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, se = (float(x) if x else 0 for x in m.groups())
    return round(d * 1440 + h * 60 + mi + se / 60, 1)


_TIME_KINDS = ("initial", "estimated", "actual", "earliest", "target", "scheduled", "controlled")


def _tfdm_side(el):
    """A TFDM departure/arrival block -> {name: value}: times as {kind: iso}, runways as designators,
    fixes as names, delays and taxi times in minutes."""
    out = {}
    for c in el:
        n = _local(c.tag)
        kids = list(c)
        if c.get("runwayDesignator"):
            out[n] = c.get("runwayDesignator")
        elif n.endswith("Fix"):
            out[n] = " ".join("".join(k.itertext()).strip() for k in kids) or (c.text or "").strip() or None
        elif n.endswith("Delay") or n.endswith("TaxiTime"):
            out[n] = {_local(k.tag): _minutes(k.text) for k in kids}
        elif n == "predictedDepartureSpot" or c.get("spotRegion"):
            out[n] = c.get("spotRegion") or (c.text or "").strip()
        elif kids and any(_local(k.tag) in _TIME_KINDS or any(_local(g.tag) == "time" for g in k.iter())
                          for k in kids):
            times = {}
            for k in kids:
                t = next((g for g in k.iter() if _local(g.tag) == "time"), None)
                v = (t.text if t is not None else k.text) or ""
                if v.strip():
                    times[_local(k.tag)] = v.strip()
            out[n] = times
        elif not kids and (c.text or "").strip():
            out[n] = c.text.strip()
    return out


def parse_tfdm(xml_text):
    """TFDM (tower) flight messages -> [{acid, dep, arr, igtd, type, at, src: 'TFDM', tfdm: {dep, arr, state}}].
    These are FIXM NasMessage documents; a message can carry several flights."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    mtype = None
    for el in root.iter():
        if _local(el.tag) == "metadata" and el.get("messageType"):
            mtype = el.get("messageType")
    out = []
    for fl in root.iter():
        if _local(fl.tag) != "flight":
            continue
        ident = next((c for c in fl.iter() if _local(c.tag) == "flightIdentification"), None)
        if ident is None or not ident.get("aircraftIdentification"):
            continue
        dep_el = next((c for c in fl if _local(c.tag) == "departure"), None)
        arr_el = next((c for c in fl if _local(c.tag) == "arrival"), None)
        dep = _tfdm_side(dep_el) if dep_el is not None else {}
        arr = _tfdm_side(arr_el) if arr_el is not None else {}
        state = next((c.get("value") for c in fl.iter() if _local(c.tag) == "tfdmFlightState"), None)
        stamps = [(c.text or "").strip() for c in fl.iter() if _local(c.tag) == "reportedTimestamp"]
        creator = next((c.get("locationIndicator") for c in fl.iter() if _local(c.tag) == "tfdmIdCreatorAirport"), None)
        out.append({"acid": ident.get("aircraftIdentification").upper(),
                    "dep": _apt(dep_el.get("departurePointText")) if dep_el is not None else None,
                    "arr": _apt(arr_el.get("destinationPointText")) if arr_el is not None else None,
                    "igtd": (dep.get("offBlockTime") or {}).get("initial") if isinstance(dep.get("offBlockTime"), dict) else None,
                    "type": f"TFDM {mtype or 'message'}", "at": max(stamps) if stamps else None, "src": "TFDM",
                    "tfdm": {"dep": dep, "arr": arr, "state": state, "airport": creator}})
    return out


def _keep(ev):
    with LOCK:
        lst = EVENTS.setdefault(ev["acid"], [])
        lst.append(ev)
        del lst[:-KEEP]


def _news(ev, force=False):
    """Takeoff, landing, a diversion flag or a new destination: worth refreshing the flight now."""
    t = ev.get("tfdm") or {}
    if (force or ev.get("off") or ev.get("on") or ev.get("diverted") or t.get("state")
            or ev.get("type") in ("departureInformation", "arrivalInformation")
            or ((t.get("dep") or {}).get("offBlockTime") or {}).get("actual")
            or ((t.get("dep") or {}).get("runwayDepartureTime") or {}).get("actual")
            or ((t.get("arr") or {}).get("runwayArrivalTime") or {}).get("actual")):
        PENDING.add(ev["acid"])
        if ON_NEWS:
            try:
                ON_NEWS(ev["acid"])
            except Exception:
                pass


def handle_tfdm(xml_text):
    """TFDM (tower data at airports that have it: EWR, LAX, LAS …): runway assignments, predicted
    delays, estimated takeoff and landing times for your flights. Everything is counted and sampled."""
    TFDM_STATE["messages"] += 1
    TFDM_STATE["last_message"] = time.time()
    if not xml_text.strip():
        return  # keep-alive (a single space)
    head = xml_text[:200000]
    try:
        types = _TYPE_RE.findall(head[:3000]) or _ROOT_RE.findall(head[:400])[:1] or ["unknown"]
        for t in set(types):
            TFDM_TYPES[t] = TFDM_TYPES.get(t, 0) + 1
            lst = SAMPLES.setdefault("TFDM_" + t, [])
            if len(lst) < 3:
                lst.append(head)
    except Exception:
        pass
    if not WATCH or not any(cs in xml_text for cs in WATCH):
        return
    hit = False
    for ev in parse_tfdm(xml_text):
        if ev["acid"] in WATCH:
            hit = True
            ev["got"] = time.time()
            _keep(ev)
            _news(ev)
    if hit:
        TFDM_STATE["matched"] += 1
        MATCHED_RAW.append({"at": time.time(), "xml": head, "feed": "TFDM"})
        del MATCHED_RAW[:-40]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_surface(xml_text):
    """STDDS surface movement (ASDE-X / ASSC) -> (airport, [{track, lat, lon, hdg, kt, cs, type, kind, full}]).
    Delta reports carry only what changed; the caller merges them by track id."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, []
    airport = None
    for el in root.iter():
        if _local(el.tag) == "airport" and (el.text or "").strip():
            airport = el.text.strip().upper()
            break
    out = []
    for rep in root.iter():
        if _local(rep.tag) != "positionReport":
            continue
        r = {"full": rep.get("full") == "true"}
        for c in rep.iter():
            n = _local(c.tag)
            v = (c.text or "").strip()
            if not v:
                continue
            if n == "track":
                r["track"] = v
            elif n == "latitude":
                r["lat"] = _num(v)
            elif n == "longitude":
                r["lon"] = _num(v)
            elif n == "heading":
                r["hdg"] = _num(v)
            elif n == "speed":
                r["kt"] = _num(v)
            elif n == "aircraftId":
                r["cs"] = v.upper()
            elif n == "acType":
                r["type"] = v
            elif n == "tgtType":
                r["kind"] = v.lower()
            elif n == "mode3ACode":
                r["squawk"] = v
            elif n == "altitude":
                r["alt"] = _num(v)
            elif n == "acAddress":
                r["hex"] = v.lower()
        if r.get("track"):
            out.append(r)
    return airport, out


def handle_stdds(xml_text):
    """STDDS: only surface positions at your watched flights' airports are kept (latest per target);
    everything else is counted and sampled, then dropped."""
    STDDS_STATE["messages"] += 1
    STDDS_STATE["last_message"] = time.time()
    try:
        t = (_ROOT_RE.findall(xml_text[:400]) or ["unknown"])[0]
        STDDS_TYPES[t] = STDDS_TYPES.get(t, 0) + 1
        lst = SAMPLES.setdefault("STDDS_" + t, [])
        if len(lst) < 3:
            lst.append(xml_text[:200000])
    except Exception:
        pass
    root = (_ROOT_RE.findall(xml_text[:400]) or [""])[0]
    if WATCH and root in ("TATrackAndFlightPlan", "SurfaceMovementEventMessage", "TowerDepartureEventMessage") \
            and any(cs in xml_text for cs in WATCH):
        try:
            _stdds_flight(root, xml_text)
        except Exception as e:
            log.debug("STDDS %s: %s", root, e)
        return
    if not WATCH_AIRPORTS or not any(ap in xml_text for ap in WATCH_AIRPORTS):
        return
    airport, reps = parse_surface(xml_text)
    if not airport or airport not in WATCH_AIRPORTS or not reps:
        return
    now = time.time()
    with LOCK:
        tracks = SURFACE.setdefault(airport, {})
        for r in reps:
            cur = {} if r.get("full") else tracks.get(r["track"], {})
            cur = {**cur, **{k: v for k, v in r.items() if v is not None and k != "full"}, "t": now}
            tracks[r["track"]] = cur
        for k in [k for k, v in tracks.items() if now - v["t"] > 30]:
            del tracks[k]
        for r in reps:
            cur = tracks[r["track"]]
            if cur.get("cs") in WATCH:
                _live(cur["cs"], f"ASDE:{airport}:{r['track']}", {
                    "lat": cur.get("lat"), "lon": cur.get("lon"), "alt_ft": cur.get("alt"), "gs_kt": cur.get("kt"),
                    "heading": cur.get("hdg"), "hex": cur.get("hex"), "type": cur.get("type"),
                    "ground": True, "src": f"{airport} surface radar", "max_age": 20}, lock=False)
    STDDS_STATE["matched"] += 1
    if any(cs in xml_text for cs in WATCH):
        STDDS_RAW.append(xml_text[:200000])
        del STDDS_RAW[:-10]


def _txt(el, name):
    c = next((x for x in el.iter() if _local(x.tag) == name), None)
    return (c.text or "").strip() if c is not None and c.text else None


def _stdds_flight(kind, xml_text):
    """STDDS messages about your flights: approach radar positions (with the transponder address,
    so the N-number), surface events (landed, runway exit, ramp in/out, takeoff) and the tower's
    departure clearance."""
    root = ET.fromstring(xml_text)
    now = time.time()
    if kind == "TATrackAndFlightPlan":
        src = _txt(root, "src") or "approach"
        for rec in root.iter():
            if _local(rec.tag) != "record":
                continue
            acid = (_txt(rec, "acid") or "").upper()
            if acid not in WATCH:
                continue
            lat, lon = _num(_txt(rec, "lat")), _num(_txt(rec, "lon"))
            if lat is None or lon is None:
                continue
            vx, vy = _num(_txt(rec, "vx")) or 0, _num(_txt(rec, "vy")) or 0
            import math
            hexa = (_txt(rec, "acAddress") or "").lower() or None
            _live(acid, f"TAIS:{src}:{_txt(rec, 'trackNum')}", {
                "lat": lat, "lon": lon, "alt_ft": _num(_txt(rec, "reportedAltitude")),
                "gs_kt": round(math.hypot(vx, vy)) if (vx or vy) else None,
                "heading": round(math.degrees(math.atan2(vx, vy)) % 360) if (vx or vy) else None,
                "hex": hexa, "type": _txt(rec, "acType"), "squawk": _txt(rec, "reportedBeaconCode"),
                "src": f"{src} approach radar", "max_age": 20})
            STDDS_STATE["matched"] += 1
        return
    acid = (_txt(root, "callsign") or _txt(root, "aircraftID") or "").upper()
    if acid not in WATCH:
        return
    ev = {"acid": acid, "src": "STDDS", "got": now, "type": kind,
          "dep": _apt(_txt(root, "departureAirport")), "arr": _apt(_txt(root, "destinationAirport")), "igtd": None}
    if kind == "SurfaceMovementEventMessage":
        ev["surface"] = {
            "airport": _apt(_txt(root, "airport")), "status": _txt(root, "status"), "hex": (_txt(root, "acAddress") or "").lower() or None,
            "events": [{"event": _txt(r, "event"), "runway": _txt(r, "runway"), "at": _txt(r, "at")}
                       for r in root.iter() if _local(r.tag) == "eventRecord"]
                      + [{"event": _txt(root, "event"), "runway": None, "at": _txt(root, "time")}]}
    else:
        ev["tower"] = {"airport": _apt(_txt(root, "departureAirport")), "clearance": _txt(root, "clearanceDeliveryTime"),
                       "gate": _txt(root, "parkingGate"), "at": _txt(root, "eventTime")}
    _keep(ev)
    if any(e.get("event") in ("on", "off") for e in (ev.get("surface") or {}).get("events", [])):
        _news(ev, force=True)
    STDDS_STATE["matched"] += 1
    MATCHED_RAW.append({"at": now, "xml": xml_text[:200000], "feed": "STDDS"})
    del MATCHED_RAW[:-40]


LIVE = {}   # callsign -> source key -> latest position from FAA radar (center, approach, surface)


def _live(acid, key, rec, lock=True):
    rec = {k: v for k, v in rec.items() if v is not None}
    if rec.get("lat") is None or rec.get("lon") is None:
        return
    rec["at"] = time.time()
    if rec.get("hex"):
        rec["tail"] = n_number(rec["hex"])
    if lock:
        with LOCK:
            LIVE.setdefault(acid, {})[key] = rec
    else:
        LIVE.setdefault(acid, {})[key] = rec


def live_all(callsign):
    """Fresh FAA radar positions for this callsign, in adsb.lol's shape ({lat, lon, alt_ft, gs_kt,
    heading, ground, tail, hex, at, src}). Several sources can see one airplane: the freshest per
    airplane (by transponder address, else within 3 nm) is kept."""
    now = time.time()
    with LOCK:
        recs = [dict(r) for r in (LIVE.get((callsign or "").upper()) or {}).values()
                if now - r["at"] <= r.get("max_age", 30)]
    recs.sort(key=lambda r: (0 if r.get("hex") else 1, -r["at"]))
    out = []
    for r in recs:
        dup = next((o for o in out if (r.get("hex") and o.get("hex") == r["hex"]) or
                    (abs(o["lat"] - r["lat"]) < 0.05 and abs(o["lon"] - r["lon"]) < 0.06)), None)
        if dup:
            continue
        r.setdefault("ground", False)
        r.pop("max_age", None)
        out.append(r)
    return out


def surface(airport, max_age=15):
    """Targets moving or parked on this airport's surface right now (positions newer than max_age s)."""
    now = time.time()
    with LOCK:
        return [dict(v) for v in (SURFACE.get((airport or "").upper()) or {}).values()
                if now - v["t"] <= max_age and v.get("lat") is not None and v.get("lon") is not None]


def events(callsign):
    with LOCK:
        return list(EVENTS.get((callsign or "").upper(), []))


def configured(cfg):
    return all((cfg.get(k) or "").strip() for k in ("swim_username", "swim_password", "swim_queue"))


def _feeds(cfg):
    """The subscriptions to read: TFMS (flight data) and, when their queues are set, TFDM (tower data)
    and STDDS (surface movement). Each uses the TFMS username/password unless given its own."""
    out = []
    if configured(cfg):
        out.append({"name": "TFMS", "state": STATE, "handle": handle, "queue": cfg["swim_queue"],
                    "host": cfg.get("swim_host") or "tcps://ems1.swim.faa.gov:55443",
                    "vpn": cfg.get("swim_vpn") or "TFMS", "user": cfg["swim_username"], "password": cfg["swim_password"]})
    if (cfg.get("swim_tfdm_queue") or "").strip():
        user = cfg.get("swim_tfdm_username") or cfg.get("swim_username")
        pw = cfg.get("swim_tfdm_password") or cfg.get("swim_password")
        if user and pw:
            out.append({"name": "TFDM", "state": TFDM_STATE, "handle": handle_tfdm, "queue": cfg["swim_tfdm_queue"],
                        "host": cfg.get("swim_tfdm_host") or "tcps://ems3.swim.faa.gov:55443",
                        "vpn": cfg.get("swim_tfdm_vpn") or "TFDM", "user": user, "password": pw})
    if (cfg.get("swim_stdds_queue") or "").strip():
        user = cfg.get("swim_stdds_username") or cfg.get("swim_username")
        pw = cfg.get("swim_stdds_password") or cfg.get("swim_password")
        if user and pw:
            out.append({"name": "STDDS", "state": STDDS_STATE, "handle": handle_stdds, "queue": cfg["swim_stdds_queue"],
                        "host": cfg.get("swim_stdds_host") or "tcps://ems2.swim.faa.gov:55443",
                        "vpn": cfg.get("swim_stdds_vpn") or "STDDS", "user": user, "password": pw})
    return out


_threads = {}


def start(cfg):
    global _cfg
    _cfg = cfg
    feeds = _feeds(cfg)
    if not any(f["name"] == "TFMS" for f in feeds):
        STATE.update(status="off")
    for feed in feeds:
        t = _threads.get(feed["name"])
        if t and t.is_alive():
            continue
        t = threading.Thread(target=_run, args=(feed,), daemon=True, name="swim-" + feed["name"].lower())
        _threads[feed["name"]] = t
        t.start()


def _run(feed):
    backoff = 10
    st = feed["state"]
    while True:
        try:
            _session(feed)
            backoff = 10
        except Exception as e:
            st.update(status="error", error=str(e).splitlines()[0][:200])
            log.warning("SWIM %s: %s; reconnecting in %d s", feed["name"], st["error"], backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 600)


def _cert_dir(host):
    """The trust folder for the Solace library (OpenSSL's hashed-directory form): every root in
    Debian's store, plus the intermediate certificate(s) the FAA server points to but may not send
    (fetched from the 'CA Issuers' address in its certificate), rehashed with openssl."""
    import os
    import shutil
    import ssl
    import subprocess
    import urllib.request
    d = "/data/solace-ca" if os.path.isdir("/data") else "/tmp/solace-ca"
    try:
        os.makedirs(d, exist_ok=True)
        for n in os.listdir("/etc/ssl/certs"):
            if n.endswith(".pem") and not os.path.exists(os.path.join(d, n)):
                shutil.copy(os.path.join("/etc/ssl/certs", n), os.path.join(d, n))
        h, _, port = host.split("://")[-1].partition(":")
        url = None
        try:
            leaf = ssl.get_server_certificate((h, int(port or 55443)), timeout=15)
            txt = subprocess.run(["openssl", "x509", "-noout", "-text"], input=leaf, capture_output=True,
                                 text=True, timeout=15).stdout
            for line in txt.splitlines():
                if "CA Issuers - URI:" in line:
                    url = line.split("URI:", 1)[1].strip()
        except Exception as e:
            log.info("SWIM: couldn't read the server certificate: %s", e)
        if url:
            name = "swim-issuer-" + os.path.basename(url).replace("/", "_") + ".pem"
            path = os.path.join(d, name)
            if not os.path.exists(path):
                raw = urllib.request.urlopen(url, timeout=20).read()
                pem = raw.decode() if raw.startswith(b"-----BEGIN") else subprocess.run(
                    ["openssl", "x509", "-inform", "DER", "-outform", "PEM"], input=raw, capture_output=True,
                    timeout=15).stdout.decode()
                if "BEGIN CERTIFICATE" in pem:
                    with open(path, "w") as f:
                        f.write(pem)
                    log.info("SWIM: added the server's issuer certificate from %s", url)
        subprocess.run(["openssl", "rehash", d], capture_output=True, timeout=60)
        return d
    except Exception as e:
        log.info("SWIM: trust folder: %s; using the system store", e)
        return "/etc/ssl/certs"


def _session(feed):
    """One connection: bind to the feed's queue and read until it drops."""
    from solace.messaging.messaging_service import MessagingService
    from solace.messaging.config.transport_security_strategy import TLS
    from solace.messaging.config.retry_strategy import RetryStrategy
    from solace.messaging.resources.queue import Queue
    from solace.messaging.receiver.message_receiver import MessageHandler

    # Set up the way the FAA's own Python JumpStart kit does it.
    host = feed["host"].strip()
    if _cfg.get("swim_skip_cert_check"):
        tls = TLS.create().without_certificate_validation()
    else:
        # SCDS presents a Let's Encrypt certificate; Debian's store has its root (ISRG Root X1)
        # in the hashed-directory form the Solace library expects.
        tls = TLS.create().with_certificate_validation(True, False, _cert_dir(host))
    # The first connection gives up after a few tries so its error is shown (forever would keep
    # retrying silently); once connected, reconnecting after a drop never gives up.
    first = RetryStrategy.parametrized_retry(3, 5000)
    retry = RetryStrategy.forever_retry(retry_interval=5000)
    STATE_ = feed["state"]
    STATE_.update(status="connecting", error=None)
    svc = (MessagingService.builder()
           .from_properties({
               "solace.messaging.transport.host": host,
               "solace.messaging.service.vpn-name": feed["vpn"].strip(),
               "solace.messaging.authentication.scheme.basic.username": feed["user"].strip(),
               "solace.messaging.authentication.scheme.basic.password": feed["password"].strip(),
           })
           .with_connection_retry_strategy(first)
           .with_reconnection_retry_strategy(retry)
           .with_transport_security_strategy(tls)
           .build())
    STATE_["attempt_at"] = time.time()
    try:
        svc.connect()
    except Exception as e:
        msg = str(e).replace("\n", " ")[:300]
        hint = ""
        low = msg.lower()
        if "auth" in low or "login" in low or "401" in low or "unauthor" in low:
            hint = " (check the SWIM username and the subscription's Connection Password)"
        elif "cert" in low or "ssl" in low or "tls" in low or "handshake" in low:
            hint = " (certificate problem: try 'Skip SWIM certificate check' in the Configuration)"
        elif "timed out" in low or "unreachable" in low or "refused" in low or "resolve" in low:
            hint = " (can't reach the FAA server from your network on port 55443)"
        raise RuntimeError(msg + hint)
    dropped = threading.Event()
    try:
        svc.add_service_interruption_listener(lambda *_: dropped.set())
    except Exception:
        pass
    try:
        receiver = svc.create_persistent_message_receiver_builder().build(
            Queue.durable_exclusive_queue(feed["queue"].strip()))
        receiver.start()
    except Exception as e:
        try:
            svc.disconnect()
        except Exception:
            pass
        raise RuntimeError(f"connected, but the queue didn't open: {str(e)[:250]} (check the {feed['name']} queue name and VPN)")

    class H(MessageHandler):
        def on_message(self, msg):
            try:
                feed["handle"](_body(msg))
            except Exception as e:
                log.debug("SWIM message: %s", e)
            finally:
                try:
                    receiver.ack(msg)
                except Exception:
                    pass
    receiver.receive_async(H())
    STATE_.update(status="connected", connected_at=time.time(), error=None)
    log.info("SWIM %s connected to %s, queue %s", feed["name"], host, feed["queue"])
    last_n, last_t = STATE_["messages"], time.time()
    try:
        while not dropped.is_set() and svc.is_connected:
            time.sleep(30)
            n, t = STATE_["messages"], time.time()
            STATE_["rate"] = round((n - last_n) / max(1, t - last_t) * 60, 1)  # per minute
            last_n, last_t = n, t
    finally:
        try:
            receiver.terminate()
        except Exception:
            pass
        try:
            svc.disconnect()
        except Exception:
            pass
    raise RuntimeError("connection dropped")


def _body(msg):
    """A message's text. Some arrive as bytes (sometimes gzip) with a near-empty text part, so take
    whichever is the real payload."""
    try:
        body = msg.get_payload_as_string()
    except Exception:
        body = None
    if body is None or len(body.strip()) < 16:
        try:
            raw = msg.get_payload_as_bytes() or b""
        except Exception:
            raw = b""
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        if len(raw) > len(body or ""):
            body = raw.decode("utf-8", "replace")
    return body or ""


def samples_zip():
    """Examples of each message type seen, the last messages for your flights, and counts."""
    import io
    import json
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.json", json.dumps({"state": STATE, "watching": sorted(WATCH), "types": TYPE_COUNTS, "airline_sent": SOURCE_COUNTS,
                                               "tfdm": {"state": TFDM_STATE, "types": TFDM_TYPES},
                                               "stdds": {"state": STDDS_STATE, "types": STDDS_TYPES,
                                                         "airports": sorted(WATCH_AIRPORTS),
                                                         "targets": {k: len(v) for k, v in SURFACE.items()}},
                                               "events": {k: v for k, v in EVENTS.items()}}, indent=1, default=str))
        for t, lst in SAMPLES.items():
            for i, x in enumerate(lst, 1):
                z.writestr(f"samples/{_re.sub(r'[^A-Za-z0-9_]+', '_', t)}_{i}.xml", x)
        for i, m in enumerate(MATCHED_RAW, 1):
            z.writestr(f"your_flights/{i:02d}{'_' + m['feed'] if m.get('feed') else ''}.xml", m["xml"])
        for i, x in enumerate(STDDS_RAW, 1):
            z.writestr(f"your_flights/surface_{i:02d}.xml", x)
    return buf.getvalue()
