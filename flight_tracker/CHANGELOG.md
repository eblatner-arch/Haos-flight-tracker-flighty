# Changelog

## 3.7.9
- adsb.lol: more time for the connection itself (its TLS handshake is slow when it's busy and was timing out at 6 s)

## 3.7.8
- Inbound card: the bar fills once the inbound is at the gate, and it says "At the gate" instead of "Due at"
- Average arrival in words ("usually 15 min early") on the flight, History flight and Search lines; the Stats tile notes that minus is early

## 3.7.7
- Double-tap-and-slide zoom no longer blanks the map: the tiles on screen are scaled while you slide (the way a pinch works) and it settles on a whole zoom level when you let go

## 3.7.6
- adsb.lol reads retry once with a longer wait when adsb.lol is slow (read timeouts), and the last tail seen is kept meanwhile

## 3.7.5
- The lookup log shows why an adsb.lol read failed ("error: …"), for the inbound and your own flight

## 3.7.4
- The earlier-leg check is confirmed against the flight-number history FlightAware already sends with your flight's page (no extra lookups, no AeroAPI): an earlier leg into your airport that FlightAware doesn't show today means adsb.lol's route is stale and it's ignored; one FlightAware shows counts even when adsb.lol has no route

## 3.7.3
- adsb.lol routes are crowd-sourced and can be stale: one is only used when it contains your exact leg (dep→arr)

## 3.7.2
- adsb.lol route lookup sends the headers its docs page sends (a plain request gets an empty "201"), so the earlier-leg check (e.g. DFW-EWR-MCO) actually gets routes

## 3.7.1
- The earlier-leg guard no longer depends on adsb.lol knowing the route: before pushback, an airplane with your flight number that isn't the one your inbound's own transponder shows is not taken as your tail; it goes to AeroAPI as a possible swap instead (the lookup log says so). The route lookup also retries without a position

## 3.7.0
- Your flight's own transponder is only believed when it's really your departure. A flight number can fly an earlier leg into your airport (UA1268 DFW→EWR, then yours EWR→MCO) and that airplane keeps broadcasting the number after landing. adsb.lol's route for the number (DFW-EWR-MCO) shows when there's an earlier leg; then, before pushback, the airplane must be on the ground at your airport and be the same airplane your inbound's transponder shows. The lookup log says why one was ignored
- Once your inbound has left its gate, the airplane can't change without a new inbound number (which FlightAware catches), so the airplane's day isn't re-read from FlightAware pages and Flightradar24 isn't asked any more; adsb.lol keeps following the inbound for the map
- Flightradar24 is off by default (it now shows a Cloudflare "verify you are human" page to the reader); if turned on, a captcha is logged as such instead of "this departure isn't listed"
- Arrival-time alerts only once you're airborne. Before takeoff the departure time carries any real delay, and FlightAware's early arrival estimates jump around
- The airplane on the map sits on its line: live ADS-B fixes extend the drawn track between FlightAware's track updates (yours and the inbound's)
- Maps: double-tap and hold, then slide down to zoom in or up to zoom out; a double tap zooms in a step
- Smoother edge swipe back: the page follows your finger and slides away, and the previous page slides in once it's ready
- Flighty import leaves out flights that haven't flown yet (and removes any an earlier import brought in)
- History: a flight without a track has "Copy for track backfill"; Search has "Copy N without a track for backfill". Paste into the Backfill tracks workflow's new flights box to fetch just those

## 3.6.1
- Search: From, To, Either airport, Route and Tail are typed boxes with suggestions (a partial code matches what starts with it; routes as EWR-FLL, EWR FLL or EWRFLL; tails with or without the N). Month is Jan–Dec, combined with Year when you pick one

## 3.6.0
- History → Search: type a flight number, airport, route (EWR-FLL), tail, aircraft, month or day, or `>30` / `<-5` for minutes late/early; tap Late 15+/30+/60+, On time, Early, Diverted, Cancelled, No track; More filters for year, month, from, to, either airport, route, aircraft, tail, departure time, day of week, a delay range, and sort (newest, oldest, latest, earliest, longest). Shows the count, on-time %, average delay and distance of what matches
- Every stat is a link: tap a route, airport, aircraft, tail, time-of-day bar, On time, Early, 30+ late or diverted and see those flights
- Back (‹ or the edge swipe) returns to the page you came from at the same scroll position, filters included, instead of the top
- The Drive pill turns amber the moment you change something (flight added or removed, settings, alert rules, imports), then green once the upload is done

