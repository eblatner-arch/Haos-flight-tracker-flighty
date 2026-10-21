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

log = logging.getLogger("flight_tracker_flighty")

STATE = {"status": "off", "error": None, "connected_at": None, "messages": 0, "matched": 0,
         "last_message": None, "rate": 0.0}
WATCH = set()        # callsigns to keep (UAL2608 …), set by the server each cycle
EVENTS = {}          # callsign -> [event, …] newest last
LOCK = threading.Lock()
KEEP = 40            # events per callsign
SAMPLES = {}         # message type -> a few raw messages, to check the parser against the real feed
MATCHED_RAW = []     # the last raw messages that matched your flights
PDC_RAW = []         # every datalink clearance message that mentions one of your flights (kept apart)
YOURS_BY_TYPE = {}   # message type -> the last few raw messages that mention one of your flights
# callsign -> the last messages that mention it and carry anything about a controlled time (an EDCT, a
# slot): what the FAA sent around an EDCT change. Kept on disk with YOURS_BY_TYPE, so an add-on
# update or restart doesn't wipe them before the samples are downloaded.
EDCT_TRAIL = {}
# airport -> the last ground delay program / ground stop messages for the airports your flights touch
# (every revision, cancel and purge, raw), so an EDCT that's removed can be lined up with what the
# program did at that minute. Kept on disk with the trail.
PROGRAM_TRAIL = {}
YOURS_PATH = None
_yours_dirty = [False]
_yours_saved = [0.0]
TYPE_COUNTS = {}
import re as _re
_TYPE_RE = _re.compile(r'msgType="([^"]+)"')
_SRC_RE = _re.compile(r'sourceFacility="([A-Z0-9]{2,3})"')  # airline-sent (CDM) messages: AAL, SKW, UAL…
_EDCTISH_RE = _re.compile(r"controlled|edct|slotTime|assignedArrivalSlot|<(?:\w+:)?ctd\b", _re.I)
SOURCE_COUNTS = {}
# EDCTs for every flight, not only the ones being watched: the FAA assigns them once, when a ground
# delay program is issued (often hours before you'd start watching), and doesn't send them again.
# Small (callsign -> a few slots), kept on disk so a restart doesn't lose them.
EDCTS = {}
EDCT_PATH = None
_edct_dirty = [False]
_edct_saved = [0.0]   # written at most every 2 min
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
            traj, fixes = [], {}
            for sub in el.iter():
                n = _local(sub.tag)
                txt = (sub.text or "").strip()
                if n == "timeOfDeparture" and txt and sub.get("estimated") == "false":
                    ev["off"] = txt
                elif n == "timeOfArrival" and txt and sub.get("estimated") == "false":
                    ev["on"] = txt
                elif n == "etd" and sub.get("etdType") == "ACTUAL" and not ev["off"]:
                    ev["off"] = sub.get("timeValue")
                elif n == "etd" and sub.get("etdType") == "CONTROLLED" and sub.get("timeValue"):
                    ev["edct"] = sub.get("timeValue")
                elif n == "etd" and sub.get("etdType") == "PROPOSED" and sub.get("timeValue"):
                    # the flight plan's departure time: airlines refile it to the EDCT they hold (a slot
                    # swap's new time shows here even when the swap itself isn't shared)
                    ev["etd_proposed"] = sub.get("timeValue")
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
                elif n == "waypoint" and sub.get("latitudeDecimal"):
                    # the FAA's computed trajectory along the cleared route (FlightSectors)
                    try:
                        traj.append((int(sub.get("sequenceNumber") or 0), round(float(sub.get("latitudeDecimal")), 4),
                                     round(float(sub.get("longitudeDecimal")), 4), int(sub.get("elapsedTime") or 0)))
                    except ValueError:
                        pass
                elif n == "fix" and txt and ev["type"] == "FlightSectors":
                    fixes[int(sub.get("elapsedTime") or 0)] = txt
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
            if len(traj) > 1:
                traj.sort()
                ev["traj"] = [[la, lo, t] for _, la, lo, t in traj]
                ev["traj_fixes"] = [[la, lo, fixes[t]] for _, la, lo, t in traj if t in fixes]
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


def _ddhhmm(v, ref):
    """'171839' (day 17, 18:39 UTC, as the FADT slot list writes times) -> ISO, in the month of ref
    (the message's own timestamp), or the month next to it when the day says so."""
    from datetime import datetime, timedelta, timezone as tz
    if not v or not _re.fullmatch(r"\d{6}", v):
        return v if v and "T" in v else None
    try:
        r = datetime.fromisoformat((ref or "").replace("Z", "+00:00")[:25]) if ref else datetime.now(tz.utc)
    except ValueError:
        r = datetime.now(tz.utc)
    d, h, m = int(v[:2]), int(v[2:4]), int(v[4:])
    best = None
    for mo in (-1, 0, 1):
        y, mm = r.year + (r.month - 1 + mo) // 12, (r.month - 1 + mo) % 12 + 1
        try:
            c = datetime(y, mm, d, h, m, tzinfo=tz.utc)
        except ValueError:
            continue
        if best is None or abs(c - r) < abs(best - r):
            best = c
    return best.strftime("%Y-%m-%dT%H:%M:%SZ") if best else None


