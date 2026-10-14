# Changelog

## 2.11.9
- On phones, scrolling over a map scrolls the page instead of dragging the map away. Move the map with two fingers (pinch to zoom still works); a one-finger drag shows a short "Use two fingers" hint

## 2.11.8
- A followed flight that isn't found says so: status "Not found", no made-up times or 0m duration, and a banner saying where the flight number actually flies ("UA 1859 flies IAH→AUS, not HOU→AUS")

## 2.11.7
- Maps show the airplane's earlier legs that still matter: your flight's map adds the inbound's inbound (and further back) while they haven't arrived, and each inbound page shows the leg before it. Stops at the leg the airplane is flying now; arrived legs aren't drawn
- Map tile seams: base-map tiles overlap by half a pixel (drawn normally, so no bright lines) to cover Safari's intermittent hairlines

## 2.11.6
- Delay trends only count where they affect the flight: departure delays at the airport you leave, arrival delays where you land (or where your inbound is still headed). An ORD departure delay no longer shows or pushes for EWR→ORD
- Delay-trend cards on the Flights screen say which flight they're for when you're watching more than one
- Weather: airports on a newly added or newly watched flight get their METAR within a minute instead of waiting up to 10; the flight page says "no METAR yet" instead of leaving the airport out

## 2.11.5
- "Inbound departure delayed" is now "Inbound arriving late": it compares the inbound's estimated arrival at your airport with its scheduled arrival, not when it leaves its origin. The push includes the expected turn

## 2.11.4
- Fix: the add-on crashed on start after 2.10.1 (KeyError max_sched_turn) while reading the removed short-turn setting

## 2.11.3
- FAA delay trends at your airports show at the top again, as a quiet gray card with an amber edge (below any ground stop or program), not as a red alert

## 2.11.2
- Ground stops and delay programs hold flights headed to that airport, not ones leaving it. A program at your departure airport only matters for an inbound that hasn't taken off; the card and push say "doesn't hold departures from EWR"
- A program that doesn't hold you or your inbound is still shown at the top, but toned down (gray card, amber label) instead of red
- FAA departure/arrival delays are shown as "delay trend" (avg 15 min and increasing), aren't treated as a problem with your flight, and no longer push by default (switch them back on under Delay trends)

## 2.11.1
- Ground stops: an inbound (or your own flight) that's already airborne isn't held, so the card says "already airborne — not held" in green instead of "includes", and its center isn't highlighted

## 2.11.0
- NAS status now comes from the FAA's live airport-events feed (the same data nasstatus.faa.gov shows), including ground-stop scope, probability of extension and the advisory link. The older XML feed is the fallback
- Ground stops and delay programs show at the top of the Flights screen and the flight page, with the departure centers in scope. Your departure's (or your inbound's) center is highlighted, and the card says whether the program includes it — or doesn't include it yet
- Every ground stop at either airport pushes, whatever its scope; the push says whether it includes your departure or inbound. A ground stop or delay program also pushes when it's extended or expanded to more centers
- NAS applies to followed flights too (each NAS event has a Followed switch on the Notifications page, on by default)

## 2.10.2
- Short turn: a turn that was skipped because of a setting no longer counts as already sent, so changing the setting pushes it on the next check

## 2.10.1
- Flight page shows times the same way as the Flights screen: big Departs/Arrives with green (early) or amber (late) and the scheduled time struck through; takeoff and landing below in the same style
- "Inbound running late" is now "Short turn": it pushes whenever the turn is under your minimum, even if the inbound is on time or early and however long the scheduled turn was. The banner reads "Short turn: 58 min"

## 2.10.0
- The first AeroAPI lookup is followed immediately by the FlightAware page check (comparison and inbound aircraft) instead of waiting for the next cycle, so the inbound shows up right away

## 2.9.9
- Fix: changing pages reloaded the whole app (blank "Loading…" flash). Navigation now stays inside the page and never changes the address, so Home Assistant doesn't reload the panel
- The Flights map draws immediately from the last data instead of appearing blank first

## 2.9.8
- Fix: the screen blinked on every live update. Updates now patch only what changed in place, so the page, scroll position and maps stay still

