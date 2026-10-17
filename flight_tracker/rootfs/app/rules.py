"""Notification rules: every event the tracker can detect, and how you want each one pushed.

Each rule: on, hours (push only from this many hours before departure; None = any time),
calendar / manual / friends (which flights it applies to: from your crew calendar, ones you
added as yours, or friends' flights; None = not applicable), ts (time-sensitive
push), and optional numeric params. Stored in the add-on's state and edited in the app.
"""

import copy

# (key, group, label, description, defaults, params)
# defaults: on, hours, calendar, manual, friends, ts
# params: {name: (label, default, unit)}
CATALOG = [
    # Departure
    ("dep_gate_assigned", "Departure", "Gate assigned", "First gate for the flight.",
     dict(on=True, hours=2, calendar=True, manual=True, friends=True, ts=False), {}),
    ("dep_gate_change", "Departure", "Gate change", "The departure gate changed.",
     dict(on=True, hours=2, calendar=True, manual=True, friends=True, ts=True), {}),
    ("terminal_change", "Departure", "Terminal change", "The departure terminal changed.",
     dict(on=True, hours=2, calendar=True, manual=True, friends=True, ts=True), {}),
    ("delay", "Departure", "Delayed", "Departure is late by at least the threshold.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True),
     {"threshold": ("Late by at least", 15, "min")}),
    ("delay_change", "Departure", "Delay grows or shrinks", "An existing delay moves by at least the step.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True),
     {"step": ("Change of at least", 10, "min")}),
    ("back_on_time", "Departure", "Back on time", "A delay goes away.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=False), {}),
    ("early", "Departure", "Earlier than scheduled", "Departure estimate moves earlier than scheduled.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=False, ts=True),
     {"minutes": ("Early by at least", 10, "min")}),
    ("pushback", "Departure", "Pushback", "The flight leaves the gate.",
     dict(on=False, hours=None, calendar=False, manual=True, friends=True, ts=False), {}),
    ("takeoff", "Departure", "Takeoff", "Wheels up, with the arrival estimate.",
     dict(on=True, hours=None, calendar=False, manual=True, friends=True, ts=False), {}),
    ("landed", "Arrival", "Landed", "Wheels down.",
     dict(on=False, hours=None, calendar=False, manual=True, friends=True, ts=False), {}),
    ("arrived", "Arrival", "At the gate", "Arrived at the gate. Tracking stops here.",
     dict(on=True, hours=None, calendar=False, manual=False, friends=True, ts=False), {}),
    # Arrival
    ("arr_gate", "Arrival", "Arrival gate", "Arrival gate assigned or changed.",
     dict(on=False, hours=2, calendar=True, manual=True, friends=True, ts=False), {}),
    ("baggage", "Arrival", "Baggage claim", "Bag claim carousel assigned.",
     dict(on=False, hours=None, calendar=False, manual=True, friends=True, ts=False), {}),
    ("arr_change", "Arrival", "Arrival time change", "Once airborne, the arrival estimate moves by at least the step (before takeoff the departure time carries any delay).",
     dict(on=False, hours=None, calendar=True, manual=True, friends=True, ts=False),
     {"step": ("Change of at least", 15, "min")}),
    # Status
    ("cancelled", "Status", "Cancelled", "The flight is cancelled.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True), {}),
    ("reinstated", "Status", "Reinstated", "A cancelled flight is back on.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True), {}),
    ("diverted", "Status", "Diverted", "The flight diverted.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True), {}),
    ("recovery", "Status", "Recovery flight followed", "After a diversion, the flight on to the original destination is found and followed automatically.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=False), {}),
    # Aircraft
    ("tail_identified", "Aircraft", "Tail identified", "First time the tail number is known.",
     dict(on=False, hours=None, calendar=True, manual=True, friends=False, ts=False), {}),
    ("swap", "Aircraft", "Aircraft swap", "A different tail number than before.",
     dict(on=True, hours=2, calendar=True, manual=True, friends=False, ts=True), {}),
    ("aircraft_type", "Aircraft", "Aircraft type change", "A different aircraft type (e.g. 737-800 to MAX 9).",
     dict(on=True, hours=None, calendar=True, manual=False, friends=False, ts=True), {}),
    # Inbound
    ("inbound_departed", "Inbound", "Inbound departed", "Your inbound aircraft took off.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=False, ts=False),
     {"max_sched_turn": ("Only if scheduled turn under", 60, "min")}),
    ("inbound_delayed", "Inbound", "Inbound arriving late", "Your inbound's estimated arrival at your airport is later than scheduled.",
     dict(on=True, hours=None, calendar=True, manual=False, friends=False, ts=True),
     {"threshold": ("Late by at least", 15, "min"), "max_sched_turn": ("Only if scheduled turn under", 60, "min")}),
    ("inbound_late", "Inbound", "Short turn",
     "The time between your inbound's arrival and your departure is under the minimum, whether or not the inbound is late.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=False, ts=True),
     {"min_turn": ("Turn shorter than", 35, "min")}),
    ("inbound_at_gate", "Inbound", "Inbound at the gate", "Your aircraft has arrived at your departure airport.",
     dict(on=True, hours=None, calendar=True, manual=False, friends=False, ts=False),
     {"max_sched_turn": ("Only if scheduled turn under", 60, "min")}),
    ("inbound_cancelled", "Inbound", "Inbound cancelled or diverted", "The flight bringing your aircraft won't arrive.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=False, ts=True), {}),
    ("inbound_changed", "Inbound", "Different inbound linked", "FlightAware links a different inbound flight.",
     dict(on=False, hours=2, calendar=True, manual=False, friends=False, ts=False), {}),
    # NAS (any flight; ground stops push whatever their scope, and say whether it includes you)
    ("nas_gs", "NAS", "Ground stop", "At either airport, even if its scope doesn't include you yet; also when it's extended or expanded.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True), {}),
    ("nas_gdp", "NAS", "Ground delay program", "At either airport; also when it's extended or expanded.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True), {}),
    ("nas_delay", "NAS", "Delay trends", "FAA-reported average departure/arrival delays. Trends only, not a program.",
     dict(on=False, hours=None, calendar=True, manual=True, friends=True, ts=False), {}),
    ("nas_closure", "NAS", "Airport closure", "Closures at either airport.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=True, ts=True), {}),
    ("nas_other", "NAS", "Other NAS programs", "Deicing and anything else the FAA lists.",
     dict(on=False, hours=None, calendar=True, manual=True, friends=True, ts=False), {}),
    # Weather
    ("wx_ifr", "Weather", "IFR at departure or arrival", "Latest METAR drops to IFR or LIFR.",
     dict(on=False, hours=3, calendar=True, manual=False, friends=None, ts=False), {}),
    ("wx_gust", "Weather", "Strong gusts", "Latest METAR gusts at or above the limit.",
     dict(on=False, hours=3, calendar=True, manual=False, friends=None, ts=False),
     {"gust": ("Gusts of at least", 30, "kt")}),
    # Route
    ("route_change", "Route", "Filed route changed", "A different filed route or altitude.",
     dict(on=False, hours=None, calendar=True, manual=False, friends=False, ts=False), {}),
    # Schedule (crew calendar)
    ("schedule_change", "Schedule", "Crew schedule change", "Your calendar adds, drops or retimes a flight in the next 3 days.",
     dict(on=True, hours=None, calendar=True, manual=True, friends=None, ts=True), {}),
    # System
    ("sys_budget", "System", "AeroAPI budget", "80% of the month's budget used, and when it runs out.",
     dict(on=True, hours=None, calendar=None, manual=None, friends=None, ts=False), {}),
    ("sys_blocked", "System", "FlightAware page checks failing", "Several checks in a row found no flight data (CAPTCHA, sign-in).",
     dict(on=True, hours=None, calendar=None, manual=None, friends=None, ts=False),
     {"count": ("After this many in a row", 3, "checks")}),
    ("sys_schedule_stale", "System", "Schedule file not updating", "The ICS2GCAL feed copy hasn't changed for a while.",
     dict(on=True, hours=None, calendar=None, manual=None, friends=None, ts=False),
     {"hours": ("Older than", 24, "h")}),
    ("watch_start", "System", "Tracking started", "When a flight enters the watch window.",
     dict(on=False, hours=None, calendar=True, manual=True, friends=False, ts=False), {}),
]

KEYS = [c[0] for c in CATALOG]
KINDS = ("calendar", "manual", "friends")
OLD_KINDS = {"mine": "calendar", "dh": "manual", "followed": "friends"}
BY_KEY = {c[0]: c for c in CATALOG}
# Events that, when held back by the hours window, are pushed once the window opens
# (with the value at that moment) instead of being dropped.
CATCH_UP = {"dep_gate_assigned", "arr_gate"}


def defaults():
    out = {}
    for key, _g, _l, _d, d, params in CATALOG:
        r = copy.deepcopy(d)
        r["params"] = {p: v[1] for p, v in params.items()}
        out[key] = r
    return out


def seed_from_config(cfg):
    """Starting rules carry over the older Configuration-tab choices."""
    r = defaults()
    gate_h = cfg.get("gate_alert_hours")
    if gate_h is not None:
        for k in ("dep_gate_assigned", "dep_gate_change", "terminal_change"):
            r[k]["hours"] = float(gate_h) or None
    if cfg.get("swap_alert_hours") is not None:
        r["swap"]["hours"] = float(cfg["swap_alert_hours"]) or None
    if cfg.get("notify_watch_start") is not None:
        r["watch_start"]["on"] = bool(cfg["notify_watch_start"])
        r["tail_identified"]["on"] = bool(cfg["notify_watch_start"])
    if cfg.get("notify_inbound_progress") is not None:
        r["inbound_departed"]["on"] = r["inbound_at_gate"]["on"] = bool(cfg["notify_inbound_progress"])
    if cfg.get("notify_progress"):
        r["pushback"]["calendar"] = r["takeoff"]["calendar"] = True
    if cfg.get("notify_progress_deadhead") is False:
        r["pushback"]["manual"] = r["takeoff"]["manual"] = r["pushback"]["friends"] = r["takeoff"]["friends"] = False
    if cfg.get("delay_threshold_minutes"):
        r["delay"]["params"]["threshold"] = int(cfg["delay_threshold_minutes"])
    if cfg.get("delay_step_minutes"):
        r["delay_change"]["params"]["step"] = int(cfg["delay_step_minutes"])
    if cfg.get("min_turn_minutes"):
        r["inbound_late"]["params"]["min_turn"] = int(cfg["min_turn_minutes"])
    if cfg.get("inbound_alert_minutes") is not None:
        for k in ("inbound_departed", "inbound_delayed", "inbound_at_gate"):
            r[k]["params"]["max_sched_turn"] = int(cfg["inbound_alert_minutes"])
    if cfg.get("time_sensitive") is False:
        for v in r.values():
            v["ts"] = False
    return r


def merge(saved, cfg):
    """Saved rules on top of the seeded ones; new events get their defaults."""
    base = seed_from_config(cfg)
    for k, v in (saved or {}).items():
        if k not in base or not isinstance(v, dict):
            continue
        v = dict(v)
        for old, new in OLD_KINDS.items():  # rules saved before 3.0: My legs / Deadheads / Followed
            if old in v and new not in v:
                v[new] = v[old]
        for f in ("on", "hours", "ts"):
            if f in v:
                base[k][f] = v[f]
        for f in KINDS:
            if f in v and v[f] is not None and base[k].get(f) is not None:
                base[k][f] = v[f]
        for p, val in (v.get("params") or {}).items():
            if p in base[k]["params"]:
                base[k]["params"][p] = val
    return base


def clean(rule_updates, current):
    """Validate rules posted from the app."""
    out = copy.deepcopy(current)
    for k, v in (rule_updates or {}).items():
        if k not in out or not isinstance(v, dict):
            continue
        r = out[k]
        if "on" in v:
            r["on"] = bool(v["on"])
        if "ts" in v:
            r["ts"] = bool(v["ts"])
        if "hours" in v:
            h = v["hours"]
            try:
                h = float(h) if h not in (None, "") else None
            except (TypeError, ValueError):
                h = r["hours"]
            r["hours"] = None if h is None or h <= 0 else min(h, 48.0)
        for f in KINDS:
            if f in v and r.get(f) is not None:
                r[f] = bool(v[f])
        for p, val in (v.get("params") or {}).items():
            if p in r["params"]:
                try:
                    r["params"][p] = max(0, min(int(float(val)), 1440))
                except (TypeError, ValueError):
                    pass
    return out


def catalog_for_ui(current):
    rows = []
    for key, group, label, desc, _d, params in CATALOG:
        rows.append({"key": key, "group": group, "label": label, "desc": desc,
                     "params": [{"name": p, "label": v[0], "unit": v[2]} for p, v in params.items()],
                     "rule": current[key]})
    return rows


def kind_of(leg):
    """calendar (from the crew schedule), manual (added in the app as your own) or friends."""
    if leg.get("extra"):
        return "manual" if leg.get("kind") == "manual" else "friends"
    return "calendar"


def allowed(rules, key, leg, now, out_time):
    """(push?, held_by_window?) for an event on this leg right now."""
    r = rules.get(key)
    if not r or not r.get("on"):
        return False, False
    if leg is not None and r.get(kind_of(leg)) is not True:
        return False, False  # off for this kind of flight, or not applicable to it
    h = r.get("hours")
    if h and out_time and out_time - now > h * 3600:
        return False, True
    return True, False
