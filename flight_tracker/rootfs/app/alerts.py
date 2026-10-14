"""Decide what changed on a leg and what is worth a push. Pure: no I/O, no clock."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import airports
import artcc
import flightaware as fa
import rules as rules_mod

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


def inbound_close(leg, cfg):
    """Is the inbound scheduled to arrive within inbound_alert_minutes of our scheduled departure?"""
    inb, f = leg.get("inbound"), leg.get("flight")
    if not inb:
        return False
    limit = float(cfg.get("inbound_alert_minutes", 60) or 0)
    if limit <= 0:
        return True
    t = inb.get("times") or {}
    inb_in = (t.get("in") or {}).get("scheduled") or (t.get("on") or {}).get("scheduled") or fa.best_time(t, "in")
    our_out = ((f or {}).get("times") or {}).get("out", {}).get("scheduled") or leg.get("sched_out")
    if inb_in is None or our_out is None:
        return False
    return (our_out - inb_in) / 60 <= limit


def problems(leg, cfg, nas_items, rules=None):
    """Current issues on a leg, for the dashboard and the problem sensor."""
    out = []
    min_turn = ((rules or {}).get("inbound_late") or {}).get("params", {}).get("min_turn") \
        or int(cfg.get("min_turn_minutes", 35))
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
            if t is not None and t < min_turn and iph != "arrived":
                out.append({"level": "warn" if t >= 0 else "critical",
                            "text": f"Short turn: {mins(t)}"})
    if ph not in ("arrived", "cancelled"):
        for n in nas_for(leg, nas_items):
            if n["kind"] == "delay":
                continue  # FAA delay trends are information, not a problem with the flight
            hit = any(i["hit"] for i in n.get("impact") or [])
            out.append({"level": "critical" if n["kind"] in ("gs", "closure") and hit is not False else "warn",
                        "text": f"{n['airport']}: {n['label']}", "nas": True})
    return out


def nas_for(leg, nas_items):
    """NAS programs at either end of a flight, each with how its scope touches this flight:
    impact = [{who: "you"|"inbound", from, center, hit: True/False/None}]."""
    f = leg.get("flight") or {}
    dep = (leg.get("dep") or (f.get("origin") or {}).get("iata") or "").upper()
    arr = (leg.get("arr") or (f.get("destination") or {}).get("iata") or "").upper()
    codes = {dep, arr, ((f.get("origin") or {}).get("iata") or "").upper(),
             ((f.get("destination") or {}).get("iata") or "").upper()}
    codes.discard("")
    inb = leg.get("inbound") or {}
    inb_from = ((inb.get("origin") or {}).get("iata") or "").upper()
    inb_ph = fa.phase(inb, 0) if inb else None
    inb_done = inb_ph in ("arrived", "cancelled", "diverted") if inb else True
    up = ("airborne", "landed")  # a ground stop only holds flights that haven't taken off
    out = []
    for n in nas_items or []:
        if n["airport"] not in codes:
            continue
        m = dict(n)
        m["here"] = "dep" if n["airport"] == dep else "arr"
        impact = []
        if n["kind"] in ("gs", "gdp") and (n.get("scope") or n.get("flights")):
            if n["airport"] == arr and dep:
                air = fa.phase(f, 0) in up if f else False
                impact.append({"who": "you", "from": dep, "center": artcc.center_of(dep), "airborne": air,
                               "hit": False if air else artcc.in_scope(n, dep)})
            if n["airport"] == dep and inb_from and not inb_done:
                air = inb_ph in up
                impact.append({"who": "inbound", "from": inb_from, "center": artcc.center_of(inb_from), "airborne": air,
                               "hit": False if air else artcc.in_scope(n, inb_from)})
        m["impact"] = impact
        out.append(m)
    return out


def nas_text(n, tz):
    """One push-sized description of a NAS program, including what its scope means for you."""
    bits = [n["label"]]
    if n.get("end") and n["kind"] in ("gs", "gdp", "closure"):
        bits[0] += f" until {hhmm(n['end'], tz)}"
    if n.get("detail"):
        bits.append(n["detail"])
    if n.get("reason"):
        bits.append(n["reason"])
    if n.get("prob"):
        bits.append(f"extension probability {n['prob'].lower()}")
    if n.get("scope"):
        bits.append("departures from " + " ".join(n["scope"]))
    if n.get("here") == "dep" and n["kind"] in ("gs", "gdp"):
        bits.append(f"doesn't hold departures from {n['airport']}")
    for i in n.get("impact") or []:
        where = f"{i['from']}" + (f" ({i['center']})" if i.get("center") else "")
        whose = "your departure" if i["who"] == "you" else "your inbound"
        if i.get("airborne"):
            bits.append(f"{whose} from {where} is already airborne, so it isn't held")
        elif i["hit"] is True:
            bits.append(f"includes {whose} from {where}")
        elif i["hit"] is False:
            bits.append(f"doesn't include {whose} from {where} yet")
    return " · ".join(bits)


NAS_EVENT = {"gs": "nas_gs", "gdp": "nas_gdp", "delay": "nas_delay", "closure": "nas_closure"}


def sched_turn_minutes(leg):
    """Scheduled gap between the inbound's arrival and our departure."""
    inb, f = leg.get("inbound"), leg.get("flight")
    if not inb:
        return None
    t = inb.get("times") or {}
    inb_in = (t.get("in") or {}).get("scheduled") or (t.get("on") or {}).get("scheduled") or fa.best_time(t, "in")
    our_out = ((f or {}).get("times") or {}).get("out", {}).get("scheduled") or leg.get("sched_out")
    if inb_in is None or our_out is None:
        return None
    return (our_out - inb_in) / 60


