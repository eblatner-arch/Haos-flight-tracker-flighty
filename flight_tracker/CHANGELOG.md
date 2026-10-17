# Changelog

## 3.21.10
- EDCT changes after a program's first slot list: a controlled departure time is now read from any FAA message shape, not only a delay program's full slot list or a flight block. The flight is the nearest part of the message naming exactly one callsign (a list naming several around one time is skipped), and the program's airport is taken from above it when the flight's own part doesn't name it. UA2677 BNA→EWR got its first EWR EDCT (22:12Z) from the 16:58Z slot list; the later moves to 22:16Z and 22:18Z were never picked up, and the restart for 3.21.9 wiped the captured messages that would show what carried them.
- The messages about your flights (the last 4 of each type) and a new EDCT trail (the last 10 messages per flight that carry a controlled time or slot) are kept on disk, so an update or restart no longer wipes them before the samples are downloaded. In the download as your_flights/edct_trail/<callsign>_<time>_<type>.xml; summary.json lists the trail and how many flights hold an EDCT from each program.

## 3.21.9
- No second push for the same ground stop or delay program when it comes back under another key (from the FAA's SWIM feed vs the NAS status page, or rebuilt after a restart): one program per airport and kind seen in the last 2 h is the same program. Extensions and added centers still push.

## 3.21.8
- SWIM samples download: for every TFMS message type, the last 4 messages that mention one of your flights (your_flights/by_type/), so what the FAA sends about a flight around an EDCT change can be checked.

## 3.21.7
- One route row in Details: the clearance's route once the tower has sent it ("Route (cleared)"); else ATC's; else the filed one. The separate "Cleared route" row is gone.
- "Route (FAA) / FlightAware shows · an older filing" only when the FAA's path really misses the filed route's fixes. Airways and procedures are left out of the comparison (the FAA lists every fix along Q87; the filed route just says Q87), which wrongly flagged UA1643's route.

## 3.21.6
- The EDCT no longer replaces the takeoff estimate: the estimate stays the tower's (or FlightAware's), and the EDCT shows under it in amber ("EDCT 14:59 · EWR program"), so a tower prediction later than the slot is visible next to it.

## 3.21.5
- Lookup log: a clearance message that repeats the clearance with one thing added (the crew's ROGER, a revised EDCT) logs only that ("MCO clearance by datalink: crew answered roger").

## 3.21.4
- Datalink clearances are matched to your flight by the clearance text too (UAL1643 or UA1643), not only by the message's aircraft field: UA1643's MCO clearance never reached the flight while the airline's delivery note did.
- Every clearance message for your flights is kept apart (last 30) and included in the SWIM samples download as your_flights/clearance_NN.xml, so they're not pushed out by the busy tower feed.

## 3.21.3
- Airport layouts download in the background for every airport of a flight being watched (yours, its destination, the inbound's origin), one at a time, so a map opens with its runways and gates already there. Saved layouts are read without waiting on a download in progress, and one airport's download no longer holds up another's.

## 3.21.2
- When the FAA's expected path doesn't follow the route FlightAware shows, the FAA's is shown as the route ("Route (FAA)", its fixes in order) and FlightAware's underneath as an older filing. Checked on UA1102 MIA→EWR: United's flight plan matched the FAA's inland path fix for fix.

## 3.21.1
- Details shows "FAA's path": the fixes on the FAA's own expected trajectory (the dashed line on the map), marked "(differs)" when most of them aren't in the filed route shown above (UA1102 today: FlightAware shows the offshore Y299 filing, the FAA's system has it inland via Q87/Q99).

## 3.21.0
- EDCTs from the FAA's FADT: the full slot list of a ground delay program, re-sent with every revision (where the EWR program's EDCTs came, e.g. UA1643 MCO→EWR). Read for every flight, kept like the others.
- Ground stops and delay programs are attached to a flight only when their time touches it: your flight between its departure and arrival overlapping the program's hours (or your inbound's, for a program at your departure airport). Programs without hours only for flights in the next 12 h. Before, every flight to EWR in the next 60 days showed on the program's map.
- A ground stop or delay program is hidden for a flight once it can't hold it anymore: your flight (and the inbound it would hold) already in the air or done.

## 3.20.13
- The inbound's (and earlier legs') dashed planned route is drawn only until that leg lands; after that only the track it actually flew shows.

