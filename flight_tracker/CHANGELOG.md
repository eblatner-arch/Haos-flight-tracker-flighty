# Changelog

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
