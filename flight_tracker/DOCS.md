# Flight Tracker

A Flighty-style tracker for your own crew schedule, inside Home Assistant.

It reads your schedule from the file the **ICS2GCAL** add-on saves, and from
12 hours before each departure it checks the flight on FlightAware in a real
browser: gate and terminal, delays, the aircraft and tail, the **inbound
aircraft** and how tight your turn is, the filed route, and the live track. It
also watches the **FAA NAS status** for ground stops, delay programs and
closures at your airports, and pulls METARs. Anything that changes is pushed
to your phone.

Open it from **Flights** in the Home Assistant sidebar.

## Setup

1. In the **ICS2GCAL** add-on, make sure `save_feed_copy` is on (version 1.4.0
   or later). This add-on reads that file; it never downloads your schedule
   itself.
2. Set **airline_icao** to your airline's three-letter code (`UAL` for United).
   Schedule titles like `2204 EWR-IAH` or `2204 EWR 27Sep 12:01 - IAH 27Sep 14:50`
   become `UAL2204`. Titles that carry their own code (`DH AA 312 EWR-ORD`) use it.
3. Set **notify_service** to your phone, e.g. `mobile_app_erics_iphone`
   (Developer Tools → Actions → search "notify.mobile_app" to find yours).
4. Start the add-on and open **Flights** from the sidebar.

## Opening the app

Use **Flights** in the Home Assistant sidebar. To open it outside Home Assistant (e.g. as a
home-screen app), set a port next to 8099 under **Network** on the add-on's page and browse to
`http://<your-ha-ip>:<that port>/`. That port has no login, so only use it on your home network.
Port 6080 is the tracker's browser viewer, not the app.

## Notifications

Open the **bell** in the app. Every event the tracker can detect is listed by group
(Departure, Arrival, Status, Aircraft, Inbound, NAS, Weather, Route, Schedule, System), and each has:

- **On / off**
- **Which flights:** My legs, Deadheads, Followed
- **From … h out:** only push once departure is this close (blank = any time). A gate held back
  this way is pushed when the window opens, with the gate at that moment.
- **Urgent:** time-sensitive on iPhone (breaks through Focus)
- Its own thresholds where it has any (delay minutes, minimum turn, gust knots, …)

Changes save as you make them. Tracking itself always runs; this only decides what reaches
your phone. Your old Configuration-tab choices (gate/swap hours, inbound rules, tracking-start
push, delay thresholds) were used as the starting values.

## The app

- **Next flight**: map with the filed route (dashed), the flown track and the
  aircraft; times in each airport's local time with the original schedule
  struck through; gate, terminal, arrival gate, bags, aircraft and tail;
  countdown or progress bar.
- **Inbound aircraft**: where your airplane is coming from, its ETA, and your
  turn time against your Short turn minimum (bell page). It's drawn on the map in purple.
- **Upcoming** legs for the next three weeks, **Following** (extra flights from
  the + button), **Recent**.
- **NAS**: ground stops, delay programs and closures at the airports on the flights you're watching.
- Tap a flight for full times (gate out, takeoff, landing, gate in), METARs
  with flight category, filed route, altitude and speed, its alert history and
  the latest page screenshot.

## Tail number

FlightAware often hides the registration on airline flights, so the add-on
works it out. It follows your airplane's day backwards (your inbound, that
flight's inbound, up to three legs back) to whichever leg is flying, and reads
the registration from its ADS-B broadcast via adsb.lol. In the last 90 minutes
it confirms the tail from your own flight's transponder. You get a push when the
aircraft is first identified, and a swap alert if it changes. The flight page
shows where the tail came from.

A parked airplane doesn't transmit, so for an early first leg the tail may only
appear once the airplane's previous flight is in the air.

## AeroAPI (optional)

With a FlightAware AeroAPI **Personal** key in `aeroapi_key`, every flight (yours
and followed ones) is handled the same way:

1. **Nothing runs** until the watch window (Settings → Start watching, 18 h) before departure: no AeroAPI, no page checks.
2. **First look: AeroAPI.** One query gets the times, gates, the airline's tail
   assignment and the inbound flight.