## 2.9.7
- Fix: light lines across the map. Tiles no longer use Leaflet's plus-lighter blending (which brightened tile overlaps), the earlier overlap tweak is removed, and the map background matches the tiles

## 2.9.6
- Flight card says Departed once the airplane pushes back, and Landed / Arrived on the arrival side

## 2.9.5
- Fix: AeroAPI was queried a second time because FlightAware's page and AeroAPI write the same inbound's ID differently.
  The inbound is now compared by flight number and scheduled departure, and there's no re-query when the page shows
  the same tail AeroAPI gave. The log names both inbounds when a real change triggers one
- LIVE indicator in the header: green while the page is receiving live updates, pulses on each one

## 2.9.4
- Fix: long flights lost the start of their track (only the most recent points were kept). Tracks are now thinned evenly
  over the whole flight; legs saved the old way are fetched once more

## 2.9.3
- Fix: an earlier leg saved while it was still flying could freeze with half a track. A leg is now only frozen once it
  has been fetched after it arrived
- "Arrived" now needs an actual arrival time or a status that says Arrived/Landed
- Each leg of the aircraft's day shows when its data was last fetched

## 2.9.2
- Fix: tapping a push opened the default dashboard. It now opens the Flights panel (the same address as the sidebar entry),
  with a fallback when the add-on can't look itself up, and a `notification_url` option to set it by hand
- Tools shows where tapping a push will take you

