# Pilot Flight Tracker — Home Assistant add-on repository

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Feblatner-arch%2Fhaos-flight-tracker)

## Add-ons

### [Flight Tracker](flight_tracker/)

Screenshots FlightAware or FlightRadar24 pages in a real browser and uses
Google Gemini to read the flight status, times, altitude and speed into
Home Assistant sensors. Includes an optional live noVNC viewer.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add `https://github.com/eblatner-arch/haos-flight-tracker`
3. Install **Flight Tracker**, set your Gemini API key and flights, and start it.

See [flight_tracker/DOCS.md](flight_tracker/DOCS.md) for configuration.