3. **Then page checks and screenshots** every 30 / 10 / 3 minutes (more than 3 hours
   out / within 3 hours / last 45 minutes), compared against what AeroAPI said.
4. **AeroAPI again only on a change:** the page links a different inbound, or a
   different tail shows up (FlightAware's page or your own transponder). While no tail is
   assigned yet it also retries at 6 h, 3 h and 90 min.

A followed flight with no date gets one query to find its next departure, then waits
until 18 hours before it. Typically 1–3 queries per flight. Spend is counted against
`aeroapi_monthly_budget` (default $5); once a query would go over, it stops until next
month. The Tools page shows the spend and every query. Without a key, page checks do
everything.

## Following other flights

Tap **+** to follow any flight: flight number (UA1234 or UAL1234), From and To
airports, and optionally a date (blank means the next departure). The airports
matter because one flight number often flies several legs. Followed flights
appear in the timeline in departure order. You can also list them in
the + button in the app with the flight number, from, to and an optional date.
NAS programs are only shown and pushed for your own flights, not followed ones. Followed flights get gate, delay, pushback and takeoff pushes.

## Tools

The wrench button opens Tools:

- **Send test notification** to check `notify_service`.
- **Setup check**: the schedule file, how many flights were found, airline,
  screenshot reader, worker status.
- **Flights found in your schedule**, plus calendar events that weren't read as
  flights, so you can see if a title format is missed.
- **Page loads & screenshots**: every FlightAware page the tracker opened, whether
  it found flight data, and a thumbnail of the page (tap for full size).

## When tracking stops

A flight is tracked from the watch window (Settings → Start watching) before departure until FlightAware shows
it has taken off. After that it moves to Recent (or shows Departed under
Following) and isn't checked again.

## Sensors

- `sensor.flight_tracker_next_leg`: status of your next flight, with times,
  gates, aircraft, inbound and problems as attributes.
- `binary_sensor.flight_tracker_problem`: on when a watched flight is delayed,
  cancelled, diverted, has a tight turn, or a NAS program affects it.
- `sensor.flight_tracker_nas_programs`: number of current NAS programs.

They're pushed through the API, so they reappear within a minute of a Home
Assistant restart.

## Where the data comes from

- **FlightAware** flight pages, opened in a real Chromium browser. The add-on
  reads the data the page itself is drawn from (gates, times, aircraft,
  inbound, filed route waypoints, track) and saves a screenshot of each page to
  `/share/flight_tracker/`. If that data can't be read, and you set a Gemini
  API key, Gemini Flash-Lite reads the screenshot instead.
- **FAA NAS status** (nasstatus.faa.gov), **aviationweather.gov** METARs and
  **adsb.lol** live positions — all free, no keys.

If FlightAware shows a CAPTCHA or wants you signed in, set a
`viewer_password`, open `http://<your-ha-ip>:6080/vnc.html`, and handle it in
the tracker's browser. The browser profile is kept, so you only do it once.
Signing in to a free FlightAware account also tends to show more detail.

## Check intervals

More than 3 hours before departure every 30 minutes, within 3
hours every 10 minutes, and from 45 minutes before departure
through arrival every 3 minutes (all adjustable under Settings → Tracking). Each check loads one or two
FlightAware pages, plus one for the inbound aircraft.

## Troubleshooting

- **"No schedule file"**: turn on `save_feed_copy` in ICS2GCAL and let it sync once.
- **A flight shows no data**: FlightAware usually lists a flight a day or two
  ahead. The note under the flight says why (for example "FlightAware doesn't
  list this departure yet").
- **Wrong or missing flight numbers**: set `airline_icao`, or a custom
  `flight_regex` with named groups `dep`, `arr` and `flt`.

## Where settings live

- **Add-on Configuration tab (Home Assistant):** schedule file, airline code, notify service, notification link, AeroAPI key and budget, Gemini key and model, browser viewer, and the advanced flight pattern and time zone.
- **In the app, wrench → Tracking:** watch window, deadheads, check intervals, NAS interval, live ADS-B positions.
- **In the app, bell:** every notification, its window, urgency and thresholds (gate and swap windows, delay thresholds, short turn, inbound settings).
- **In the app, +:** flights you follow.