## 3.5.6
- Imported tracks mark the flight "flown track saved" and are saved to History right away

## 3.5.5
- Drive pill updates on every page (it stayed amber on Settings after the backup had gone up)

## 3.5.4
- Importing a tracks .zip that got cut off in the download (iOS Safari does this) keeps every complete track in it and says the rest is missing, instead of failing with "File is not a zip file"

## 3.5.3
- Backup card text matches the new timing

## 3.5.2
- Drive backup goes up about 15 s after any change: a flight added, removed or finished, History imported or deleted, alert rules or settings changed (a burst of edits is one upload). Failed uploads retry after 2 min; the daily backup still runs
- Deleting a lot of History keeps the previous Drive backup as "Flight Tracker backup before delete <date> (<n> flights).zip", so a mistaken delete can be restored
- Never writes into a file or folder that's in the Drive trash (or was moved out of the folder): it checks first and makes a fresh one

## 3.5.1
- Drive pill next to Live: green when backed up, amber when changes are waiting (uploads within 10 min), pulsing while uploading, red on a problem. Tap it for Settings → Backup. On narrow phones it's just the cloud icon

## 3.5.0
- History → Import also takes the tracks .zip from the flight-track-history Backfill tracks run (the GitHub artifact download works as is). Adds the flown track to each past flight that doesn't have one

## 3.4.0
- History → Export & delete: delete just the flights imported from Flighty (to re-import from scratch), delete all of History, or export a CSV in Flighty's columns
- Google Drive backup (Settings → Backup), same setup as the OOOI Flight Logger: your own Google client (the logger's works), Connect Google with a code at google.com/device. Uploads "Flight Tracker backup.zip" (all of History with flown tracks, plus your alert and tracking settings) and "Flight history.csv" within 10 min of a History change and once a day
- Restore from Drive or from a downloaded backup file; restoring adds what's missing and keeps what's here
- New options in the Configuration tab: google_client_id, google_client_secret, drive_folder

## 3.3.0
- Diversions are one journey: the diverted flight's lateness is when you finally reached the planned airport (via the continuation leg) against the original schedule, e.g. FLL→EWR diverted to IAD, into EWR 7h 26m late. The continuation leg (IAD→EWR) shows actual times only, links back, and no longer counts as its own 446-min-late flight in stats or route history. A diverted flight's distance is to the airport it actually landed at
- Add a past flight: the date can be up to 10 days back. AeroAPI finds it, one FlightAware page load adds the track and gates, and it goes straight into History (Mine)
- The date field no longer runs off the right edge on iPhone
- Live updates reconnect right away when you come back to the app, instead of waiting for the old connection to time out; a dead connection is also noticed within about 35 s

## 3.2.2
- Your calendar is read 60 days ahead instead of 3 weeks (Settings → Watch window → Show schedule ahead, 7–180 days). Showing far-off flights costs nothing: checks still only start at the watch window

## 3.2.1
- No changes (test update for diagnosing Home Assistant updates)

## 3.2.0
- ADS-B on your inbound: while it's taxiing or flying (or due to be), its transponder is read every 3 min (Settings → Tracking → Tail and position sources; 0 turns it off). A different tail than the one we have triggers a full check right away, and the airplane actually flying your inbound now wins over Flightradar24 and AeroAPI
- Lookup log shows what each tail source said on every check, even when nothing changed: Flightradar24 (tail, "no tail listed yet" or "not listed") and adsb.lol (your inbound, earlier legs, your own flight near departure). adsb.lol and Flightradar24 have their own tags
- Settings and Notifications are grouped into collapsible sections (they remember open/closed); the Notifications groups show how many alerts are on

## 3.1.0
- Tail swaps without a new inbound flight number: Flightradar24's schedule page for your flight number (it lists the airline's tail assignment for upcoming flights) is checked every 15 min from the watch window until pushback (Settings → Tracking; 0 turns it off). Calendar and Manual flights only
- It also runs right away when FlightAware shows a different aircraft type (e.g. 739 → B39M), a different inbound, or a different airplane flying into your inbound
- The airplane flying in on ADS-B is now used even when AeroAPI already gave a tail, so a swap on the same inbound number shows as soon as that airplane is in the air
- Which tail wins: your own transponder; then the newest of Flightradar24, your own earlier leg on a turn, and AeroAPI; then the inbound seen on ADS-B (when Flightradar24 has no tail yet). AeroAPI is only re-asked when none of the free sources explain a change
- Flightradar24 checks appear in Settings → page checks with a screenshot