def parse_edct(xml_text):
    """EDCTs: a flight's controlled departure (and arrival) time from a ground delay program or
    ground stop, in TFMS slot assignments and program flight lists ->
    [{acid, dep, arr, igtd, type: 'EDCT', edct, cta, element, exempt}]."""
    root = ET.fromstring(xml_text)
    out = []
    ref = (_re.search(r'sourceTimeStamp="([^"]+)"', xml_text[:3000]) or [None, None])[1]
    for el in root.iter():
        if _local(el.tag) == "slot":
            # the FADT: a delay program's whole slot list, re-sent with every revision. Times are
            # DDHHMM; the airport's departures that are exempt or cancelled have no EDCT.
            k = {_local(c.tag): (c.text or "").strip() for c in el.iter() if (c.text or "").strip()}
            acid = (k.get("aircraftId") or "").upper()
            if not acid or not k.get("controlledDepartureTime") or k.get("exemptFlag") == "true" or k.get("cancelFlag") == "true":
                continue
            out.append({"acid": acid, "type": "EDCT", "src": "TFMS", "dep": _apt(k.get("departureAirport")),
                        "arr": _apt(k.get("arrivalAirport")), "igtd": _ddhhmm(k.get("initialGateDepartureTime"), ref),
                        "edct": _ddhhmm(k["controlledDepartureTime"], ref), "cta": _ddhhmm(k.get("controlledArrivalTime"), ref),
                        "element": k.get("controlledElement") or k.get("arrivalAirport"), "exempt": False})
            continue
        if _local(el.tag) != "flight":
            continue
        kids = {}
        for c in el.iter():
            n = _local(c.tag)
            if n not in kids and (c.text or "").strip():
                kids[n] = c.text.strip()
        if not kids.get("controlledDepartureTime"):
            continue
        acid = (kids.get("callSign") or kids.get("aircraftId") or "").upper()
        if not acid:
            continue
        out.append({"acid": acid, "type": "EDCT", "src": "TFMS",
                    "dep": _apt(kids.get("departureAirport") or kids.get("airport")),
                    "arr": _apt(kids.get("arrivalAirport")),
                    "igtd": kids.get("igtd") or kids.get("initialGateDepartureTime"),
                    "edct": kids["controlledDepartureTime"], "cta": kids.get("controlledArrivalTime"),
                    "element": kids.get("controlledElement") or kids.get("elementName"),
                    "exempt": kids.get("exemptFlag") not in (None, "-", "N", "false")})
    seen = {(e["acid"], _ddhhmm(e["edct"], ref)) for e in out}
    out.extend(e for e in _edct_any_shape(root, ref) if (e["acid"], e["edct"]) not in seen)
    return out


_ACID_TAGS = ("aircraftId", "callSign", "acid", "flightId", "aircraftIdentification")


def _edct_any_shape(root, ref):
    """A controlled departure time in a container the shapes above don't know (a slot substitution or
    compression list, a per-flight TMI update): the flight is the nearest enclosing element that names
    exactly one callsign, and its airports, times and controlled element are read from inside it."""
    parent = {c: p for p in root.iter() for c in p}
    out = []
    for cdt in root.iter():
        if _local(cdt.tag) != "controlledDepartureTime" or not (cdt.text or "").strip():
            continue
        box = parent.get(cdt)
        while box is not None:
            acids = {(c.text or "").strip().upper() for c in box.iter() if _local(c.tag) in _ACID_TAGS and (c.text or "").strip()}
            acids |= {c.get("acid").upper() for c in box.iter() if c.get("acid")}
            if acids:
                break
            box = parent.get(box)
        if box is None or len(acids) != 1:
            continue   # no callsign, or a list of several flights: can't tell whose time it is
        k = {}
        for c in box.iter():
            n = _local(c.tag)
            if n not in k and (c.text or "").strip():
                k[n] = c.text.strip()
        k["controlledDepartureTime"] = cdt.text.strip()
        if k.get("exemptFlag") == "true" or k.get("cancelFlag") == "true":
            continue
        element = k.get("controlledElement") or k.get("elementName") or k.get("controlElement")
        up = parent.get(box)
        while not element and up is not None:   # the program's element is often named once, above its flights
            element = next(((c.text or "").strip() for c in up if _local(c.tag) in ("controlledElement", "elementName", "controlElement")
                            and (c.text or "").strip()), None)
            up = parent.get(up)
        k["controlledElement"] = element
        out.append({"acid": acids.pop(), "type": "EDCT", "src": "TFMS",
                    "dep": _apt(k.get("departureAirport") or k.get("departurePoint") or k.get("airport")),
                    "arr": _apt(k.get("arrivalAirport") or k.get("arrivalPoint")),
                    "igtd": _ddhhmm(k.get("igtd") or k.get("initialGateDepartureTime"), ref),
                    "edct": _ddhhmm(k["controlledDepartureTime"], ref), "cta": _ddhhmm(k.get("controlledArrivalTime"), ref),
                    "element": k.get("controlledElement") or k.get("elementName") or k.get("controlElement"),
                    "exempt": False})
    return out


# Ground stops and ground delay programs as the FAA issues them (TFMS flow messages), for every
# airport: (airport, kind) -> NAS item in the same shape as the NAS status page's. A cancel removes
# it and is remembered for a while, so the slower NAS page can't bring it back.
NAS = {}
NAS_CANCELLED = {}
ON_NAS = None


