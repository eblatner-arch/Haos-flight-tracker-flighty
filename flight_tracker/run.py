import datetime
import json
import os
import re
import sys
import time
from typing import Optional

import requests
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright
from pydantic import BaseModel

OPTS = json.load(open("/data/options.json"))
API = "http://supervisor/core/api/states/"
HDRS = {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}",
        "Content-Type": "application/json"}
SHOT_DIR = "/share/flight_tracker"
PROFILE_DIR = "/data/browser_profile"
os.makedirs(SHOT_DIR, exist_ok=True)

URLS = {
    "flightaware":   "https://www.flightaware.com/live/flight/{}",
    "fr24_flight":   "https://www.flightradar24.com/data/flights/{}",
    "fr24_aircraft": "https://www.flightradar24.com/data/aircraft/{}",
}

PROMPT = """This is a screenshot of a flight tracking page. Extract the
current or most recent flight's details. Use airport ICAO/IATA codes for
origin/destination and include time zones with times when shown. Use null
for anything not visible. Keep status short (e.g. 'Scheduled', 'En Route',
'Landed', 'Delayed', 'Cancelled'). If the page shows an error, CAPTCHA, or no
flight, set status to 'Unavailable'."""


class FlightInfo(BaseModel):
    status: str
    flight_number: Optional[str] = None
    registration: Optional[str] = None
    aircraft_type: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    departure_scheduled: Optional[str] = None
    departure_actual: Optional[str] = None
    arrival_scheduled: Optional[str] = None
    arrival_estimated: Optional[str] = None
    delay_minutes: Optional[int] = None
    altitude_ft: Optional[int] = None
    groundspeed_kt: Optional[int] = None
    progress_percent: Optional[int] = None


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def push(entity, state, attrs):
    # HA rejects states longer than 255 characters
    requests.post(API + entity, headers=HDRS, timeout=10,
                  json={"state": str(state)[:255], "attributes": attrs}).raise_for_status()


def capture(ctx, url, path):
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.get_by_role("button", name=re.compile("accept|agree", re.I)).first.click(timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(6000)
        page.screenshot(path=path)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        page.close()


def analyze(client, png):
    resp = client.models.generate_content(
        model=OPTS["gemini_model"],
        contents=[types.Part.from_bytes(data=png, mime_type="image/png"), PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=FlightInfo))
    if resp.parsed is None:
        raise ValueError(f"Gemini returned no parseable data: {resp.text!r}")
    return resp.parsed


def track(ctx, client, flight):
    ident = flight["ident"].strip()
    s = slug(ident)
    source = flight["source"]
    url = URLS[source].format(ident.lower() if source.startswith("fr24") else ident.upper())
    info = analyze(client, capture(ctx, url, f"{SHOT_DIR}/{s}.png"))
    attrs = info.model_dump(exclude={"status"})
    attrs.update(friendly_name=f"Flight {ident.upper()}", icon="mdi:airplane",
                 tracked_ident=ident.upper(), source=source, source_url=url,
                 screenshot=f"{SHOT_DIR}/{s}.png",
                 last_updated=datetime.datetime.now().astimezone().isoformat())
    push(f"sensor.flight_{s}", info.status, attrs)
    log(f"{ident}: {info.status}")


def run_cycle(p, client):
    """One browser session; returns normally only on a fatal browser error."""
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,  # runs on Xvfb so the noVNC viewer can show it
        viewport={"width": 1366, "height": 900},
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )
    try:
        while True:
            for f in OPTS["flights"]:
                try:
                    track(ctx, client, f)
                except Exception as e:
                    log(f"{f.get('ident')} error: {e}")
                    if "Target page, context or browser has been closed" in str(e):
                        return
                time.sleep(10)
            time.sleep(OPTS["interval"])
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def main():
    if not OPTS.get("gemini_api_key"):
        log("gemini_api_key is not set. Add it in the add-on Configuration tab.")
        sys.exit(1)
    if not OPTS.get("flights"):
        log("No flights configured. Add at least one under 'flights'.")
        sys.exit(1)
    client = genai.Client(api_key=OPTS["gemini_api_key"])
    log(f"Tracking {len(OPTS['flights'])} flight(s) every {OPTS['interval']}s "
        f"with {OPTS['gemini_model']}")
    with sync_playwright() as p:
        while True:
            try:
                run_cycle(p, client)
            except Exception as e:
                log(f"Browser error: {e}")
            log("Restarting browser in 30s")
            time.sleep(30)


if __name__ == "__main__":
    main()
