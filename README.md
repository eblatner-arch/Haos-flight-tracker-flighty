# Pilot Flight Tracker — Home Assistant add-on

[![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Feblatner-arch%2Fhaos-flight-tracker)

A Flighty-style tracker for your crew schedule. It reads the schedule saved by
[ICS2GCAL](https://github.com/eblatner-arch/ics2gcal) and, from 12 hours before
each departure, watches gates, delays, the inbound aircraft, NAS programs and
weather, and pushes changes to your phone. Includes a live map with the filed
route and the aircraft.

## Install

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add `https://github.com/eblatner-arch/haos-flight-tracker`
3. Install **Flight Tracker**, set `airline_icao` and `notify_service`, and start it.

See [flight_tracker/DOCS.md](flight_tracker/DOCS.md) for everything else.

## Development

```
python3 -m unittest discover -s tests
```