def _flow(xml_text):
    root = ET.fromstring(xml_text)
    for msg in root.iter():
        if _local(msg.tag) != "fiMessage" or msg.get("msgType") not in ("GS", "GDP", "AFP"):
            continue
        kind = {"GS": "gs", "GDP": "gdp", "AFP": "gdp"}[msg.get("msgType")]
        for body in msg:
            name = _local(body.tag)
            f = {}
            for c in body.iter():
                n = _local(c.tag)
                if n not in f and (c.text or "").strip() and not list(c):
                    f[n] = c.text.strip()
            arpt = (f.get("airportId") or f.get("ctlElement") or "").upper()
            if not arpt or (f.get("elementType") and f["elementType"] != "APT"):
                continue
            status = (f.get("tmiStatus") or "ACTUAL").upper()
            k = (arpt, kind)
            if "cancel" in name.lower() or status in ("PURGED", "CANCELLED", "CANCELED", "EXPIRED"):
                NAS.pop(k, None)
                NAS_CANCELLED[k] = time.time()
                if kind == "gdp":
                    # the program's EDCTs no longer apply ("DISREGARD EDCTS FOR DEST AUS")
                    with LOCK:
                        for acid in list(EDCTS):
                            EDCTS[acid] = [e for e in EDCTS[acid] if _apt(e.get("element") or "") != _apt(arpt)]
                            if not EDCTS[acid]:
                                del EDCTS[acid]
                    _edct_dirty[0] = True
                _nas_changed(arpt)
                continue
            if status != "ACTUAL":
                continue   # proposed programs aren't in effect yet
            def period(*names):
                for pn in names:
                    el = next((c for c in body.iter() if _local(c.tag) == pn), None)
                    if el is not None:
                        st = next((c.text for c in el.iter() if _local(c.tag) == "startTime"), None)
                        en = next((c.text for c in el.iter() if _local(c.tag) == "endTime"), None)
                        if st or en:
                            return st, en
                return None, None
            st, en = period("gsPeriod", "groundStopPeriod", "advisoryValidPeriod", "cumulativeProgramPeriod")
            scope_el = next((c for c in body.iter() if _local(c.tag) == "departureFacilitiesIncludedList"), None) \
                or next((c for c in body.iter() if _local(c.tag) == "departureScope"), None)
            scope = [(c.text or "").strip() for c in scope_el.iter() if _local(c.tag) == "facilityId" and (c.text or "").strip()] \
                if scope_el is not None else []
            def delays(group):
                el = next((c for c in body.iter() if _local(c.tag) == group), None)
                if el is None:
                    return None, None
                g = {_local(c.tag): (c.text or "").strip() for c in el.iter()}
                return g.get("avgDelay"), g.get("maxDelay")
            # ground stops report delays so far and those expected; delay programs one set
            avg, mx = delays("currentDelays")
            ant_avg, ant_max = delays("anticipatedDelays")
            if not avg and not mx:
                avg, mx = f.get("avgDelay"), f.get("maxDelay")
            det = ", ".join(x for x in (f"avg {round(float(avg))} min" if avg else "", f"max {mx} min" if mx else "",
                                        f"expected avg {round(float(ant_avg))} min" if ant_avg else "") if x)
            NAS[k] = {"airport": arpt, "kind": kind, "label": "Ground stop" if kind == "gs" else "Ground delay program",
                      "reason": (f.get("impactingCondition") or "").title(), "detail": det,
                      "start": _iso_epoch(st), "end": _iso_epoch(en),
                      "prob": (f.get("probabilityOfExtension") or "").title(), "center": f.get("center") or "",
                      "scope": scope, "avg": avg, "max": mx, "source": "FAA SWIM", "at": time.time(),
                      "key": f"{arpt}|{kind}|swim|{st or ''}"}
            NAS_CANCELLED.pop(k, None)
            _nas_changed(arpt)


def _iso_epoch(iso):
    if not iso:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _nas_changed(arpt):
    if ON_NAS:
        try:
            ON_NAS(arpt)
        except Exception:
            pass


def _around(xml_text, cs, cap=250000):
    """The message as is when it fits; else its head and ~8 KB around each mention of the callsign
    (a delay program's flight list runs past a megabyte, and the flight's own record is in the middle)."""
    if len(xml_text) <= cap:
        return xml_text
    parts, last = [xml_text[:20000]], 20000
    for m in _re.finditer(_re.escape(cs) + r"\b", xml_text):
        a, b = max(last, m.start() - 4000), m.end() + 4000
        if a < b:
            parts.append(f"\n<!-- … {a - last} characters left out … -->\n" + xml_text[a:b])
            last = b
        if sum(len(x) for x in parts) > cap:
            break
    return "".join(parts)[:cap]


def handle(xml_text):
    """Keep the events for watched callsigns."""
    STATE["messages"] += 1
    STATE["last_message"] = time.time()
    try:
        _sample(xml_text)
    except Exception:
        pass
    head = xml_text[:4000]
    if WATCH:
        hit = next((cs for cs in WATCH if cs in xml_text), None)
        if hit:
            # what the FAA sends about your flights, every kind of message, for checking (samples download)
            t = (_TYPE_RE.findall(head) or ["unknown"])[0]
            edctish = _EDCTISH_RE.search(xml_text)
            with LOCK:
                lst = YOURS_BY_TYPE.setdefault(t, [])
                lst.append(xml_text[:150000])
                del lst[:-4]
                if edctish:
                    for cs in [c for c in WATCH if c in xml_text]:
                        trail = EDCT_TRAIL.setdefault(cs, [])
                        trail.append({"at": time.time(), "type": t, "xml": _around(xml_text, cs)})
                        del trail[:-10]
            _yours_dirty[0] = True
    if "controlledDepartureTime" in xml_text or 'etdType="CONTROLLED"' in xml_text:
        _edct_sample(xml_text)
        try:
            for ev in parse_edct(xml_text):
                ev["got"] = time.time()
                _remember_edct(ev)
                if ev["acid"] in WATCH:
                    _news(ev, force=True)
                    STATE["matched"] += 1
        except Exception as e:
            log.debug("SWIM EDCT: %s", e)
    elif "CONTROLLED" in xml_text or "EDCT" in xml_text:
        _edct_sample(xml_text)
    if "fiMessage" in head and ('msgType="GS"' in head or 'msgType="GDP"' in head or 'msgType="AFP"' in head):
        try:
            _program_keep(xml_text)
        except Exception as e:
            log.debug("SWIM program trail: %s", e)
        try:
            _flow(xml_text)
        except Exception as e:
            log.debug("SWIM flow message: %s", e)
        return
    if 'etdType="CONTROLLED"' in xml_text:
        try:
            for ev in parse(xml_text):
                if ev.get("edct"):
                    _remember_edct({"acid": ev["acid"], "type": "EDCT", "src": "TFMS", "dep": ev.get("dep"),
                                    "arr": ev.get("arr"), "igtd": ev.get("igtd"), "edct": ev["edct"], "got": time.time()})
        except Exception as e:
            log.debug("SWIM EDCT: %s", e)
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
TBFM_STATE = {"status": "off", "error": None, "connected_at": None, "messages": 0, "matched": 0,
              "last_message": None, "rate": 0.0}