## 2.9.1
- Check interval uses the earlier of scheduled and estimated departure, so a delay never slows checks down
  (e.g. 3-minute checks start 45 min before the original departure even if it's now 1.5 h late)
- Fix: hairline seam across the map on iPhone; map labels moved to their own layer

## 2.9.0
- Live page: the add-on streams a signal whenever anything changes and the open page updates in place — no reloading
  (falls back to checking every 30 s if the stream can't get through)
- Maps keep your zoom and pan through updates; only the aircraft, tracks and routes are redrawn
- Map zoom and center buttons no longer draw over the header

## 2.8.0
- Tap the inbound card to follow the airplane's day backwards: your inbound, its inbound, and so on, each with times,
  gates, delay and a map, plus a strip of the whole chain to jump between legs
- The chain stops at the first leg that has arrived (or that the airplane is flying right now); finished legs are never
  reloaded, open ones refresh every 10 minutes

## 2.7.2
- The viewer port (6080) opens the browser viewer directly instead of a file listing
- Optional direct port for the app (8099, off by default) for use outside the Home Assistant app

## 2.7.1
- New icon and logo (add-on store, browser tab, home-screen icon)

## 2.7.0
- New Notifications page (bell button): 37 events, each with on/off, which flights (my legs / deadheads / followed),
  how close to departure it starts pushing, urgent (time-sensitive) and its own thresholds
- New events: terminal change, earlier than scheduled, reinstated after cancel, aircraft type change, inbound departure
  delayed, different inbound linked, per-type NAS (ground stop / GDP / delays / closure / other), IFR and gust alerts,
  filed route change, crew schedule changes (next 3 days), AeroAPI budget, page checks failing, schedule file stale
- Held-back gate pushes catch up when their window opens, with the gate at that moment
- Existing Configuration-tab notification settings become the starting values; the page is where to change them now

## 2.6.3
- Aircraft swap pushes only from 2 hours before departure (`swap_alert_hours`); swaps earlier are tracked and shown in the lookup log

## 2.6.2
- The "aircraft identified" push only goes out when "Alert when tracking starts" is on, and never for flights you follow; aircraft swaps still always push

## 2.6.1
- Fix: flights removed (or added) right before an update or restart came back (or went missing); changes now save immediately and on shutdown

## 2.6.0
- Map: center-on-route button
- Flight page: lookup log at the bottom — every check, which sources it used (AeroAPI, FlightAware page, inbound page,
  ADS-B, Gemini) and exactly what changed; checks that change nothing are grouped

## 2.5.4
- A cancelled or diverted inbound always pushes, whatever the scheduled turn

## 2.5.3
- Fix: a tail found by the first AeroAPI lookup showed as "not known yet" on the flight page

## 2.5.2
- Inbound pushes (departed, running late, at the gate, cancelled) only when the inbound is scheduled to land within
  60 minutes of your scheduled departure (`inbound_alert_minutes`); the inbound is still tracked and shown in the app

## 2.5.1
- Gate pushes wait until 2 hours before departure (`gate_alert_hours`); gates are still tracked and shown all along,
  the first push carries the current gate and later changes push immediately. Delays, cancellations, diversions,
  swaps and inbound problems push whenever they happen

## 2.5.0
- Nothing runs for a flight until 18 hours before departure (new default `watch_hours`: 18), then the first look is AeroAPI
- Page checks and screenshots start right after, as the comparison; AeroAPI is asked again only when they show a different
  inbound or a different tail (or, while no tail is assigned, at 6 h / 3 h / 90 min)
- Followed flights without a date: one AeroAPI query finds the next departure, then nothing until 18 hours before it

## 2.4.1
- AeroAPI and tail-number lookup apply to followed flights too, with the same rules
- The first AeroAPI query waits until a flight is within the watch window, even for flights added days ahead

## 2.4.0
- Optional AeroAPI: one query when a flight enters the watch window for the airline's tail and inbound flight;
  retries at 6 h, 3 h and 90 min only while no tail is assigned; one more whenever the free page check links a different inbound
- Monthly budget cap (default $5) with spend and every query shown on the Tools page; stops calling instead of billing past the cap
- Tail priority: your own transponder, then AeroAPI, then the airplane's earlier legs on ADS-B, then FlightAware's page

## 2.3.1
- Screenshots are deleted 48 hours after a flight is done (or when it leaves the list)
- Tools shows Gemini use (today, this month, last call) and screenshot storage; flights read by Gemini carry a GEMINI tag

## 2.3.0
- Adding a flight asks for From and To (and an optional date), so a flight number that flies several legs picks the right one
- One timeline in departure order: your legs and followed flights together, in Flighty-style cards with Today / Tomorrow headings
- NAS status and NAS alerts only for airports on your own flights in the watch window (not followed flights); the NAS sensor counts only those
- `track_flights` entries accept `from`, `to` and `date`

## 2.2.0
- Finds your tail number without FlightAware showing it: follows the airplane's day back
  (inbound, its inbound, …) to the leg that's flying and reads the registration from ADS-B (adsb.lol)
- Confirms it from your own flight's transponder in the last 90 minutes; a different airplane triggers the swap alert
- Push when your aircraft is first identified; the flight page shows where the tail came from and the airplane's day

## 2.1.1
- Stops re-checking the inbound aircraft once it's at the gate; looks again only if your tail number changes or a different inbound is linked

## 2.1.0
- Follow any flight from the app (+ button): flight number and optional date; remove it any time
- Tracking stops once a flight has taken off
- Map tiles are free with no API key (Esri gray canvas, OpenStreetMap fallback)
- Tools page (wrench button): send a test notification, check setup, see which calendar events were read as flights, and every page load with its screenshot
- The image is built on GitHub and downloaded, so installs and updates no longer build on your device

## 2.0.1
- Much smaller image: Python slim base with only Chromium instead of the full Playwright image, so installs finish faster

## 2.0.0
- Rebuilt as a full flight tracker app for your crew schedule (Home Assistant sidebar panel)
- Reads the schedule from the file ICS2GCAL saves; starts watching each leg 12 hours before departure
- Gate, terminal, delay, aircraft swap, cancellation and diversion alerts pushed to your phone
- Inbound aircraft tracking with turn-time warnings
- FAA NAS status (ground stops, ground delay programs, delays, closures) at your airports
- Map with the filed route, flown track, live aircraft and inbound aircraft
- METARs with flight category
- Follow extra flights by number
- Reads FlightAware's page data directly; Gemini Flash-Lite reads the screenshot only as a fallback (API key now optional)
- Sensors: next leg, problem, NAS programs

## 1.2.0
- Default model is now `gemini-flash-lite-latest`
- Browser restarts automatically if Chromium crashes

## 1.1.0
- Live noVNC browser viewer on port 6080
- FlightRadar24 flight and aircraft sources

## 1.0.0
- Initial release
