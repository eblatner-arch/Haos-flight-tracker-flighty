# Changelog

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