TBFM_TYPES = {}
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


def _pdc_sample(xml_text):
    """One datalink clearance per tower and airline (they come in several formats), up to 40."""
    ap = _re.search(r"<airportID>([A-Z0-9]+)", xml_text[:2000])
    hd = _re.search(r"<dataHeader>\S+ (\S+)", xml_text[:3000])
    key = f"{ap.group(1) if ap else '?'} {hd.group(1)[:7] if hd else '?'}"
    seen = SAMPLES.setdefault("pdc_by_tower", [])
    if len(seen) < 40 and not any(k == key for k, _ in seen):
        seen.append((key, xml_text[:20000]))


def _edct_sample(xml_text):
    """Every kind of message that carries an EDCT, a few of each, for checking the parser."""
    t = (_TYPE_RE.findall(xml_text[:4000]) or ["unknown"])[0]
    lst = SAMPLES.setdefault(f"edct_{t}", [])
    if len(lst) < 3:
        lst.append(xml_text[:200000])


def _remember_edct(ev):
    with LOCK:
        lst = EDCTS.setdefault(ev["acid"], [])
        lst[:] = [e for e in lst if (e.get("dep"), e.get("igtd")) != (ev.get("dep"), ev.get("igtd"))] + [ev]
        del lst[:-4]
    _edct_dirty[0] = True


def save_edcts(force=False):
    """Drop EDCTs more than 12 h past, then write the rest to disk (only when something changed;
    at most every 2 min unless forced, as on shutdown)."""
    if not EDCT_PATH or not _edct_dirty[0] or (not force and time.time() - _edct_saved[0] < 120):
        return
    _edct_saved[0] = time.time()
    import json
    from datetime import datetime
    cut = time.time() - 12 * 3600
    def ts(x):
        try:
            return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0
    with LOCK:
        for acid in list(EDCTS):
            EDCTS[acid] = [e for e in EDCTS[acid] if ts(e.get("edct") or "") > cut]
            if not EDCTS[acid]:
                del EDCTS[acid]
        data = json.dumps(EDCTS)
        _edct_dirty[0] = False
    try:
        tmp = EDCT_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        import os
        os.replace(tmp, EDCT_PATH)
    except OSError as e:
        log.info("SWIM EDCTs not saved: %s", e)


def load_edcts(path):
    global EDCT_PATH
    EDCT_PATH = path
    import json
    try:
        with open(path) as f:
            EDCTS.update(json.load(f) or {})
    except (OSError, ValueError):
        pass


def _k3(code):
    c = (code or "").strip().upper()
    return c[1:] if len(c) == 4 and c.startswith("K") else c


def _program_keep(xml_text):
    """A GDP/GS/AFP message for an airport one of your flights departs, lands at or holds an EDCT for."""
    with LOCK:
        apts = {_k3(e.get(k)) for cs in WATCH for e in EVENTS.get(cs, []) for k in ("dep", "arr")}
        apts |= {_k3(e.get("element")) for cs in WATCH for e in EDCTS.get(cs, [])}
    apts.discard("")
    if not apts:
        return
    found = set(_re.findall(r"<(?:\w+:)?(?:airportId|ctlElement)>\s*([A-Z0-9]{3,4})\s*<", xml_text))
    hit = sorted({_k3(a) for a in found} & apts)
    if not hit:
        return
    t = (_TYPE_RE.findall(xml_text[:4000]) or ["flow"])[0]
    m = _re.search(r"<(?:\w+:)?tmiStatus>\s*(\w+)", xml_text)
    body = _re.search(r"fiMessage\b[^>]*>\s*<(?:\w+:)?(\w+)", xml_text)   # the message body: gdpAdvisory, gsCancel …
    # TFMS re-sends a program's advisory every 15 min unchanged: count those on the entry already kept,
    # so the 20 kept are the changes (proposed, actual, revisions, cancel), not 5 hours of repeats
    sig = _re.sub(r"<(?:\w+:)?(updateTime|adlTime|issueTime|sourceTimeStamp)>[^<]*<", "<", xml_text)
    sig = _re.sub(r'(?:sourceTimeStamp|TimeStamp|msgTime)="[^"]*"', "", sig)
    import hashlib
    sig = hashlib.md5(sig.encode()).hexdigest()
    with LOCK:
        for a in hit:
            lst = PROGRAM_TRAIL.setdefault(a, [])
            if lst and lst[-1].get("sig") == sig:
                lst[-1]["repeats"] = lst[-1].get("repeats", 0) + 1
                lst[-1]["last"] = time.time()
                continue
            lst.append({"at": time.time(), "type": t, "status": m.group(1) if m else None, "sig": sig,
                        "kind": body.group(1) if body else None, "xml": xml_text[:250000]})
            del lst[:-20]
    _yours_dirty[0] = True


