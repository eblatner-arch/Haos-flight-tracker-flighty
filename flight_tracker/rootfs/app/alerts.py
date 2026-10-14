"""Decide what changed on a leg and what is worth a push. Pure: no I/O, no clock."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import airports
import flightaware as fa

PHASE_LABEL = {
    "scheduled": "Scheduled", "taxi_out": "Taxiing", "airborne": "En Route", "landed": "Landed",
    "arrived": "Arrived", "cancelled": "Cancelled", "diverted": "Diverted",
}


def tz_for(code, flight_airport=None):
    name = (flight_airport or {}).get("tz") or airports.get(code).get("tz") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def hhmm(ts, tz):
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def mins(n):
    n = int(round(n))
    sign = "-" if n < 0 else ""
    n = abs(n)
    return f"{sign}{n // 60}h {n % 60:02d}m" if n >= 60 else f"{sign}{n} min"


def status_label(leg, cfg):
    f = leg.get("flight")
    ph = fa.phase(f, 0)
    if ph != "scheduled":
        return PHASE_LABEL[ph]
    d = fa.delay_minutes(f, leg.get("sched_out"))
    if d is not None and d >= int(cfg.get("delay_threshold_minutes", 15)):
        return "Delayed"
    if f and (f["times"]["out"]["estimated"] or f["times"]["out"]["scheduled"]):
        return "On Time"
    return "Scheduled"


def turn_minutes(leg):
    f, inb = leg.get("flight"), leg.get("inbound")
    if not inb:
        return None
    our_out = (fa.best_time(f["times"], "out") if f else None) or leg.get("sched_out")
    inb_in = fa.best_time(inb["times"], "in")
    if our_out is None or inb_in is None or fa.phase(inb, 0) == "arrived" and fa.phase(f, 0) != "scheduled":
        return None
    return (our_out - inb_in) / 60


def problems(leg, cfg, nas_items):
    """Current issues on a leg, for the dashboard and the problem sensor."""
    out = []
    f, inb = leg.get("flight"), leg.get("inbound")
    ph = fa.phase(f, 0)
    if ph == "cancelled":
        out.append({"level": "critical", "text": "Cancelled"})
    if ph == "diverted":
        out.append({"level": "critical", "text": "Diverted"})
    if ph == "scheduled":
        d = fa.delay_minutes(f, leg.get("sched_out"))
        if d is not None and d >= int(cfg.get("delay_threshold_minutes", 15)):
            out.append({"level": "warn", "text": f"Delayed {mins(d)}"})
        if inb:
            iph = fa.phase(inb, 0)
            if iph in ("cancelled", "diverted"):
                out.append({"level": "critical", "text": f"Inbound {inb.get('display_ident') or ''} {iph}".strip()})
            t = turn_minutes(leg)
            if t is not None and t < int(cfg.get("min_turn_minutes", 35)) and iph != "arrived":
                out.append({"level": "warn" if t >= 0 else "critical",
                            "text": f"Inbound late: {mins(t)} turn"})
    if ph not in ("arrived", "cancelled"):
        for n in nas_for(leg, nas_items):
            out.append({"level": "warn", "text": f"{n['airport']}: {n['label']}"})
    return out


def nas_for(leg, nas_items):
    """NAS programs at either end of a flight you're on. Flights you only follow get none."""
    if leg.get("extra"):
        return []
    f = leg.get("flight") or {}
    codes = {(leg.get("dep") or "").upper(), (leg.get("arr") or "").upper(),
             ((f.get("origin") or {}).get("iata") or "").upper(),
             ((f.get("destination") or {}).get("iata") or "").upper()}
    codes.discard("")
    return [n for n in nas_items or [] if n["airport"] in codes]