## 3.0.0
- **History tab** (bottom bar): every Calendar and Manual flight is saved when it finishes, with gate and runway times, delays, gates, aircraft, tail and the flown track. Friends' flights aren't saved
- **Stats**: flights, distance, hours, airports, on-time rate, average delays, delay by time of day, most delayed routes, most-flown routes/airports/aircraft/tails, records. By year or all time
- **Flighty import**: History → Import, pick the .xlsx or .csv export. Times are read in each airport's local time; importing again is safe
- **Notification history**: History → Notifications, searchable and grouped by day. The Flights screen only shows the last 12 hours of alerts and the last 18 hours of finished flights
- **Calendar / Manual / Friends** replace My legs / Deadheads / Followed. Flights added with + are yours (Manual, tracked and kept) or a friend's; switch on the flight page. Notification switches carry over: My legs → Calendar, Deadheads → Manual, Followed → Friends. Deadheads in the calendar are always tracked
- **Tracking continues after takeoff** until the arrival gate, every 10 min in the air (Settings → Tracking) and every few minutes near landing. New Landed and At-the-gate alerts
- **Turns**: when your own earlier flight brings the airplane (EWR–MIA–EWR), a tail change found on that leg now carries to the return leg without another AeroAPI call
- Live flight page shows how often you've flown the route and that tail
- Bottom tab bar: Flights, History, Alerts, Settings
- Airports renamed in some apps (DJT for West Palm Beach) are recognised

## 2.12.2
- Pushes carry a custom icon (white airplane on the app's indigo): shows on Android now, and on iPhone once the Home Assistant iOS app ships custom notification icons (planned for 2026.8). Older apps ignore it

## 2.12.1
- Swipe in from the left edge to go back (to wherever the page's ‹ link goes). The page follows your finger; let go past about a third of the way to go back. Works over the map too

## 2.12.0
- The add-on Configuration tab only has setup and API items now: schedule file, airline code, notify service and link, AeroAPI key/budget, Gemini key/model, browser viewer, and the advanced pattern/time zone
- New Tracking settings in the app (wrench page): watch window, deadheads, check intervals, NAS interval, live ADS-B positions. Your current values carry over
- Notification settings that were also in the config (gate/swap windows, delay thresholds, turn minimums, progress and urgency switches) live only on the bell page, where they already were. Followed flights are added with +

## 2.11.14
- Map dragging is now switched on per touch: only a touch that lands inside a map's visible box can drag it; any other touch turns that map's dragging off before the map sees it

## 2.11.13
- Maps show one flight plus its own inbound only: your flight's map shows the inbound, and the inbound's page shows the leg before it (if that hasn't arrived). No more whole chain on one map

## 2.11.12
- Fix (again): a swipe starting just below a map could still drag it on iPhone. Touches that start outside the map's visible box are now ignored by the map before it can react, and the route layer is no bigger than the map

## 2.11.11
- Fix: a swipe starting just below a map still dragged it on iPhone. Leaflet's route layer is drawn 10% larger than the map and was catching those touches; routes, markers and labels now ignore touches (dragging the map itself is unchanged)

## 2.11.10
- One finger moves the map again (2.11.9's two-finger mode is gone)
- Fix: swipes that started below a map could drag the map. Maps are now hard-clipped to their box, so the off-screen part of the map can't catch touches

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
