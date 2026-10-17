"""The headed Chromium (on Xvfb, visible in the noVNC viewer) and the Gemini screenshot reader.

Only the tracker worker thread may call into Browser: Playwright's sync API is
not thread-safe.
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

log = logging.getLogger("flight_tracker")

BOOTSTRAP_JS = """() => {
  try { const b = window.trackpollBootstrap; return b ? JSON.stringify(b) : null; }
  catch (e) { return null; }
}"""


class VisionFlight(BaseModel):
    status: str
    cancelled: Optional[bool] = None
    diverted: Optional[bool] = None
    flight_number: Optional[str] = None
    origin_iata: Optional[str] = None
    destination_iata: Optional[str] = None
    departure_gate: Optional[str] = None
    departure_terminal: Optional[str] = None
    arrival_gate: Optional[str] = None
    arrival_terminal: Optional[str] = None
    baggage_claim: Optional[str] = None
    gate_departure_scheduled: Optional[str] = None
    gate_departure_estimated: Optional[str] = None
    gate_departure_actual: Optional[str] = None
    takeoff_actual: Optional[str] = None
    landing_actual: Optional[str] = None
    gate_arrival_scheduled: Optional[str] = None
    gate_arrival_estimated: Optional[str] = None
    gate_arrival_actual: Optional[str] = None
    aircraft_type: Optional[str] = None
    registration: Optional[str] = None
    inbound_flight: Optional[str] = None
    filed_route: Optional[str] = None


VISION_PROMPT = """This is a screenshot of a FlightAware flight page for flight {ident}
scheduled to depart {dep} around {when}. Read the details of THAT flight.
Give every time as ISO 8601 with its UTC offset (e.g. 2026-10-14T14:05:00-04:00),
using the time zone the page shows for that airport. Use null for anything not visible.
status: a short phrase such as Scheduled, On Time, Delayed, Taxiing, En Route,
Landed, Arrived, Cancelled, Diverted. If the page shows an error, a CAPTCHA,
a login wall, or no matching flight, set status to Unavailable."""


def _iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo else None
    except ValueError:
        return None


def vision_to_flight(v: VisionFlight):
    if (v.status or "").lower().startswith("unavailable"):
        return None

    def ap(code, gate, term, bag=None):
        return {"iata": (code or None), "icao": None, "name": None, "city": None, "lat": None, "lon": None,
                "tz": None, "gate": gate, "terminal": term, "baggage": bag, "delays": None}

    return {
        "fa_id": None, "ident": v.flight_number, "display_ident": v.flight_number,
        "status_text": v.status, "cancelled": bool(v.cancelled) or "cancel" in (v.status or "").lower(),
        "diverted": bool(v.diverted) or "divert" in (v.status or "").lower(),
        "origin": ap(v.origin_iata, v.departure_gate, v.departure_terminal),
        "destination": ap(v.destination_iata, v.arrival_gate, v.arrival_terminal, v.baggage_claim),
        "times": {
            "out": {"scheduled": _iso(v.gate_departure_scheduled), "estimated": _iso(v.gate_departure_estimated),
                    "actual": _iso(v.gate_departure_actual)},
            "off": {"scheduled": None, "estimated": None, "actual": _iso(v.takeoff_actual)},
            "on": {"scheduled": None, "estimated": None, "actual": _iso(v.landing_actual)},
            "in": {"scheduled": _iso(v.gate_arrival_scheduled), "estimated": _iso(v.gate_arrival_estimated),
                   "actual": _iso(v.gate_arrival_actual)},
        },
        "aircraft_type": v.aircraft_type, "aircraft_name": None, "tail": v.registration,
        "route": v.filed_route, "filed_altitude": None, "filed_speed": None, "distance_nm": None, "ete": None,
        "waypoints": [], "track": [], "inbound_fa_id": None, "inbound_ident": v.inbound_flight,
    }


class Browser:
    def __init__(self, profile_dir, shot_dir, gemini_key="", gemini_model="gemini-flash-lite-latest"):
        self.profile_dir = profile_dir
        self.shot_dir = shot_dir
        self.gemini_model = gemini_model
        self.client = None
        if gemini_key:
            from google import genai
            self.client = genai.Client(api_key=gemini_key)
        os.makedirs(shot_dir, exist_ok=True)
        self._pw = None
        self._ctx = None

    # -- lifecycle ---------------------------------------------------------
    def _ensure(self):
        if self._ctx is not None:
            return
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            headless=False,  # on Xvfb, so the noVNC viewer can show it
            viewport={"width": 1366, "height": 900},
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        log.info("Browser started")

    def reset(self):
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        self._ctx = None

    # -- pages -------------------------------------------------------------
    def load(self, url, shot_name):
        """Open url; return (bootstrap dict or None, screenshot path or None)."""
        for attempt in (1, 2):
            try:
                self._ensure()
                page = self._ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.get_by_role("button", name=re.compile("accept|agree|got it", re.I)).first.click(timeout=2500)
                    except Exception:
                        pass
                    boot = None
                    for _ in range(8):  # the variable appears once the page script runs
                        raw = page.evaluate(BOOTSTRAP_JS)
                        if raw:
                            boot = json.loads(raw)
                            break
                        page.wait_for_timeout(1000)
                    page.wait_for_timeout(2500)
                    path = os.path.join(self.shot_dir, f"{shot_name}.png")
                    try:
                        page.screenshot(path=path)
                    except Exception as e:
                        log.warning("Screenshot failed for %s: %s", url, e)
                        path = None
                    return boot, path
                finally:
                    page.close()
            except Exception as e:
                msg = str(e)
                log.warning("Browser error on %s (attempt %d): %s", url, attempt, msg.splitlines()[0][:200])
                if "closed" in msg or "Target" in msg or attempt == 1:
                    self.reset()
                if attempt == 2:
                    raise
                time.sleep(3)

    def links(self, url, contains):
        """Every link on a page whose address contains `contains` (FlightAware's flight history list)."""
        self._ensure()
        page = self._ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            return page.evaluate("(c) => Array.from(document.querySelectorAll('a[href]')).map(a => a.href).filter(h => h.includes(c))",
                                 contains) or []
        finally:
            page.close()

    def read_screenshot(self, path, ident, dep, when):
        """Ask Gemini to read a saved screenshot. Returns a flight dict or None."""
        if not self.client or not path or not os.path.exists(path):
            return None
        from google.genai import types
        with open(path, "rb") as f:
            png = f.read()
        resp = self.client.models.generate_content(
            model=self.gemini_model,
            contents=[types.Part.from_bytes(data=png, mime_type="image/png"),
                      VISION_PROMPT.format(ident=ident, dep=dep or "?", when=when or "?")],
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=VisionFlight))
        v = resp.parsed
        if v is None:
            raise ValueError(f"Gemini returned nothing usable: {(resp.text or '')[:200]!r}")
        return vision_to_flight(v)