## 3.20.12
- Smoother tracks: below 18,000 ft (or under 250 kt) a track point is kept every 5 s from FAA approach and surface radar (15 s at cruise), so departures and approaches follow the real turns; and the drawn line rounds the corners between points.

## 3.20.11
- Lookup log: a check where no source saw the tail (adsb.lol not answering) no longer makes the known tail show again on the next check. Each tail shows once; each reason for no tail shows once.

## 3.20.10
- Lookup log: a transponder or clearance line shows only when it says something new about that airplane (a different tail, or a different reason for none), whichever source saw it. Checks that add nothing fold into the previous entry's count.

## 3.20.9
- Airplane pointing backwards: when FlightAware's track had a newer point than the FAA radar fix, the direction was taken from that newer point back to the older fix. The airplane now sits at the newest known position and points along the track from an earlier point; it's only carried forward when the radar fix is the newest.
- Datalink clearances checked against 40 towers: the delivery notes without the dot ("DAL425 001 N193DN KLAX G133", Delta, Southwest, American) now give the airplane and gate; gates read without the leading G (E6, not GE6); "MAINTAIN 5000FT", "XPCT RWY" / "DEP RWY", "DPFRQ SEE SID", remarks like @MEDEVAC and the "./." route shorthand are understood.
- A delay program cancel ("DISREGARD EDCTS") clears the EDCTs it had assigned, and the flight's EDCT with a line in the log.

## 3.20.8
- The datalink clearance's airplane is logged once (and again only if it changes), not on every check.
- The ZNY label sits over Pennsylvania instead of the New Jersey coast.

## 3.20.7
- Ground stop / delay program maps: each center is drawn from all of its FAA boundaries (low and high altitude), filled once where they overlap. Only one was drawn before, which left part of New York center (ZNY) uncovered. The boundaries are fetched again once.

## 3.20.6
- Datalink clearances in the flight-strip format (United at SFO) are read: cleared route, SID and transition, climb via SID / expected altitude, departure frequency, runway, and the EDCT printed after the destination. The airline's delivery note that follows ("UAL444 001 Y C .N14214 P1527 GE6") gives the airplane and gate, so the PDC tail source works there too.
- An EDCT in the clearance becomes the flight's EDCT when the FAA's own assignment didn't reach the app.
- CPDLC clearances with an intermediate clearance limit ("CLEARED TO LCU VIA … AFTER LCU CLEARED TO KIAD") name the destination, not the fix.
- A clearance saved before this version is read again.
- The SWIM samples download keeps one clearance per tower and airline (up to 40), to check the other formats.