def save_yours(force=False):
    """The raw messages about your flights (by type, and the EDCT trail) to disk, at most every 2 min,
    dropping trail entries older than 36 h and callsigns no longer watched for that long."""
    if not YOURS_PATH or not _yours_dirty[0] or (not force and time.time() - _yours_saved[0] < 120):
        return
    _yours_saved[0] = time.time()
    import json
    import os
    cut = time.time() - 36 * 3600
    with LOCK:
        for cs in list(EDCT_TRAIL):
            EDCT_TRAIL[cs] = [m for m in EDCT_TRAIL[cs] if m.get("at", 0) > cut]
            if not EDCT_TRAIL[cs]:
                del EDCT_TRAIL[cs]
        for a in list(PROGRAM_TRAIL):
            PROGRAM_TRAIL[a] = [m for m in PROGRAM_TRAIL[a] if m.get("at", 0) > cut]
            if not PROGRAM_TRAIL[a]:
                del PROGRAM_TRAIL[a]
        evs = {cs: [e for e in EVENTS.get(cs, []) if (e.get("got") or 0) > cut] for cs in WATCH if EVENTS.get(cs)}
        data = json.dumps({"by_type": YOURS_BY_TYPE, "trail": EDCT_TRAIL, "programs": PROGRAM_TRAIL, "events": evs})
        _yours_dirty[0] = False
    try:
        tmp = YOURS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, YOURS_PATH)
    except OSError as e:
        log.info("SWIM messages about your flights not saved: %s", e)


def load_yours(path):
    global YOURS_PATH
    YOURS_PATH = path
    import json
    try:
        with open(path) as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return
    for t, lst in (d.get("by_type") or {}).items():
        YOURS_BY_TYPE[t] = (list(lst) + YOURS_BY_TYPE.get(t, []))[-4:]
    for cs, lst in (d.get("trail") or {}).items():
        EDCT_TRAIL[cs] = (list(lst) + EDCT_TRAIL.get(cs, []))[-10:]
    for a, lst in (d.get("programs") or {}).items():
        PROGRAM_TRAIL[a] = (list(lst) + PROGRAM_TRAIL.get(a, []))[-20:]
    # the parsed SWIM events for your flights survive a restart (the samples zip and the leg's facts)
    for cs, lst in (d.get("events") or {}).items():
        have = {(e.get("type"), e.get("at"), e.get("src")) for e in EVENTS.get(cs, [])}
        EVENTS[cs] = ([e for e in lst if (e.get("type"), e.get("at"), e.get("src")) not in have] + EVENTS.get(cs, []))[-KEEP:]


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


# ---------------------------------------------------------------- TBFM
# Time Based Flow Management (metering): per flight, its departure and arrival airports, the meter
# fix, runway, the scheduled (metered) times, the departure release time (ctm, "coordination time")
# and the EDCT TBFM holds for it (etm). Read loosely: a flight record is any element that names a
# flight (aid) with its own fields as children or attributes, whatever the namespace.
_TBFM_TIMEY = _re.compile(r"(^|_)(etm|ctm|sta|eta|std|etd|stdt|eta_rwy|sta_rwy|rta|eto|sto|ott|ctd|cta|edct)", _re.I)


def _tbfm_time(v, ref=None):
    """A TBFM time -> ISO UTC. Seen shapes: ISO (with or without Z), epoch seconds or ms,
    and DDHHMM / HHMM (nearest to ref)."""
    v = (v or "").strip()
    if not v:
        return None
    from datetime import datetime, timezone, timedelta
    try:
        if "T" in v or "-" in v:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if v.isdigit() and len(v) >= 10:
            t = int(v) / (1000 if len(v) >= 13 else 1)
            return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if v.isdigit() and len(v) in (4, 6):
            base = datetime.now(timezone.utc) if not ref else datetime.fromisoformat(ref.replace("Z", "+00:00"))
            day = int(v[:2]) if len(v) == 6 else None
            hh, mm = int(v[-4:-2]), int(v[-2:])
            best = None
            for dd in (-1, 0, 1):
                c = (base + timedelta(days=dd)).replace(hour=hh, minute=mm, second=0, microsecond=0)
                if day is not None and c.day != day:
                    continue
                if best is None or abs((c - base).total_seconds()) < abs((best - base).total_seconds()):
                    best = c
            return best.strftime("%Y-%m-%dT%H:%M:%SZ") if best else None
    except Exception:
        return None
    return None


