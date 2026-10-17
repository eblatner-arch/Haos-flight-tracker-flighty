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
                elif n == "flightTimeData" and sub.get("airlineOutTime"):
                    # The airline's own gate/runway times (CDM): only from messages the airline sent.
                    ev["airline"] = {k: sub.get(f"airline{k.title()}Time") for k in ("out", "off", "on", "in")}
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
            with LOCK:
                lst = EVENTS.setdefault(ev["acid"], [])
                lst.append(ev)
                del lst[:-KEEP]
            STATE["matched"] += 1
    if hit:
        MATCHED_RAW.append({"at": time.time(), "xml": xml_text[:200000]})
        del MATCHED_RAW[:-20]


TFDM_STATE = {"status": "off", "error": None, "connected_at": None, "messages": 0, "matched": 0,
              "last_message": None, "rate": 0.0}
TFDM_TYPES = {}
_ROOT_RE = _re.compile(r'<(?![?!])(?:[\w.-]+:)?([\w.-]+)')


def handle_tfdm(xml_text):
    """TFDM (tower surface data): not parsed yet. Count and sample each kind, and keep the raw
    messages that mention your flights, so the parser can be built from the real feed."""
    TFDM_STATE["messages"] += 1
    TFDM_STATE["last_message"] = time.time()
    head = xml_text[:200000]
    try:
        types = _TYPE_RE.findall(head) or _ROOT_RE.findall(head)[:1] or ["unknown"]
        for t in set(types):
            TFDM_TYPES[t] = TFDM_TYPES.get(t, 0) + 1
            lst = SAMPLES.setdefault("TFDM_" + t, [])
            if len(lst) < 3:
                lst.append(head)
    except Exception:
        pass
    if WATCH and any(cs in xml_text for cs in WATCH):
        TFDM_STATE["matched"] += 1
        MATCHED_RAW.append({"at": time.time(), "xml": head, "feed": "TFDM"})
        del MATCHED_RAW[:-40]


def events(callsign):
    with LOCK:
        return list(EVENTS.get((callsign or "").upper(), []))


def configured(cfg):
    return all((cfg.get(k) or "").strip() for k in ("swim_username", "swim_password", "swim_queue"))


def _feeds(cfg):
    """The subscriptions to read: TFMS (flight data) and, when its queue is set, TFDM (tower).
    TFDM uses the TFMS username/password unless its own are given."""
    out = []
    if configured(cfg):
        out.append({"name": "TFMS", "state": STATE, "handle": handle, "queue": cfg["swim_queue"],
                    "vpn": cfg.get("swim_vpn") or "TFMS", "user": cfg["swim_username"], "password": cfg["swim_password"]})
    if (cfg.get("swim_tfdm_queue") or "").strip():
        user = cfg.get("swim_tfdm_username") or cfg.get("swim_username")
        pw = cfg.get("swim_tfdm_password") or cfg.get("swim_password")
        if user and pw:
            out.append({"name": "TFDM", "state": TFDM_STATE, "handle": handle_tfdm, "queue": cfg["swim_tfdm_queue"],
                        "vpn": cfg.get("swim_tfdm_vpn") or "TFDM", "user": user, "password": pw})
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


def _cert_dir():
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
        host = (_cfg.get("swim_host") or "tcps://ems1.swim.faa.gov:55443").split("://")[-1]
        h, _, port = host.partition(":")
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
    host = (_cfg.get("swim_host") or "tcps://ems1.swim.faa.gov:55443").strip()
    if _cfg.get("swim_skip_cert_check"):
        tls = TLS.create().without_certificate_validation()
    else:
        # SCDS presents a Let's Encrypt certificate; Debian's store has its root (ISRG Root X1)
        # in the hashed-directory form the Solace library expects.
        tls = TLS.create().with_certificate_validation(True, False, _cert_dir())
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
                body = msg.get_payload_as_string()
                if body is None:
                    raw = msg.get_payload_as_bytes() or b""
                    if raw[:2] == b"\x1f\x8b":
                        import gzip
                        raw = gzip.decompress(raw)
                    body = raw.decode("utf-8", "replace")
                feed["handle"](body)
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


def samples_zip():
    """Examples of each message type seen, the last messages for your flights, and counts."""
    import io
    import json
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.json", json.dumps({"state": STATE, "watching": sorted(WATCH), "types": TYPE_COUNTS, "airline_sent": SOURCE_COUNTS,
                                               "tfdm": {"state": TFDM_STATE, "types": TFDM_TYPES},
                                               "events": {k: v for k, v in EVENTS.items()}}, indent=1, default=str))
        for t, lst in SAMPLES.items():
            for i, x in enumerate(lst, 1):
                z.writestr(f"samples/{_re.sub(r'[^A-Za-z0-9_]+', '_', t)}_{i}.xml", x)
        for i, m in enumerate(MATCHED_RAW, 1):
            z.writestr(f"your_flights/{i:02d}{'_' + m['feed'] if m.get('feed') else ''}.xml", m["xml"])
    return buf.getvalue()
