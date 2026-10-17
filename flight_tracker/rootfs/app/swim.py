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
_thread = None
_cfg = {}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _apt(code):
    """TFMS airports are ICAO (KEWR) or FAA codes (ATL): -> as given, upper case."""
    return (code or "").strip().upper() or None


def parse(xml_text):
    """Flight-data messages in one TFMS payload -> [{acid, dep, arr, type, at, igtd, gufi}].
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
                  "type": a.get("msgType"), "at": a.get("sourceTimeStamp"), "igtd": None, "gufi": None}
            for sub in el.iter():
                n = _local(sub.tag)
                if n == "igtd" and sub.text and not ev["igtd"]:
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


def handle(xml_text):
    """Keep the events for watched callsigns."""
    STATE["messages"] += 1
    STATE["last_message"] = time.time()
    if not WATCH:
        return
    if not any(cs in xml_text for cs in WATCH):  # cheap test before parsing
        return
    for ev in parse(xml_text):
        if ev["acid"] in WATCH:
            ev["got"] = time.time()
            with LOCK:
                lst = EVENTS.setdefault(ev["acid"], [])
                lst.append(ev)
                del lst[:-KEEP]
            STATE["matched"] += 1


def events(callsign):
    with LOCK:
        return list(EVENTS.get((callsign or "").upper(), []))


def configured(cfg):
    return all((cfg.get(k) or "").strip() for k in ("swim_username", "swim_password", "swim_queue"))


def start(cfg):
    global _thread, _cfg
    _cfg = cfg
    if not configured(cfg):
        STATE.update(status="off")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, daemon=True, name="swim")
    _thread.start()


def _run():
    backoff = 10
    while True:
        try:
            _session()
            backoff = 10
        except Exception as e:
            STATE.update(status="error", error=str(e).splitlines()[0][:200])
            log.warning("SWIM: %s; reconnecting in %d s", STATE["error"], backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 600)


def _session():
    """One connection: bind to the queue and read until it drops."""
    from solace.messaging.messaging_service import MessagingService
    from solace.messaging.config.transport_security_strategy import TLS
    from solace.messaging.config.authentication_strategy import BasicUserNamePassword
    from solace.messaging.resources.queue import Queue
    from solace.messaging.receiver.message_receiver import MessageHandler

    host = (_cfg.get("swim_host") or "tcps://ems1.swim.faa.gov:55443").strip()
    props = {"solace.messaging.transport.host": host,
             "solace.messaging.service.vpn-name": (_cfg.get("swim_vpn") or "TFMS").strip(),
             "solace.messaging.transport.reconnection-attempts": "20",
             "solace.messaging.transport.connection.retries-per-host": "3"}
    tls = TLS.create().with_certificate_validation(True, False, "/etc/ssl/certs")
    if _cfg.get("swim_skip_cert_check"):
        tls = TLS.create().without_certificate_validation()
    STATE.update(status="connecting", error=None)
    svc = (MessagingService.builder().from_properties(props)
           .with_transport_security_strategy(tls)
           .with_authentication_strategy(BasicUserNamePassword.of(_cfg["swim_username"].strip(), _cfg["swim_password"].strip()))
           .build())
    svc.connect()
    dropped = threading.Event()
    try:
        svc.add_service_interruption_listener(lambda *_: dropped.set())
    except Exception:
        pass
    receiver = svc.create_persistent_message_receiver_builder().build(
        Queue.durable_exclusive_queue(_cfg["swim_queue"].strip()))
    receiver.start()

    class H(MessageHandler):
        def on_message(self, msg):
            try:
                body = msg.get_payload_as_string()
                if body is None:
                    raw = msg.get_payload_as_bytes() or b""
                    if raw[:2] == b"\x1f\x8b":
                        import gzip
                        raw = gzip.decompress(raw)
                    body = raw.decode("utf-8", "replace")
                handle(body)
            except Exception as e:
                log.debug("SWIM message: %s", e)
            finally:
                try:
                    receiver.ack(msg)
                except Exception:
                    pass
    receiver.receive_async(H())
    STATE.update(status="connected", connected_at=time.time(), error=None)
    log.info("SWIM connected to %s, queue %s", host, _cfg["swim_queue"])
    last_n, last_t = STATE["messages"], time.time()
    try:
        while not dropped.is_set() and svc.is_connected:
            time.sleep(30)
            n, t = STATE["messages"], time.time()
            STATE["rate"] = round((n - last_n) / max(1, t - last_t) * 60, 1)  # per minute
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