## 3.20.5
- Swap on the inbound that keeps its flight number, without adsb.lol: the 5-second position loop records the inbound's tail whenever FAA approach radar sees it (climbing out of its origin, arriving at yours), and a new tail triggers a check of your flight at once.
- Once the inbound has left its gate, the tail seen on it stays the answer for the rest of its flight (its airplane can't change in the air), instead of expiring after 20 min while en route, where the FAA's center radar carries no tail.
- The inbound's own datalink clearance at its origin also names its airplane.

## 3.20.4
- The FAA datalink clearance (PDC) is a tail source: it names the airplane (/AN N12345) for your flight at your departure airport, usually 30-60 min before departure. Trusted next after your own transponder; if it differs from what the app had, you get the aircraft swap alert ("from FAA datalink clearance"). A clearance from another day's flight with the same number is ignored. Airports without PDC simply don't add this signal.
- adsb.lol's route lookup also waits out the pause after adsb.lol stops answering.

## 3.20.3
- The airplane points the way it's flying when the position comes from FAA center radar (which sends no heading): the direction is taken from its track.
- Between the FAA's once-a-minute en-route fixes, the map carries the airplane forward at its speed and heading (up to 2 min), so it moves smoothly without extra lookups.
- adsb.lol is asked only when the FAA radar has nothing from the last 90 s (outside the US, or a gap), at most once a minute per airplane.

## 3.20.2
- EDCTs are kept for every flight, not only the ones being watched. The FAA assigns them once, when a ground delay program is issued (UA444 SFO→SAN got its EDCT from the SAN GDP at 06:51 PDT, before it was being watched), and doesn't send them again. Kept on disk for 12 h (written at most every 2 min), so a restart doesn't lose them; a flight you start watching picks up its EDCT at once.
- EDCTs also read from flight-data messages that carry a controlled departure time.
- When the departure tower's system shows a flight is held for an EDCT but the time hasn't reached the app, Details shows "EDCT: assigned".
- The SWIM samples download now includes examples of every message that carries an EDCT.

## 3.20.1
- The big map on the Flights screen now moves the airplane every 5 s too (before, only the flight page did; on cellular the Flights screen could sit still for minutes).
- A position request that hangs on a weak signal is dropped after 8 s instead of stalling the updates.
- adsb.lol not answering no longer holds up the FAA radar positions: it's asked on its own thread with a short timeout, and after a failure the app waits (20 s, doubling up to 5 min) before asking again.
- The lookup log says "adsb.lol not answering" instead of a raw connection error.

## 3.20.0
- Live position every 5 s: airplanes taxiing, in the air or landing (yours, and your inbound in the air) move between flight checks. Near airports from FAA approach and surface radar (every few seconds, no extra network); en route from adsb.lol every 30 s (the FAA's center radar only reports about once a minute). The flight page fetches just the new position and track points and repaints the map in place.

## 3.19.3
- Faster loading: the flights list no longer carries each flight's SWIM route drawing, clearance text and time sources (the flight page loads them); a flight page no longer downloads the tracks of the airplane's earlier legs (only the aircraft page does) and shows the latest 40 lookup-log entries with a "Show older" button. A typical flight page is about a third of its old size.
- Live updates redraw at most every 2.5 s, so frequent SWIM changes don't make the phone stutter.

## 3.19.2
- A ground stop or delay program the FAA cancels (SWIM) now disappears right away and stays gone: the NAS status page lags several minutes and was bringing it back. A new program at that airport still shows.
- Fix: back swipe after opening a program map could land on that map again (the map link changed the page address); it's now an in-app step like every other link. The program map draws on canvas, so it no longer stalls while loading.

## 3.19.1
- Fix: the ground stop / delay program map broke with "Map container is already initialized" when a live update re-drew the page; the map is now kept across updates.

## 3.19.0
- Ground stop and delay program cards open a map ("Map ›"): the en-route centers whose departures it holds are shaded (red for a ground stop, amber for a delay program) with their names, the program airport is marked, and your departure (or your inbound's) shows held, not held or airborne. Center boundaries come from the FAA's aeronautical data, fetched once and kept for 90 days.

## 3.18.0
- EDCTs from FAA SWIM: when a ground delay program or ground stop gives your flight an expected departure clearance time (or changes it), you get a push ("UA 698 EDCT 23:45 · SFO→EWR · EWR program · 40 min after scheduled takeoff"), the takeoff estimate shows it ("from FAA EDCT"), and the details show the EDCT, the earlier one and the arrival slot. New "EDCT" notification under NAS, on by default.

## 3.17.3
- SWIM ground stops: delays so far and the FAA's expected average (checked against tonight's real LAS ground stop).

## 3.17.2
- Lookup log: takeoff and landing estimates (and the landing time) are tracked too, so when SWIM moves them the log shows the change and where it came from ("Takeoff estimate 22:53 → 22:57 via SFO tower").
- Map: the XPDR label sits centered in its button.

## 3.17.1
- Map: once the inbound has arrived (or your flight has left the gate), centering frames only your own route; the inbound's line stays, faded.
- Map: surface traffic is drawn from zoom 12 in, so zoomed out its callsign tags don't pile up over the route.

## 3.17.0
- NAS status woven from two sources: ground stops and ground delay programs come from FAA SWIM the moment they're issued, extended or cancelled (with average/max delay, reason and departure scope), and the flights they touch are checked right away; the NAS status page still supplies delay trends, closures and deicing, plus advisory links. One program keeps one alert whichever source reports it first, and a SWIM cancel isn't undone by an older page read. Each NAS card says where it came from.

## 3.16.1
- Datalink clearance (PDC / CPDLC DCL) from STDDS on the flight page: cleared to, departure procedure, initial altitude, departure frequency, expected runway, squawk, EDCT revisions, the tail the clearance went to, the pilot's response, and the full cleared route. Also in the lookup log.

## 3.16.0
- Map: the route ahead is the FAA's trajectory along the route ATC has cleared (FAA SWIM), updated with every amendment and clearance, drawn from the airplane onward so flown track, position and the (new) route ahead read as one line; fix names show from zoom 8. FlightAware's filed route is the fallback.
- Map: takeoff and landing buttons jump to the departure or arrival airport zoomed in to see the surface traffic (they replace the runway button).

## 3.15.16
- Map credits restyled: small, dim and translucent in the map's colors instead of a bright strip; the Leaflet tag is gone (the required OpenStreetMap and CARTO credits stay).

## 3.15.15
- Lookup log entries are stamped when the check finishes (after the FlightAware page load), so news that arrived during the check no longer looks like it came before it happened.

## 3.15.14
- After landing, the arrival time holds the last estimate (or takes United's gate time once United updates after landing) instead of jumping back to FlightAware's.
- Lookup log: SWIM lines only show what's new since the previous line; no more "update, nothing new" or the same estimate every minute.
- Tower states in plain words ("taxiing in" instead of "ama taxi in").

## 3.15.13
- Airport map restyled for contrast: taxiway labels look like taxiway location signs (yellow on black, boxed), runway labels like runway signs (white on red), taxiway pavement in neutral gray with the yellow centerline only at zoom 17+, and surface aircraft brighter.

## 3.15.12
- Airport map: taxiways drawn at their real width (about 75 ft) with a yellow centerline, like the paint on the ground.

## 3.15.11
- Airport map: runways are drawn lighter, at their real width at every zoom, with a dashed centerline.

## 3.15.10
- Surface map: targets the radar labels "UNKN" (seen by radar only, no transponder reply) no longer count as identified, so the XPDR filter hides them and ID doesn't tag them "UNKN".

## 3.15.9
- Maps: never fall back to OpenStreetMap's own tile servers (they block apps: "Access blocked"). If CARTO tiles won't load the map steps down to keyless CARTO, then Esri's gray map. A source is only abandoned when it loads nothing, not on a few bad tiles. The keyed CARTO address no longer asks for retina tiles.

## 3.15.8
- Surface traffic shows whenever you zoom the flight's map in on its departure or arrival airport (zoom 13+), at any phase of the flight, and loads right away when you get there.

## 3.15.7
- Arrival estimate in the air uses only estimates worked out after takeoff (the tower's pre-departure one still assumed the schedule).
- The shown arrival only moves when the estimate changes by 2+ minutes, so the log isn't a new line every minute.

## 3.15.6
- Lookup log: transponder checks name the source that actually answered ("EWR surface radar", "N90 approach radar", or adsb.lol when the FAA had nothing).

## 3.15.5
- Surface map: an ID button shows every aircraft's callsign next to it (squawk when there's no callsign). Remembered on that device.

## 3.15.4
- Map tiles: the CARTO key goes in the address CARTO documents (rastertiles …?key=), so tiles load without the "API key required" mark.

## 3.15.3
- Map tiles: optional API key and tile address in the Configuration (CARTO or another provider).

## 3.15.2
- FAA SWIM: callsigns and airports no longer watched are forgotten after 12 hours, so memory stays flat over months.

## 3.15.1
- Surface map: an XPDR button filters to aircraft with transponders on (the default) or shows everything the surface radar sees, including vehicles and untransponded targets. The choice is remembered on that device.

## 3.15.0
- FAA radar first for live positions: approach radar (every ~5 s near airports) and airport surface radar give the position and the airplane itself (its transponder address decodes to the N-number, no lookup). adsb.lol is still used en route, for foreign aircraft and outside the US.
- Surface radar events on the flight: landing and takeoff to the second with the runway, runway exit, onto/off the ramp. Wheels up/down now come from surface radar first, then the FAA, then the airline.
- Tower departure clearance time and gate on the details page.
- United's own gate-arrival time when it reports the flight complete.
- The surface map leaves out aircraft still in the air on approach.
- TFDM keep-alive messages are ignored.

## 3.14.3
- The lookup log says in plain words who reported what, in local time ("FAA: took off 22:19; United: pushed 21:53; EWR tower: landing est. 22:49").
- Each time on the flight page says where it came from (FAA, United, EWR tower, FlightAware), and log changes show "via".

## 3.14.2
- SWIM news (takeoff, landing, diversion, a tower state change) now updates the flight within seconds without waiting for a FlightAware page load; the page follows 20 s later.
- Tower (TFDM) actual off-block and in-block times are used when present.
- Map: your airplane isn't drawn twice when it's on the surface radar.

## 3.14.1
- Maps use CARTO's OpenStreetMap tiles: current (EWR's new Terminal A) and detailed down to zoom 20.
- Zoomed in on one of your flight's airports, the map labels runways and terminals (zoom 14+), taxiways (15+) and gates (16+), from OpenStreetMap, fetched once per airport and kept for a month.
- Surface traffic labels restyled to match the app.

## 3.14.0
- FAA SWIM now takes priority: the FAA's takeoff and landing times, United's own push time and gate-arrival estimate, and in the air the best landing estimate (the tower's at TFDM airports like EWR, else the FAA's).
- Route: the details show the route ATC has on file (with amendments like a shortcut, and the previous route), replacing FlightAware's copy.
- A SWIM takeoff, landing or diversion refreshes the flight and alerts right away instead of waiting for the next check.
- TFDM tower data in the details: departure runway, departure fix, predicted delay, estimated takeoff and taxi time; arrival runway and landing estimate.
- STDDS (optional third subscription): live surface traffic on the flight's map while it's on the ground at an airport with surface radar; a runway button zooms to the airport.
- SWIM facts are saved on the flight, so they survive restarts and long flights.

## 3.13.1
- TFDM: its own connection URL (ems3) and password fields.

## 3.13.0
- FAA SWIM: optional second subscription, TFDM (tower surface data). Set its queue and VPN in the Configuration; it uses the TFMS login unless given its own. Messages are counted and sampled (not used yet) so the parser can be built from the real feed.

## 3.12.9
- FAA SWIM samples: counts messages sent by each airline itself and keeps up to five sent by United.

## 3.12.8
- FAA SWIM: the flight on after a diversion (UA2127 MDT→EWR) now matches. The FAA keeps the original departure time on it, which the 6-hour check used to reject.

## 3.12.7
- FAA SWIM: ignores the FAA's automatic "still not departed" re-timings (every 5 min for a filed flight that hasn't left); they carry no news and a placeholder ETA.

## 3.12.6
- FAA SWIM: reads the times the airline itself files with the FAA (gate out, off, on, gate in) and logs them for your flights.

## 3.12.5
- FAA SWIM: reads actual takeoff and landing times, the FAA ETA and the diversion flag from TFMS messages; the flight log shows them. Parser checked against real feed samples.

## 3.12.4
- FAA SWIM certificate check fixed ("unable to get local issuer certificate"): the trust folder now holds all of Debian's roots plus the intermediate certificate the FAA server points to (fetched from its certificate's CA Issuers address), hashed with openssl