def evaluate(leg, cfg, now, nas_items, rules=None, wx=None):
    """Compare the leg's fresh data with what was already pushed. Returns the alerts the rules
    allow, and updates leg['notified'] so the same news is never pushed twice."""
    R = rules or rules_mod.seed_from_config(cfg)
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
    out_now = (fa.best_time(f["times"], "out") if f else None) or leg.get("sched_out") or now
    P = lambda ev, name: R[ev]["params"].get(name)  # noqa: E731

    def add(event, title, message, level="info", undo=None):
        ok, held = rules_mod.allowed(R, event, leg, now, out_now)
        if ok:
            alerts.append({"key": event, "title": title, "message": message, "level": level, "at": now,
                           "tag": tag, "ts": bool(R[event].get("ts"))})
            return True
        elif held and event in rules_mod.CATCH_UP and undo:
            for k, v in undo:  # push it once the window opens, with the value at that moment
                nt[k] = v

    # Tracking started
    if not nt.get("watch"):
        nt["watch"] = True
        bits = [f"departs {hhmm(out_now, dep_tz)}"]
        g = (f or {}).get("origin", {}).get("gate")
        if g:
            bits.append(f"gate {g}")
        if f and f.get("tail"):
            bits.append(f"{f.get('aircraft_type') or ''} {f['tail']}".strip())
            nt["watch_sent_tail"] = f["tail"]
        add("watch_start", f"Tracking {ident} {route}", " · ".join(bits))
    if not f:
        return alerts

    # Status
    if ph == "cancelled" and not nt.get("cancelled"):
        nt["cancelled"] = True
        add("cancelled", f"{ident} cancelled", f"{route} scheduled {hhmm(leg.get('sched_out'), dep_tz)} is cancelled.",
            "critical")
    elif ph != "cancelled" and nt.get("cancelled"):
        nt["cancelled"] = False
        add("reinstated", f"{ident} is back on", f"{route} reinstated, departs {hhmm(out_now, dep_tz)}", "warn")
    if ph == "diverted" and not nt.get("diverted"):
        nt["diverted"] = True
        to = (f.get("destination") or {}).get("iata") or "another airport"
        add("diverted", f"{ident} diverted", f"{route} diverted to {to}.", "critical")

    o, d = f.get("origin") or {}, f.get("destination") or {}
    # Departure gate and terminal
    if ph == "scheduled":
        dg, term = o.get("gate"), o.get("terminal")
        where = f"Terminal {term} · " if term else ""
        old = nt.get("dep_gate")
        if dg and dg != old:
            nt["dep_gate"] = dg
            if old:
                add("dep_gate_change", f"{ident} gate change",
                    f"{dep} gate {old} → {dg} ({where}departs {hhmm(out_now, dep_tz)})", "warn")
            else:
                add("dep_gate_assigned", f"{ident} gate {dg}", f"{where}{dep} gate {dg}, departs {hhmm(out_now, dep_tz)}",
                    undo=[("dep_gate", None)])
        if term and nt.get("dep_term") and term != nt["dep_term"]:
            add("terminal_change", f"{ident} terminal change", f"{dep} terminal {nt['dep_term']} → {term}", "warn")
        if term:
            nt["dep_term"] = term
    # Arrival gate, bags, arrival time
    if ph not in ("arrived", "cancelled"):
        ag = d.get("gate")
        if ag and ag != nt.get("arr_gate"):
            old = nt.get("arr_gate")
            nt["arr_gate"] = ag
            add("arr_gate", f"{ident} arrival gate {'change' if old else ag}",
                f"{arr} gate {old} → {ag}" if old else f"{arr} gate {ag}", undo=[("arr_gate", old)])
        bag = d.get("baggage")
        if bag and bag != nt.get("bag"):
            nt["bag"] = bag
            add("baggage", f"{ident} bags at {bag}", f"{arr} baggage claim {bag}")
        ain = fa.best_time(f["times"], "in")
        if ain:
            if nt.get("arr_est") and abs(ain - nt["arr_est"]) / 60 >= (P("arr_change", "step") or 15):
                add("arr_change", f"{ident} now arrives {hhmm(ain, arr_tz)}",
                    f"{route} arrival {hhmm(nt['arr_est'], arr_tz)} → {hhmm(ain, arr_tz)}")
                nt["arr_est"] = ain
            elif not nt.get("arr_est"):
                nt["arr_est"] = ain

    # Departure delay / early
    if ph == "scheduled":
        dly = fa.delay_minutes(f, leg.get("sched_out"))
        thr = P("delay", "threshold") or 15
        step = P("delay_change", "step") or 10
        last = nt.get("delay")
        if dly is not None and dly >= thr:
            if last is None:
                add("delay", f"{ident} delayed {mins(dly)}",
                    f"{route} now departs {hhmm(out_now, dep_tz)} (was {hhmm(leg.get('sched_out'), dep_tz)})", "warn")
                nt["delay"] = dly
            elif abs(dly - last) >= step:
                add("delay_change", f"{ident} {'now delayed' if dly > last else 'delay down to'} {mins(dly)}",
                    f"{route} now departs {hhmm(out_now, dep_tz)} (was {hhmm(leg.get('sched_out'), dep_tz)})", "warn")
                nt["delay"] = dly
        elif dly is not None and last is not None:
            add("back_on_time", f"{ident} back on time", f"{route} departs {hhmm(out_now, dep_tz)}")
            nt["delay"] = None
        early = P("early", "minutes") or 10
        if dly is not None and dly <= -early and not nt.get("early"):
            nt["early"] = True
            add("early", f"{ident} leaving early", f"{route} now departs {hhmm(out_now, dep_tz)} "
                f"({mins(-dly)} early)", "warn")
        elif dly is not None and dly >= 0:
            nt["early"] = False

    # Aircraft
    tail = f.get("tail")
    src = f" · from {f['tail_source']}" if f.get("tail_source") else ""
    if ph == "scheduled" and tail:
        if nt.get("tail") and tail != nt["tail"]:
            add("swap", f"{ident} aircraft swap", f"{nt['tail']} → {tail} {f.get('aircraft_type') or ''}".strip() + src,
                "warn")
        elif not nt.get("tail") and nt.get("watch_sent_tail") != tail:
            add("tail_identified", f"{ident} aircraft {tail}", f"{f.get('aircraft_type') or 'Aircraft'} {tail}{src}")
    if tail:
        nt["tail"] = tail
    typ = f.get("aircraft_type")
    if ph == "scheduled" and typ and nt.get("atype") and typ != nt["atype"]:
        add("aircraft_type", f"{ident} equipment change", f"{nt['atype']} → {typ}", "warn")
    if typ:
        nt["atype"] = typ

    # Route
    if f.get("route"):
        rk = f"{f['route']}|{f.get('filed_altitude') or ''}"
        if nt.get("route") and rk != nt["route"] and ph == "scheduled":
            alt = f.get("filed_altitude")
            add("route_change", f"{ident} new filed route",
                f"{f['route']}" + (f" · FL{int(alt / (100 if alt > 1000 else 1)):03d}" if alt else ""))
        nt["route"] = rk

    # Inbound aircraft
    if inb and ph == "scheduled":
        iid = inb.get("display_ident") or inb.get("ident") or leg.get("inbound_ident") or "Inbound"
        ifrom = (inb.get("origin") or {}).get("iata") or ""
        iph = fa.phase(inb, now)
        if inb.get("fa_id") and nt.get("inb_id") and inb["fa_id"] != nt["inb_id"]:
            add("inbound_changed", f"{ident}: different inbound", f"Now {iid} from {ifrom}")
            for k in ("inbound_off", "inbound_in", "turn", "inb_delay"):
                nt.pop(k, None)
        if inb.get("fa_id"):
            nt["inb_id"] = inb["fa_id"]
        if iph in ("cancelled", "diverted") and nt.get("inbound_bad") != inb.get("fa_id"):
            nt["inbound_bad"] = inb.get("fa_id")
            add("inbound_cancelled", f"Inbound {iid} {iph}",
                f"The aircraft for {ident} ({iid} from {ifrom}) is {iph}.", "critical")
        st = sched_turn_minutes(leg)

        def close(ev):
            mx = P(ev, "max_sched_turn")
            return not mx or (st is not None and st <= mx)
        idly = fa.delay_minutes(inb)
        if idly is not None and idly >= (P("inbound_delayed", "threshold") or 15) and not nt.get("inb_delay") \
                and iph in ("scheduled", "taxi_out", "airborne") and close("inbound_delayed"):
            nt["inb_delay"] = True
            add("inbound_delayed", f"Inbound {iid} delayed {mins(idly)}",
                f"Leaves {ifrom} {hhmm(fa.best_time(inb['times'], 'out'), tz_for(ifrom, inb.get('origin')))}, "
                f"due {dep} {hhmm(fa.best_time(inb['times'], 'in'), dep_tz)}", "warn")
        t = turn_minutes(leg)
        last = nt.get("turn")
        if t is not None and t < (P("inbound_late", "min_turn") or 35) and iph not in ("arrived", "cancelled", "diverted") \
                and (last is None or t <= last - 10) and close("inbound_late"):
            # only remember it once it's actually pushed, so turning the setting on later still alerts
            if add("inbound_late", f"{ident}: short turn, {mins(t)}",
                   f"{iid} from {ifrom} due {hhmm(fa.best_time(inb['times'], 'in'), dep_tz)} at {dep} — "
                   f"{mins(t)} turn for your {hhmm(out_now, dep_tz)} departure", "critical" if t < 0 else "warn"):
                nt["turn"] = t
        if iph in ("airborne", "landed", "arrived") and not nt.get("inbound_off") and close("inbound_departed"):
            nt["inbound_off"] = True
            add("inbound_departed", f"Inbound {iid} departed {ifrom}",
                f"Due in {hhmm(fa.best_time(inb['times'], 'in'), dep_tz)} for {ident}"
                + (f" · {inb.get('tail')}" if inb.get("tail") else ""))
        if iph == "arrived" and not nt.get("inbound_in") and close("inbound_at_gate"):
            nt["inbound_in"] = True
            add("inbound_at_gate", f"Inbound {iid} at the gate",
                f"Your aircraft is in at {dep}" + (f" gate {(inb.get('destination') or {}).get('gate')}"
                                                    if (inb.get("destination") or {}).get("gate") else ""))

    # Pushback / takeoff (tracking stops after takeoff)
    order = ["scheduled", "taxi_out", "airborne", "landed", "arrived"]
    if ph in order:
        for key, at_phase, ev, words in (("out", "taxi_out", "pushback", "pushed back"),
                                         ("off", "airborne", "takeoff", "departed")):
            if order.index(ph) >= order.index(at_phase) and not nt.get(f"p_{key}"):
                nt[f"p_{key}"] = True
                if order.index(ph) == order.index(at_phase) or key == "off":
                    tt = f["times"][key]["actual"]
                    msg = f"{route} at {hhmm(tt, dep_tz)}"
                    if key == "off":
                        msg += f", arriving {hhmm(fa.best_time(f['times'], 'in'), arr_tz)}"
                    add(ev, f"{ident} {words}", msg)

    # NAS programs at either end
    if ph not in ("arrived", "cancelled"):
        seen = set(nt.get("nas") or [])
        ext = nt.get("nas_ext") or {}
        cur = {}
        for n in nas_for(leg, nas_items):
            ev = NAS_EVENT.get(n["kind"], "nas_other")
            atz = dep_tz if n["airport"] == dep else arr_tz
            hit = any(i["hit"] for i in n.get("impact") or [])
            level = "critical" if hit and n["kind"] in ("gs", "closure") else "warn"
            prev = ext.get(n["key"])
            cur[n["key"]] = {"end": n.get("end"), "scope": n.get("scope") or []}
            if n["key"] not in seen:
                seen.add(n["key"])
                add(ev, f"{n['airport']}: {n['label']}", f"{ident} {route}: {nas_text(n, atz)}", level)
            elif prev and n["kind"] in ("gs", "gdp"):
                if prev.get("end") and n.get("end") and n["end"] > prev["end"] + 60:
                    add(ev, f"{n['airport']}: {n['label'].lower()} extended",
                        f"{ident} {route}: now until {hhmm(n['end'], atz)} (was {hhmm(prev['end'], atz)}) · "
                        + nas_text(n, atz).split(" · ", 1)[-1], level)
                added = [c for c in n.get("scope") or [] if c not in (prev.get("scope") or [])]
                if added and prev.get("scope"):
                    add(ev, f"{n['airport']}: {n['label'].lower()} expanded",
                        f"{ident} {route}: now also {' '.join(added)} · {nas_text(n, atz)}", level)
        nt["nas"] = sorted(seen)
        # keep what we knew about a program through a feed hiccup, so extensions are still compared
        nt["nas_ext"] = {**{k: v for k, v in ext.items() if k in seen}, **cur}

    # Weather at either end
    if wx and ph == "scheduled":
        cats, gusts = nt.setdefault("wx_cat", {}), nt.setdefault("wx_gust", {})
        for code in (dep, arr):
            w = wx.get(airports.icao(code) or ("K" + code if len(code) == 3 else code)) or {}
            cat = w.get("category")
            if cat in ("IFR", "LIFR") and cats.get(code) not in ("IFR", "LIFR"):
                add("wx_ifr", f"{code} now {cat}", f"{ident} {route}: {w.get('raw') or cat}")
            if cat:
                cats[code] = cat
            g, lim = w.get("gust_kt"), P("wx_gust", "gust") or 30
            if g and g >= lim and not gusts.get(code):
                gusts[code] = True
                add("wx_gust", f"{code} gusting {g} kt", f"{ident} {route}: {w.get('raw') or ''}")
            elif not g or g < lim:
                gusts[code] = False

    return alerts