def parse_tbfm(xml_text):
    """TBFM metering messages -> [{acid, dep, arr, type, src: 'TBFM', at, igtd, tbfm: {field: value}, edct}].
    Shape (tbfmmeteringpublication 1.1): <env><tma msgTime><air aid dap apt airType ...> with groups of
    fields under it (<flt>, <eta> with mfx / eta_mfx / eta_dfx / eta_sfx, scheduled times, ...): every
    field anywhere under a flight's <air> is read."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for tma in root.iter():
        if _local(tma.tag) != "tma":
            continue
        at = tma.get("msgTime")
        for el in tma:
            if _local(el.tag) != "air":
                continue
            kids = {_local(k): v for k, v in el.attrib.items()}
            for c in el.iter():
                if c is el or list(c):
                    continue
                t = (c.text or "").strip()
                n = _local(c.tag)
                if t and n not in kids:
                    kids[n] = t
            aid = (kids.get("aid") or "").upper()
            if not aid:
                continue
            f = {}
            for k, v in kids.items():
                if _TBFM_TIMEY.search(k):
                    f[k] = _tbfm_time(v, at) or v
                elif k in ("mfx", "dfx", "sfx", "rwy", "trw", "gat", "cfg", "scn", "tra", "fps", "acs", "typ", "tmaId", "apreq", "sus", "airType"):
                    f[k] = v
            etm = f.get("etm")
            out.append({"acid": aid, "dep": _apt(kids.get("dap") or ""), "arr": _apt(kids.get("apt") or ""),
                        "type": f"TBFM {kids.get('airType') or 'air'}", "src": "TBFM", "at": at, "igtd": None, "tbfm": f,
                        "edct": etm if isinstance(etm, str) and "T" in etm else None})
    return out


def handle_tbfm(xml_text):
    """TBFM (metering): release times, scheduled runway times and the EDCT TBFM holds, for your flights."""
    TBFM_STATE["messages"] += 1
    TBFM_STATE["last_message"] = time.time()
    if not xml_text.strip():
        return
    head = xml_text[:200000]
    try:
        types = _TYPE_RE.findall(head[:3000]) or _ROOT_RE.findall(head[:400])[:1] or ["unknown"]
        for t in set(types):
            TBFM_TYPES[t] = TBFM_TYPES.get(t, 0) + 1
            lst = SAMPLES.setdefault("TBFM_" + t, [])
            if len(lst) < 3:
                lst.append(head)
        # one example of each record layout (its type and the groups of fields it carries), so the
        # scheduled times, release times and EDCTs show up in the samples, not only amendments
        m = _re.search(r'airType="(\w+)"', head[:3000])
        if m:
            groups = sorted(set(_re.findall(r"<(\w+)>\s*<", head[:20000])) - {"tma", "air", "env"})
            key = f"TBFM_{m.group(1)}_{'-'.join(groups)[:60] or 'plain'}"
            if key not in SAMPLES and sum(1 for k in SAMPLES if k.startswith("TBFM_")) < 60:
                SAMPLES[key] = [head]
    except Exception:
        pass
    if not WATCH or not any(cs in xml_text for cs in WATCH):
        return
    hit = False
    for ev in parse_tbfm(xml_text):
        if ev["acid"] in WATCH:
            hit = True
            ev["got"] = time.time()
            _keep(ev)
            _news(ev, force=bool(ev.get("edct")))
            if ev.get("edct"):
                with LOCK:
                    trail = EDCT_TRAIL.setdefault(ev["acid"], [])
                    trail.append({"at": time.time(), "type": "TBFM", "xml": head[:250000]})
                    del trail[:-10]
                _yours_dirty[0] = True
    if hit:
        TBFM_STATE["matched"] += 1
        MATCHED_RAW.append({"at": time.time(), "xml": head, "feed": "TBFM"})
        del MATCHED_RAW[:-40]
    # every TBFM message naming your flight is kept as a sample, so the reader can be tuned to it
    with LOCK:
        for cs in [c for c in WATCH if c in xml_text]:
            lst = YOURS_BY_TYPE.setdefault("TBFM", [])
            lst.append(head[:150000])
            del lst[:-6]


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
                if v.upper() not in ("UNKN", "UNK", "UNKNOWN", "NONE"):   # radar-only target, no transponder
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
    if root == "TDLSCSPMessage":
        _pdc_sample(xml_text)
    if WATCH and root in ("TATrackAndFlightPlan", "SurfaceMovementEventMessage", "TowerDepartureEventMessage",
                          "TDLSCSPMessage") \
            and (any(cs in xml_text for cs in WATCH)
                 or (root == "TDLSCSPMessage" and any(_flight_no(cs) and _re.search(r"\b" + _flight_no(cs) + r"\b", xml_text) for cs in WATCH))):
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


_AIRLINE_2 = {"UAL": "UA", "AAL": "AA", "DAL": "DL", "SWA": "WN", "JBU": "B6", "ASA": "AS", "SKW": "OO",
              "RPA": "YX", "ENY": "MQ", "NKS": "NK", "FFT": "F9", "HAL": "HA", "AAY": "G4", "GJS": "G7", "UCA": "C5"}


def _flight_no(cs):
    """UAL1643 -> UA1643 (how airline messages sometimes name a flight)."""
    m = _re.fullmatch(r"([A-Z]{3})(\d+[A-Z]?)", cs or "")
    return _AIRLINE_2[m.group(1)] + m.group(2) if m and m.group(1) in _AIRLINE_2 else None


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
    if kind == "TDLSCSPMessage":
        body = _txt(root, "dataBody") or ""
        if acid not in WATCH:
            # a clearance whose header field names the flight differently (or not at all): the
            # callsign is in the clearance text itself
            acid = next((cs for cs in WATCH if _re.search(r"\b" + cs + r"\b", body)
                         or (_flight_no(cs) and _re.search(r"\b" + _flight_no(cs) + r"\b", body))), acid)
        if acid in WATCH:
            PDC_RAW.append({"at": time.time(), "xml": xml_text[:50000]})
            del PDC_RAW[:-30]
    if acid not in WATCH:
        return
    if kind == "TDLSCSPMessage":
        pdc = parse_pdc(_txt(root, "dataBody") or "")
        t = _txt(root, "time") or ""
        if len(t) == 14 and t.isdigit():   # MMDDYYYYhhmmss, UTC
            pdc["at"] = f"{t[4:8]}-{t[0:2]}-{t[2:4]}T{t[8:10]}:{t[10:12]}:{t[12:14]}Z"
        pdc["squawk"] = _txt(root, "beaconCode")
        ev = {"acid": acid, "src": "STDDS", "got": now, "type": kind, "igtd": None,
              "dep": _apt(_txt(root, "airportID")), "arr": _apt(_txt(root, "destinationAirport")), "pdc": pdc}
        _keep(ev)
        STDDS_STATE["matched"] += 1
        MATCHED_RAW.append({"at": now, "xml": xml_text[:200000], "feed": "STDDS"})
        del MATCHED_RAW[:-40]
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


def _strip_route(t):
    """The flight-strip clearance's route and destination, and the EDCT printed after it:
    '… 370 -SSTIK5 YYUNG LAX- KSFO SSTIK5 KTINA … PLYYA2 KSAN 1557 CLEARED …' (the -…- part is the
    amended piece; remarks like @MEDEVAC and the './.' route shorthand can follow)."""
    m = _re.search(r"\bP\d{4} \S+ \d{2,3} (.*?) CLEARED ", t)
    if not m:
        return None, None, None
    body = _re.sub(r"(?:^| )-[^-]*- ?", " ", " " + m.group(1)).split()
    edct = None
    while body and (body[-1].startswith("@") or _re.fullmatch(r"\d{4}", body[-1])):
        tok = body.pop()
        if tok.isdigit():
            edct = tok
    if not body:
        return None, None, edct
    dest = body[-1].split("./.")[-1]
    return " ".join(body), (dest if _re.fullmatch(r"[A-Z0-9]{3,4}", dest) else None), edct


def parse_pdc(text):
    """A pre-departure clearance by datalink as the tower's system logs it, in either format: the CPDLC
    DCL ('/AN N47434 … CLEARED TO KIAD ARPT …') or the flight-strip style ('001 UAL444 3616 KSFO B738/L
    P1527 960 370 … CLEARED SSTIK5 DEPARTURE …'); or the delivery note that follows ('UAL444 001 Y C
    .N14214 P1527 GE6', 'DAL425 001 N193DN KLAX G133') naming the airplane and gate ->
    {tail, gate, response, cleared_to, departure, altitude, expect_alt, dep_freq, runway, fl, edct, route, text}."""
    t = " ".join((text or "").split())
    def g(rx):
        m = _re.search(rx, t)
        return m.group(1) if m else None
    route = g(r"\b([A-Z]{4}\.[A-Z0-9./]+\.[A-Z]{4})\s*$")
    # a route change by datalink: 'MODIFIED RTE … KDEN.BAYLR6.TEHRU..PLNDL.Q86.TTRUE.LUCKI1.KSAN BAYLR6.TEHRU'
    # or 'REVISED RTE KDEN*.SMMUR2.DAAYE..ZUN…LUCKI1.KSAN REVISED DPP SMMUR2.DAAYE … REVISED DEPFREQ 128.45'
    rc = g(r"\b(MODIFIED|REVISED) RTE\b")
    rc_dep = None
    if rc:
        rts = _re.findall(r"\b([A-Z]{4}\*?\.[A-Z0-9./*]+\.[A-Z]{4})\b", t)
        if rts:
            route = rts[-1].replace("*", "")
        rc_dep = g(r"REVISED DPP ([A-Z0-9]+\.[A-Z0-9]+)") or g(r"\.[A-Z]{4} ([A-Z]+\d[A-Z]?\.[A-Z0-9]+) ")
    s_route, s_dest, s_edct = _strip_route(t)
    note = _re.fullmatch(r"[A-Z0-9]+ \d{1,3} (?:Y C \.(N[0-9][0-9A-Z]{0,4})? P\d{4}|(N[0-9][0-9A-Z]{0,4}) [A-Z0-9]{4})(?: G([A-Z0-9]{1,5}))?", t)
    alt = g(r"MAINT(?:AIN)? (\d+) ?FT")
    out = {
        "tail": g(r"/AN (N?[A-Z0-9-]+)") or (note and (note.group(1) or note.group(2))),
        "gate": note and note.group(3),
        "response": g(r"PILOT RESPONSE - ([A-Z ]+?)(?: \d{3}|$)"),
        "route_change": rc and rc.lower(),
        "cleared_to": g(r"CLEARED TO ([A-Z0-9]{4}) (?:ARPT|AIRPORT)") or s_dest
                      or (rc and route and route.split(".")[-1])
                      or g(r"\.([A-Z]{4})\s*$") or g(r"CLEARED TO ([A-Z0-9]{3,4})"),
        "departure": rc_dep or g(r"CLEARED TO [A-Z0-9]{3,4}(?: AIRPORT)? ([A-Z0-9]+[0-9][A-Z0-9]*(?:\.[A-Z0-9]+)?)")
                     or " ".join(x for x in (g(r"CLEARED ([A-Z]+\d[A-Z]?) DEPARTURE"), g(r"DEPARTURE ([A-Z0-9]+) TRSN")) if x)
                     or g(r"CLEARED TO [A-Z0-9]{3,5} VIA [A-Z0-9 ]*?\b([A-Z]+\d\.[A-Z0-9]+)") or None,
        "altitude": alt or ("climb via SID" if _re.search(r"CL(?:IM)?B VIA SID", t) else None),
        "expect_alt": g(r"\bEXP (\d{3}) "),
        "dep_freq": g(r"DEP FREQ ([\d.]+)") or g(r"DPFRQ ([\d.]+)") or g(r"REVISED DEPFREQ ([\d.]+)")
                    or ("see SID" if _re.search(r"(?:DPFRQ|DEP FREQ) SEE SID", t) else None),
        "runway": g(r"(?:EXPECT|XPCT|DEP) RWY (\S+)"),
        "fl": g(r"\bFL(\d{3})\b") or g(r"\bP\d{4} \S+ (\d{3}) "),
        "edct": g(r"EDCT (\d{4})") or s_edct,
        "route": route_text(route) if route else s_route,
        "text": t if "CLEAR" in t or rc else None}
    return {k: v for k, v in out.items() if v}

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


def prune(max_age=12 * 3600):
    """Forget callsigns no longer watched (their last message over 12 h old), so months of flights
    don't pile up in memory. Nothing from the feeds is ever written to disk."""
    now = time.time()
    with LOCK:
        for acid in [a for a, lst in EVENTS.items() if a not in WATCH and (not lst or now - (lst[-1].get("got") or 0) > max_age)]:
            del EVENTS[acid]
        for acid in [a for a, recs in LIVE.items() if a not in WATCH and all(now - r["at"] > max_age for r in recs.values())]:
            del LIVE[acid]
        for ap in [a for a in SURFACE if a not in WATCH_AIRPORTS]:
            del SURFACE[ap]


def events(callsign):
    cs = (callsign or "").upper()
    with LOCK:
        return list(EDCTS.get(cs, [])) + list(EVENTS.get(cs, []))


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
    if (cfg.get("swim_tbfm_queue") or "").strip():
        user = cfg.get("swim_tbfm_username") or cfg.get("swim_username")
        pw = cfg.get("swim_tbfm_password") or cfg.get("swim_password")
        if user and pw:
            out.append({"name": "TBFM", "state": TBFM_STATE, "handle": handle_tbfm, "queue": cfg["swim_tbfm_queue"],
                        "host": cfg.get("swim_tbfm_host") or "tcps://ems2.swim.faa.gov:55443",
                        "vpn": cfg.get("swim_tbfm_vpn") or "TBFM", "user": user, "password": pw})
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


def _edct_counts():
    """How many flights hold an EDCT from each program (EWR: 140 …), to see a program's EDCTs arrive or vanish."""
    out = {}
    for lst in list(EDCTS.values()):
        for el in {_apt(e.get("element")) or "?" for e in lst}:
            out[el] = out.get(el, 0) + 1
    return out


def samples_zip():
    """Examples of each message type seen, the last messages for your flights, and counts."""
    import io
    import json
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.json", json.dumps({"state": STATE, "watching": sorted(WATCH), "types": TYPE_COUNTS, "airline_sent": SOURCE_COUNTS,
                                               "tfdm": {"state": TFDM_STATE, "types": TFDM_TYPES},
                                               "tbfm": {"state": TBFM_STATE, "types": TBFM_TYPES},
                                               "stdds": {"state": STDDS_STATE, "types": STDDS_TYPES,
                                                         "airports": sorted(WATCH_AIRPORTS),
                                                         "targets": {k: len(v) for k, v in SURFACE.items()}},
                                               "events": {k: v for k, v in EVENTS.items()},
                                               "edcts": {"flights": len(EDCTS), "yours": {k: EDCTS[k] for k in WATCH if k in EDCTS},
                                                         "by_program": _edct_counts()},
                                               "edct_trail": {k: [{"at": m.get("at"), "type": m.get("type")} for m in v]
                                                              for k, v in list(EDCT_TRAIL.items())},
                                               "program_trail": {k: [{"at": m.get("at"), "type": m.get("type"), "status": m.get("status"),
                                                                      "kind": m.get("kind"), "repeats": m.get("repeats"),
                                                                      "last": m.get("last")} for m in v]
                                                                 for k, v in list(PROGRAM_TRAIL.items())}},
                                              indent=1, default=str))
        for t, lst in SAMPLES.items():
            for i, x in enumerate(lst, 1):
                if isinstance(x, tuple):   # (label, xml)
                    z.writestr(f"samples/{t}/{_re.sub(r'[^A-Za-z0-9_]+', '_', x[0])}.xml", x[1])
                else:
                    z.writestr(f"samples/{_re.sub(r'[^A-Za-z0-9_]+', '_', t)}_{i}.xml", x)
        for i, m in enumerate(MATCHED_RAW, 1):
            z.writestr(f"your_flights/{i:02d}{'_' + m['feed'] if m.get('feed') else ''}.xml", m["xml"])
        for t, lst in YOURS_BY_TYPE.items():
            for i, x in enumerate(lst, 1):
                z.writestr(f"your_flights/by_type/{_re.sub(r'[^A-Za-z0-9_]+', '_', t)}_{i}.xml", x)
        for cs, lst in list(EDCT_TRAIL.items()):
            for j, m in enumerate(list(lst), 1):
                stamp = time.strftime("%H%M%SZ", time.gmtime(m.get("at") or 0))
                z.writestr(f"your_flights/edct_trail/{_re.sub(r'[^A-Za-z0-9_]+', '_', cs)}_{j:02d}_{stamp}_"
                           f"{_re.sub(r'[^A-Za-z0-9_]+', '_', m.get('type') or 'unknown')}.xml", m["xml"])
        for a, lst in list(PROGRAM_TRAIL.items()):
            for j, m in enumerate(list(lst), 1):
                stamp = time.strftime("%H%M%SZ", time.gmtime(m.get("at") or 0))
                z.writestr(f"your_flights/programs/{_re.sub(r'[^A-Za-z0-9_]+', '_', a)}_{j:02d}_{stamp}_"
                           f"{_re.sub(r'[^A-Za-z0-9_]+', '_', (m.get('type') or '') + '_' + (m.get('kind') or m.get('status') or ''))}.xml", m["xml"])
        for i, m in enumerate(PDC_RAW, 1):
            z.writestr(f"your_flights/clearance_{i:02d}.xml", m["xml"])
        for i, x in enumerate(STDDS_RAW, 1):
            z.writestr(f"your_flights/surface_{i:02d}.xml", x)
    return buf.getvalue()