## 3.12.3
- FAA SWIM: the first connection gives up after three tries and shows why (with a hint for login, certificate, network or queue problems) instead of sitting at "Connecting…"; it then tries again after a pause. Reconnecting after a drop still never gives up

## 3.12.2
- Settings → FAA SWIM → Download sample messages: a zip with up to three raw messages of every type the feed has sent, the last 20 that matched your flights, and a summary of counts and what was read from them

## 3.12.1
- FAA SWIM connection set up the way the FAA's Python JumpStart kit does it: credentials as connection properties, retry forever, certificate checked against Let's Encrypt's ISRG Root X1

## 3.12.0
- FAA SWIM (TFMS) feed, optional: with your SWIFT portal subscription's username, password and queue in the Configuration, the add-on stays connected to the FAA's live flight data, keeps only messages for flights you're watching (and their inbounds), and marks a diversion the moment a flight plan is amended to a new destination. SWIM messages show in the lookup log; Settings → FAA SWIM shows the connection and message rate. Blank = off

## 3.11.0
- Flightradar24 and AeroDataBox removed entirely (both block automated readers). Their settings are gone; tails come from ADS-B (your inbound, its earlier legs, your own transponder), your earlier leg on a turn, and AeroAPI; diversions from FlightAware's destination and route

## 3.10.2
- A ground stop or ground delay program at your departure airport only pushes when your inbound is known, hasn't taken off, and is inside the program's scope (or the scope can't be told). No inbound known, or an inbound from outside the listed centers, no push (it still shows on the flight's screen)

