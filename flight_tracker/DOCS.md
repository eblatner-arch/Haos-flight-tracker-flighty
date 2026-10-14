# Flight Tracker

Opens flight tracking pages in a real Chromium browser, screenshots them, and
asks Gemini to read the flight details. Each tracked flight becomes a sensor.

## Setup

1. Get a Gemini API key at https://aistudio.google.com/apikey and paste it into
   **gemini_api_key**.
2. Add your flights:

   ```yaml
   flights:
     - ident: UAL1234
       source: flightaware
     - ident: N12345
       source: fr24_aircraft
   ```

   | source          | ident                         | page                                   |
   |-----------------|-------------------------------|----------------------------------------|
   | `flightaware`   | flight number or tail number  | flightaware.com/live/flight/IDENT      |
   | `fr24_flight`   | flight number (e.g. `ua1234`) | flightradar24.com/data/flights/ident   |
   | `fr24_aircraft` | registration (e.g. `n12345`)  | flightradar24.com/data/aircraft/ident  |

3. Start the add-on and check the **Log** tab.

## Sensors

Each flight creates `sensor.flight_<ident>`. The state is the flight status
(`Scheduled`, `En Route`, `Landed`, `Delayed`, `Unavailable`, ...). Attributes:
`flight_number`, `registration`, `aircraft_type`, `origin`, `destination`,
`departure_scheduled`, `departure_actual`, `arrival_scheduled`,
`arrival_estimated`, `delay_minutes`, `altitude_ft`, `groundspeed_kt`,
`progress_percent`, `source_url`, `screenshot`, `last_updated`.

The latest screenshot for each flight is saved to `/share/flight_tracker/`.

These sensors are pushed through the API, so they disappear after a Home
Assistant restart until the next cycle runs (within one interval).

## Live viewer

With **enable_viewer** on and a **viewer_password** set, open
`http://<your-ha-ip>:6080/vnc.html` to watch or drive the browser. Use it to
clear a CAPTCHA, accept cookies, or log in to a FlightAware/FR24 account; the
browser profile is kept in `/data`, so sessions survive restarts. VNC passwords
are limited to 8 characters.

## Notes

- If a site blocks the browser, the sensor state becomes `Unavailable`. Open the
  viewer and clear the challenge.
- Each flight costs one Gemini call per interval. With a 300 s interval that is
  about 290 calls per flight per day.