def evaluate(leg, cfg, now, nas_items):
    """Compare the leg's fresh data with what was already pushed. Returns new alerts and
    updates leg['notified'] in place so the same news is never pushed twice."""
    alerts = []
    nt = leg.setdefault("notified", {})
    f, inb = leg.get("flight"), leg.get("inbound")
    ident = leg.get("display") or leg.get("ident") or "Flight"
    dep, arr = leg.get("dep") or "", leg.get("arr") or ""
    dep_tz = tz_for(dep, (f or {}).get("origin"))
    arr_tz = tz_for(arr, (f or {}).get("destination"))
    route = f"{dep}→{arr}"
    tag = f"leg-{leg['id']}"
    ph = fa.phase(f, now)

    def add(key, title, message, level="info"):
        alerts.append({"key": key, "title": title, "message": message, "level": level, "at": now, "tag": tag})

    # 1. Watch start
    if not nt.get("watch") and cfg.get("notify_watch_start", True):
        nt["watch"] = True
        out_t = (fa.best_time(f["times"], "out") if f else None) or leg.get("sched_out")
        bits = [f"departs {hhmm(out_t, dep_tz)}"]
        g = (f or {}).get("origin", {}).get("gate")
        if g:
            bits.append(f"gate {g}")
        if f and f.get("tail"):
            bits.append(f"{f.get('aircraft_type') or ''} {f['tail']}".strip())
            nt["watch_sent_tail"] = f["tail"]
        add("watch", f"Tracking {ident} {route}", " · ".join(bits))
    nt["watch"] = True
    if not f:
        return alerts

    # 2. Cancelled / diverted
    if ph == "cancelled" and not nt.get("cancelled"):
        nt["cancelled"] = True
        add("cancelled", f"{ident} cancelled", f"{route} scheduled {hhmm(leg.get('sched_out'), dep_tz)} is cancelled.",
            "critical")
    if ph == "diverted" and not nt.get("diverted"):
        nt["diverted"] = True
        to = (f.get("destination") or {}).get("iata") or "another airport"
        add("diverted", f"{ident} diverted", f"{route} diverted to {to}.", "critical")

    # 3. Gates
    dg = (f.get("origin") or {}).get("gate")
    dterm = (f.get("origin") or {}).get("terminal")
    if dg and ph in ("scheduled",) and dg != nt.get("dep_gate"):
        old = nt.get("dep_gate")
        where = f"Terminal {dterm} · " if dterm else ""
        if old:
            add("dep_gate", f"{ident} gate change", f"{dep} gate {old} → {dg} ({where}departs "
                f"{hhmm(fa.best_time(f['times'], 'out') or leg.get('sched_out'), dep_tz)})", "warn")
        else:
            add("dep_gate", f"{ident} gate {dg}", f"{where}{dep} gate {dg}, departs "
                f"{hhmm(fa.best_time(f['times'], 'out') or leg.get('sched_out'), dep_tz)}")
    if dg:
        nt["dep_gate"] = dg
    ag = (f.get("destination") or {}).get("gate")
    if ag and ph not in ("arrived", "cancelled") and ag != nt.get("arr_gate"):
        old = nt.get("arr_gate")
        if old:
            add("arr_gate", f"{ident} arrival gate change", f"{arr} gate {old} → {ag}", "info")
        elif ph in ("taxi_out", "airborne", "landed"):
            add("arr_gate", f"{ident} arriving gate {ag}", f"{arr} gate {ag}"
                + (f", bags {f['destination']['baggage']}" if f['destination'].get("baggage") else ""))
    if ag:
        nt["arr_gate"] = ag

    # 4. Departure delay
    if ph == "scheduled":
        d = fa.delay_minutes(f, leg.get("sched_out"))
        thr = int(cfg.get("delay_threshold_minutes", 15))
        step = int(cfg.get("delay_step_minutes", 10))
        last = nt.get("delay")
        new_out = fa.best_time(f["times"], "out")
        if d is not None and d >= thr and (last is None or abs(d - last) >= step):
            verb = "now delayed" if last is not None and d > last else "delayed" if last is None else "delay down to"
            add("delay", f"{ident} {verb} {mins(d)}",
                f"{route} now departs {hhmm(new_out, dep_tz)} (was {hhmm(leg.get('sched_out'), dep_tz)})", "warn")
            nt["delay"] = d
        elif d is not None and d < thr and last is not None:
            add("delay", f"{ident} back on time", f"{route} departs {hhmm(new_out, dep_tz)}")
            nt["delay"] = None

    # 5. Aircraft: first sighting, then swaps
    tail = f.get("tail")
    src = f" · from {f['tail_source']}" if f.get("tail_source") else ""
    if tail and nt.get("tail") and tail != nt["tail"] and ph == "scheduled":
        add("tail", f"{ident} aircraft swap",
            f"{nt['tail']} → {tail} {f.get('aircraft_type') or ''}".strip() + src, "warn")
    elif tail and not nt.get("tail") and ph == "scheduled" and nt.get("watch_sent_tail") != tail \
            and f.get("tail_source") and f["tail_source"] != "FlightAware":
        add("tail", f"{ident} aircraft {tail}", f"{f.get('aircraft_type') or 'Aircraft'} {tail}{src}")
    if tail:
        nt["tail"] = tail

    # 6. Inbound aircraft
    if inb and ph == "scheduled":
        iid = inb.get("display_ident") or inb.get("ident") or leg.get("inbound_ident") or "Inbound"
        ifrom = (inb.get("origin") or {}).get("iata") or ""
        iph = fa.phase(inb, now)
        if iph in ("cancelled", "diverted") and nt.get("inbound_bad") != inb.get("fa_id"):
            nt["inbound_bad"] = inb.get("fa_id")
            add("inbound", f"Inbound {iid} {iph}", f"The aircraft for {ident} ({iid} from {ifrom}) is {iph}.", "critical")
        t = turn_minutes(leg)
        min_turn = int(cfg.get("min_turn_minutes", 35))
        last = nt.get("turn")
        if t is not None and t < min_turn and iph not in ("arrived", "cancelled", "diverted") \
                and (last is None or t <= last - 10):
            nt["turn"] = t
            add("turn", f"{ident}: inbound running late",
                f"{iid} from {ifrom} due {hhmm(fa.best_time(inb['times'], 'in'), dep_tz)} at {dep} — "
                f"{mins(t)} turn for your {hhmm(fa.best_time(f['times'], 'out') or leg.get('sched_out'), dep_tz)} departure",
                "critical" if t < 0 else "warn")
        if cfg.get("notify_inbound_progress", True):
            if iph in ("airborne", "landed", "arrived") and not nt.get("inbound_off"):
                nt["inbound_off"] = True
                add("inbound_off", f"Inbound {iid} departed {ifrom}",
                    f"Due in {hhmm(fa.best_time(inb['times'], 'in'), dep_tz)} for {ident}"
                    + (f" · {inb.get('tail')}" if inb.get("tail") else ""))
            if iph == "arrived" and not nt.get("inbound_in"):
                nt["inbound_in"] = True
                add("inbound_in", f"Inbound {iid} at the gate",
                    f"Your aircraft is in at {dep}" + (f" gate {(inb.get('destination') or {}).get('gate')}"
                                                        if (inb.get("destination") or {}).get("gate") else ""))

    # 7. Progress
    if cfg.get("notify_progress", False) or ((leg.get("dh") or leg.get("extra"))
                                             and cfg.get("notify_progress_deadhead", True)):
        steps = [("out", "taxi_out", "pushed back"), ("off", "airborne", "departed"),
                 ("on", "landed", "landed"), ("in", "arrived", "arrived at the gate")]
        order = ["scheduled", "taxi_out", "airborne", "landed", "arrived"]
        if ph in order:
            for key, at_phase, words in steps:
                if order.index(ph) >= order.index(at_phase) and not nt.get(f"p_{key}"):
                    nt[f"p_{key}"] = True
                    # Only push the latest step, not a backlog after a gap.
                    if order.index(ph) == order.index(at_phase):
                        t = f["times"][key]["actual"]
                        tz = dep_tz if key in ("out", "off") else arr_tz
                        msg = f"{route} at {hhmm(t, tz)}"
                        if key == "off":
                            msg += f", arriving {hhmm(fa.best_time(f['times'], 'in'), arr_tz)}"
                        if key == "in" and (f.get("destination") or {}).get("baggage"):
                            msg += f", bags {f['destination']['baggage']}"
                        add(f"p_{key}", f"{ident} {words}", msg)

    # 8. NAS programs at either end
    if ph not in ("arrived", "cancelled"):
        seen = set(nt.get("nas") or [])
        for n in nas_for(leg, nas_items):
            if n["key"] in seen:
                continue
            seen.add(n["key"])
            text = n["label"]
            if n["detail"]:
                text += f" ({n['detail']})"
            if n["reason"]:
                text += f" — {n['reason']}"
            add("nas", f"{n['airport']}: {n['label']}", f"{ident} {route}: {text}", "warn")
        nt["nas"] = sorted(seen)

    return alerts