## 3.10.1
- AeroDataBox diversion check is off by default: its site answers the page reader with "Verification required" (the lookup log now says so instead of "no flight data read")

## 3.10.0
- Recovery flights are followed automatically: when a flight you track diverts and lands at the diversion airport, the same flight number from there to the original destination is added and found (UA2608 IAH→EWR diverted to ABE → UA2608 ABE→EWR), with a push "UA 2608 continues ABE→EWR" (Alerts → Recovery flight followed). It's yours (kept in History, paired with the diversion) or a friend's, like the diverted flight
- If your work calendar already has that leg, nothing is added; if the calendar lists it later, the calendar's copy wins and the automatic one is removed

## 3.9.5
- Two airplanes broadcasting the same flight number at once (a late earlier leg still flying or holding): ADS-B now picks the one with the tail we know, otherwise the one nearest where this flight should be (its last tracked position, or along its route), for your flight, your inbound and the airplane's earlier legs

## 3.9.4
- History-list lookup: if a route appears twice on the date, it takes the leg closest to now

## 3.9.3
- A flight you add that isn't on the flight number's main FlightAware page (a diversion's recovery leg like UA2608 ABE→EWR) is found through FlightAware's history list for the number, which links every leg by route and date (…/UAL2608/history/20261017/0123Z/KABE/KEWR)

## 3.9.2
- AeroDataBox reader waits for the page's search to finish ("Searching…" gone, up to 45 s) before reading and taking its screenshot

## 3.9.1
- A flight you add that AeroAPI doesn't have (like a diversion's recovery leg, UA2608 ABE→EWR) is now looked up on FlightAware's page instead of waiting: the page often shows it long before AeroAPI does, and it's free. The "Not found" note says it's checking there

