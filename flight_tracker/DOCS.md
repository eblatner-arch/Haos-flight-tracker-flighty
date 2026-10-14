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

## What you get pushed

| When | Example |
|---|---|
| A flight enters the watch window | Tracking UA 1234 EWR→SFO · departs 14:05 · gate C71 · B39M N37502 |
| Gate assigned or changed | UA 1234 gate change: EWR gate C71 → C80 |
| Departure delayed, delay grows/shrinks, back on time | UA 1234 delayed 25 min · now departs 14:30 (was 14:05) |
| Aircraft swap | N37502 → N12345 |
| Inbound aircraft departs / reaches the gate | Inbound UA567 departed ORD · due in 13:20 |
| Inbound running late for your departure | UA567 from ORD due 13:50 — 15 min turn for your 14:05 departure |
| Inbound or your flight cancelled / diverted | |
| Ground stop, ground delay program, delays or closure at either end | EWR: Ground delay program (avg 1 hour) — low ceilings |
| Off, takeoff, landing, on (deadheads and followed flights; working legs optional) | |

Each piece of news is pushed once. Gate and delay pushes replace the earlier
one on your lock screen. Delays, gate changes, tight turns and NAS programs
are time-sensitive on iPhone (turn off with `time_sensitive`).

## The app

- **Next flight**: map with the filed route (dashed), the flown track and the
  aircraft; times in each airport's local time with the original schedule
  struck through; gate, terminal, arrival gate, bags, aircraft and tail;
  countdown or progress bar.
- **Inbound aircraft**: where your airplane is coming from, its ETA, and your
  turn time against `min_turn_minutes`. It's drawn on the map in purple.
- **Upcoming** legs for the next three weeks, **Following** (extra flights from
  `track_flights`), **Recent**.
- **NAS**: ground stops, delay programs and closures at the airports on your own flights in the next `watch_hours`.
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

With a FlightAware AeroAPI **Personal** key in `aeroapi_key`, the add-on asks
AeroAPI for the airline's tail assignment and inbound flight, for every flight
(your legs and followed ones alike):

- once when a flight enters the watch window (12 hours out),
- again at 6 hours, 3 hours and 90 minutes only if no tail was assigned yet,
- and once more whenever the free FlightAware page check links a different
  inbound flight (an aircraft swap).

That's typically 1–4 queries per leg. Spend is counted against
`aeroapi_monthly_budget` (default $5, which the Personal tier includes free); once
a query would go over, it stops until next month. The Tools page shows the month's
spend and every query with its reason and result. Everything else stays on the free
sources. Tail priority: your own transponder, then AeroAPI, then ADS-B from the
airplane's earlier legs, then FlightAware's page.

## Following other flights

Tap **+** to follow any flight: flight number (UA1234 or UAL1234), From and To
airports, and optionally a date (blank means the next departure). The airports
matter because one flight number often flies several legs. Followed flights
appear in the timeline in departure order. You can also list them in
`track_flights` on the Configuration tab with `ident`, `from`, `to` and `date`.
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

A flight is tracked from `watch_hours` before departure until FlightAware shows
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

More than 3 hours before departure every `poll_far_minutes` (30), within 3
hours every `poll_near_minutes` (10), and from 45 minutes before departure
through arrival every `poll_fast_minutes` (3). Each check loads one or two
FlightAware pages, plus one for the inbound aircraft.

## Troubleshooting

- **"No schedule file"**: turn on `save_feed_copy` in ICS2GCAL and let it sync once.
- **A flight shows no data**: FlightAware usually lists a flight a day or two
  ahead. The note under the flight says why (for example "FlightAware doesn't
  list this departure yet").
- **Wrong or missing flight numbers**: set `airline_icao`, or a custom
  `flight_regex` with named groups `dep`, `arr` and `flt`.