## 3.9.0
- AeroDataBox diversion check (free, no key): while your flight is in the air, its page for the flight number is read every 15 minutes (Settings → Tail and position sources → AeroDataBox diversion check; 0 turns it off). When it shows a different arrival airport, the flight is marked diverted right away, often before FlightAware. Each read is in the lookup log (purple tag) with what it found, and a screenshot
- Fix: your own flight's live ADS-B position (used for the map while you're flying) was never fetched since 2.6.0, because of a name clash in the code; it is now

## 3.8.5
- "Check AeroAPI now" on a flight's page: one paid query on demand (counted in the monthly budget, always allowed past the per-flight limit). Its answer goes in the lookup log and a full check follows
- After a diversion every message uses the real route (LAX→MDT), not the planned one
- A flight that picks up a new FlightAware record mid-trip (a diversion) keeps its tail number
- Lookup log shows a source's note next to the tail it gave

## 3.8.4
- Diversions are also caught from the filed route: when ATC amends it to end at another airport ("KLAX DCT RIC261040 DCT KMDT" on a flight planned to EWR), it counts as diverted even before FlightAware changes the destination. Routes that end at an arrival procedure are left alone

## 3.8.3
- Diversions: a flight headed somewhere other than its planned arrival (LAX→MDT instead of LAX→EWR), or "cancelled" after it already took off (how AeroAPI marks the original), is a diversion. It shows Diverted with a "Diverted to MDT (planned EWR)" banner, pushes "UA 2127 diverted to MDT" once (no more "cancelled" then "back on"), keeps being followed to the gate, and goes into History as planned EWR, diverted to MDT
- A flight you added keeps the arrival you entered as its planned airport

## 3.8.2
- NAS cards and pushes no longer add "already airborne — not held" or "doesn't hold … leaving" lines

## 3.8.1
- NAS pushes only while they can still hold your flight:
  - Ground stops and ground delay programs at your arrival airport, and departure delays at your departure airport: only until you take off. Once you're airborne, none of them push
  - A ground stop or ground delay program at your departure airport only matters through your inbound: it pushes only while the inbound hasn't taken off (or isn't known yet)

## 3.8.0
- Faster loading on slow connections:
  - The app draws right away from what it showed last time, then updates when the add-on answers (no blank page while a weak signal catches up)
  - app.js, the styles and icons are kept on the phone until an update changes them (they were downloaded again on every open); the page itself and the flight data answer "not changed" in a few bytes when nothing moved
  - Fonts no longer hold up the first draw
  - On the direct port, everything goes compressed (through the Home Assistant sidebar, Home Assistant already compresses)
- If anything looks wrong after updating, the previous release is kept on the `restore-3.7.9` branch

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
