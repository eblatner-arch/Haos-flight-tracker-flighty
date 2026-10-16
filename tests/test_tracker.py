"""Run with: python3 -m unittest discover -s tests"""

import os
import sys
import json
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "flight_tracker", "rootfs", "app"))

import alerts  # noqa: E402
import flightaware as fa  # noqa: E402
import schedule  # noqa: E402
import sources  # noqa: E402

T0 = 1_760_450_000  # an arbitrary "now"
H = 3600


def ev(summary, start, hours=3.0, oid=None):
    return {"oid": oid or summary.replace(" ", "")[:16], "start": start, "end": start + hours * H,
            "allday": False, "summary": summary, "location": ""}


def boot(fa_id="UAL1234-1760000000-airline-0001", gate="C71", out_s=T0 + 10 * H, out_e=None,
         tail="N37502", inbound_id="UAL567-1760000000-airline-0002", origin="EWR", dest="SFO",
         activity=None, off_a=None, cancelled=False):
    f = {
        "flightId": fa_id, "ident": "UAL1234", "displayIdent": "UA1234", "flightStatus": "",
        "cancelled": cancelled, "diverted": False,
        "origin": {"iata": origin, "icao": "K" + origin, "TZ": ":America/New_York", "gate": gate, "terminal": "C",
                   "coord": [-74.17, 40.69], "friendlyLocation": "Newark, NJ"},
        "destination": {"iata": dest, "icao": "K" + dest, "TZ": ":America/Los_Angeles", "gate": "F12",
                        "terminal": "3", "coord": [-122.38, 37.62]},
        "gateDepartureTimes": {"scheduled": out_s, "estimated": out_e, "actual": None},
        "takeoffTimes": {"scheduled": out_s + 900, "estimated": None, "actual": off_a},
        "landingTimes": {"scheduled": out_s + 6 * H, "estimated": None, "actual": None},
        "gateArrivalTimes": {"scheduled": out_s + 6.3 * H, "estimated": None, "actual": None},
        "aircraft": {"type": "B39M", "friendlyType": "Boeing 737 MAX 9", "tail": tail},
        "flightPlan": {"route": "PARKE J6 LEJOY", "altitude": 370, "speed": 460, "directDistance": 2200},
        "waypoints": [[-74.17, 40.69], [-100.0, 41.0], [-122.38, 37.62]],
        "track": [{"coord": [-74.2, 40.7], "alt": 50, "gs": 180, "timestamp": T0}] if off_a else [],
        "inboundFlight": {"fa_flight_id": inbound_id} if inbound_id else None,
        "activityLog": {"flights": activity or []},
    }
    return {"flights": {fa_id: f}}


CFG = {"airline_icao": "UAL", "gate_alert_hours": 0, "swap_alert_hours": 0, "inbound_alert_minutes": 0, "delay_threshold_minutes": 15, "delay_step_minutes": 10, "min_turn_minutes": 35,
       "notify_watch_start": True, "notify_inbound_progress": True, "notify_progress": False,
       "notify_progress_deadhead": True}


class Schedule(unittest.TestCase):
    def test_titles(self):
        events = [
            ev("2204 EWR-IAH", T0),
            ev("UA1873 IAH - EWR", T0 + 5 * H),
            ev("DH AA 312 EWR-ORD", T0 + 24 * H),
            ev("2204 EWR 27Sep 12:01 - IAH 27Sep 14:50", T0 + 48 * H),
            ev("Trip 1234 EWR-IAH-EWR", T0, hours=12),  # spans two flights → container
            ev("Hotel IAH", T0 + 3 * H, hours=10),
        ]
        legs = schedule.legs_from_events(events, CFG)
        got = [(L["ident"], L["dep"], L["arr"], L["dh"]) for L in legs]
        self.assertEqual(got, [("UAL2204", "EWR", "IAH", False), ("UAL1873", "IAH", "EWR", False),
                               ("AAL312", "EWR", "ORD", True), ("UAL2204", "EWR", "IAH", False)])

    def test_no_airline_configured(self):
        legs = schedule.legs_from_events([ev("2204 EWR-IAH", T0)], {"airline_icao": ""})
        self.assertEqual(legs[0]["ident"], "")


class FlightAware(unittest.TestCase):
    def test_normalize(self):
        cur, act = fa.flights_from_bootstrap(boot(off_a=T0))
        self.assertEqual(cur["fa_id"], "UAL1234-1760000000-airline-0001")
        self.assertEqual(cur["origin"]["gate"], "C71")
        self.assertEqual(cur["origin"]["tz"], "America/New_York")
        self.assertEqual(cur["origin"]["lat"], 40.69)
        self.assertEqual(cur["tail"], "N37502")
        self.assertEqual(cur["inbound_fa_id"], "UAL567-1760000000-airline-0002")
        self.assertEqual(cur["waypoints"][0], [40.69, -74.17])
        self.assertEqual(cur["track"][0][2], 5000)
        self.assertEqual(fa.phase(cur, T0), "airborne")

    def test_pick_instance(self):
        other = boot(fa_id="UAL1234-1760086400-airline-0003", out_s=T0 + 34 * H)["flights"]
        b = boot(out_s=T0 - 14 * H, activity=list(other.values()))
        cur, act = fa.flights_from_bootstrap(b)
        inst = fa.pick_instance(cur, act, "EWR", T0 + 34 * H + 600)
        self.assertEqual(inst["fa_id"], "UAL1234-1760086400-airline-0003")
        self.assertIsNone(fa.pick_instance(cur, act, "IAH", T0 + 34 * H))

    def test_garbage(self):
        self.assertEqual(fa.flights_from_bootstrap(None), (None, []))
        self.assertEqual(fa.flights_from_bootstrap({"flights": {}}), (None, []))
        self.assertIsNotNone(fa.normalize({}))


class Alerts(unittest.TestCase):
    def leg(self):
        return {"id": "abc", "ident": "UAL1234", "display": "UA 1234", "dep": "EWR", "arr": "SFO",
                "sched_out": T0 + 10 * H, "sched_in": T0 + 16.3 * H, "dh": False, "notified": {}}

    def test_sequence(self):
        L = self.leg()
        L["flight"], _ = fa.flights_from_bootstrap(boot())
        a = alerts.evaluate(L, CFG, T0, [])
        self.assertEqual([x["key"] for x in a], ["watch_start", "dep_gate_assigned"])
        self.assertIn("C71", a[1]["message"])
        self.assertEqual(alerts.evaluate(L, CFG, T0 + 60, []), [])  # nothing new → nothing pushed

        L["flight"], _ = fa.flights_from_bootstrap(boot(gate="C80", out_e=T0 + 10 * H + 25 * 60, tail="N12345"))
        keys = [x["key"] for x in alerts.evaluate(L, CFG, T0 + 120, [])]
        self.assertEqual(keys, ["dep_gate_change", "delay", "swap"])
        # small wobble in the delay is not news
        L["flight"], _ = fa.flights_from_bootstrap(boot(gate="C80", out_e=T0 + 10 * H + 30 * 60, tail="N12345"))
        self.assertEqual(alerts.evaluate(L, CFG, T0 + 180, []), [])
        # back on time
        L["flight"], _ = fa.flights_from_bootstrap(boot(gate="C80", out_e=T0 + 10 * H + 5 * 60, tail="N12345"))
        a = alerts.evaluate(L, CFG, T0 + 240, [])
        self.assertEqual([x["key"] for x in a], ["back_on_time"])
        self.assertIn("back on time", a[0]["title"])

    def test_inbound_and_nas(self):
        L = self.leg()
        L["notified"] = {"watch": True}
        L["flight"], _ = fa.flights_from_bootstrap(boot())
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="UAL567-x", out_s=T0 + 4 * H, origin="ORD", dest="EWR",
                                               inbound_id=None, off_a=T0 + 4.2 * H))
        inb["times"]["in"]["estimated"] = T0 + 10 * H - 10 * 60  # 10 min before our departure
        L["inbound"] = inb
        nas = [{"airport": "EWR", "kind": "gdp", "label": "Ground delay program", "reason": "low ceilings",
                "detail": "avg 1 hour", "key": "EWR|gdp|x"},
               {"airport": "BOS", "kind": "gs", "label": "Ground stop", "reason": "", "detail": "", "key": "BOS|gs"}]
        a = alerts.evaluate(L, CFG, T0, nas)
        keys = [x["key"] for x in a]
        self.assertIn("inbound_late", keys)
        self.assertIn("inbound_departed", keys)
        self.assertEqual([x for x in a if x["key"] == "nas_gdp"][0]["title"], "EWR: Ground delay program")
        self.assertEqual(len([x for x in a if x["key"].startswith("nas")]), 1)
        self.assertEqual(alerts.evaluate(L, CFG, T0 + 60, nas), [])
        probs = [p["text"] for p in alerts.problems(L, CFG, nas)]
        self.assertTrue(any("turn" in p for p in probs))
        self.assertIn("EWR: Ground delay program", probs)

    def test_cancel(self):
        L = self.leg()
        L["notified"] = {"watch": True, "dep_gate": "C71"}
        L["flight"], _ = fa.flights_from_bootstrap(boot(cancelled=True))
        a = alerts.evaluate(L, CFG, T0, [])
        self.assertEqual([x["key"] for x in a if x["key"] != "tail_identified"], ["cancelled"])
        self.assertEqual(a[0]["level"], "critical")
        self.assertEqual(alerts.status_label(L, CFG), "Cancelled")


class Nas(unittest.TestCase):
    XML = """<AIRPORT_STATUS_INFORMATION><Update_Time>x</Update_Time>
<Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List><Ground_Delay><ARPT>BOS</ARPT>
<Reason>low ceilings</Reason><Avg>3 hours and 26 minutes</Avg><Max>6 hours and 35 minutes</Max></Ground_Delay></Ground_Delay_List></Delay_type>
<Delay_type><Name>Ground Stop Programs</Name><Ground_Stop_List><Program><ARPT>EWR</ARPT><Reason>thunderstorms</Reason><End_Time>9:45 pm EDT.</End_Time></Program></Ground_Stop_List></Delay_type>
<Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List><Delay><ARPT>PHX</ARPT><Reason>WX:Low Ceilings</Reason>
<Arrival_Departure Type="Departure"><Min>31 minutes</Min><Max>45 minutes</Max><Trend>Increasing</Trend></Arrival_Departure></Delay></Arrival_Departure_Delay_List></Delay_type>
<Delay_type><Name>Airport Closures</Name><Airport_Closure_List><Airport><ARPT>SNA</ARPT><Reason>!SNA CLSD</Reason><Start>Oct 13 at 06:30 UTC.</Start><Reopen>Oct 17 at 13:15 UTC.</Reopen></Airport></Airport_Closure_List></Delay_type>
</AIRPORT_STATUS_INFORMATION>"""

    def test_parse(self):
        items = sources.parse_nas(self.XML)
        by = {i["airport"]: i for i in items}
        self.assertEqual(by["BOS"]["kind"], "gdp")
        self.assertIn("avg 3 hours", by["BOS"]["detail"])
        self.assertEqual(by["EWR"]["kind"], "gs")
        self.assertEqual(by["EWR"]["detail"], "until 9:45 pm EDT.")
        self.assertEqual(by["PHX"]["label"], "Departure delays")
        self.assertIn("31 minutes–45 minutes", by["PHX"]["detail"])
        self.assertEqual(by["SNA"]["kind"], "closure")
        self.assertEqual(len(items), 4)



class AppSettings(unittest.TestCase):
    def test_carry_over_and_api(self):
        import server, threading, urllib.request
        server.STATE["app_settings"] = {}
        server.CFG["watch_hours"], server.CFG["poll_far_minutes"] = 12, 45     # what the old config had
        server.init_app_settings()
        self.assertEqual((server.STATE["app_settings"]["watch_hours"], server.STATE["app_settings"]["poll_far_minutes"]), (12, 45))
        self.assertEqual(server.clean_app_settings({"watch_hours": "99", "live_positions": 0, "bogus": 1})["watch_hours"], 48)
        server.save_state = lambda: None
        srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        got = json.load(urllib.request.urlopen(base + "/api/settings"))
        self.assertIn("watch_hours", [x["key"] for x in got["settings"]])
        req = urllib.request.Request(base + "/api/settings", data=json.dumps({"settings": {"watch_hours": 20, "live_positions": False}}).encode(), method="POST")
        self.assertTrue(json.load(urllib.request.urlopen(req))["ok"])
        self.assertEqual((server.CFG["watch_hours"], server.CFG["live_positions"]), (20, False))
        srv.shutdown()
        server.CFG["watch_hours"], server.CFG["live_positions"] = 18, True


class TurnTail(unittest.TestCase):
    def test_turn_takes_tail_from_your_first_leg(self):
        import server
        a = {"id": "out", "display": "UA 2785", "fa_id": "UAL2785-1760000000-airline-1", "sched_out": T0, "sched_in": T0 + 3 * H,
             "tail_info": {"tail": "N37560", "kind": "aeroapi", "at": T0 + 100, "source": "AeroAPI (airline assignment)"}}
        b = {"id": "back", "display": "UA 1102", "ident": "UAL1102", "sched_out": T0 + 20 * H, "sched_in": T0 + 23 * H,
             "aero": {"tail": "N38467", "at": T0}}
        server.STATE["legs"] = {"out": a, "back": b}
        f = {"times": {k: {"scheduled": None, "estimated": None, "actual": None} for k in ("out", "off", "on", "in")},
             "inbound_fa_id": "UAL2785-1760000000-airline-1", "tail": None}
        calls = []
        real = server.aero_call
        server.aero_call = lambda *x, **k: calls.append(x)
        try:
            server.resolve_tail(None, b, f, T0 + 200)
        finally:
            server.aero_call = real
        self.assertEqual(f["tail"], "N37560")
        self.assertIn("UA 2785", f["tail_source"])
        self.assertEqual(calls, [])


class History(unittest.TestCase):
    CSV = ("Date,Airline,Flight,From,To,Dep Terminal,Dep Gate,Arr Terminal,Arr Gate,Canceled,Diverted To,"
           "Gate Departure (Scheduled),Gate Departure (Actual),Take off (Scheduled),Take off (Actual),"
           "Landing (Scheduled),Landing (Actual),Gate Arrival (Scheduled),Gate Arrival (Actual),Aircraft Type Name,Tail Number\n"
           "2026-10-10,UAL,2283,EWR,CUR,C,C136,,6,false,,2026-10-10T08:56,2026-10-10T08:48,2026-10-10T09:06,2026-10-10T09:12,"
           "2026-10-10T13:30,2026-10-10T13:16,2026-10-10T13:40,2026-10-10T13:20,Boeing 737-900ER,N67815\n"
           "2026-07-27,UAL,1611,FLL,EWR,,C1,,D8,false,IAD,2026-07-27T12:46,2026-07-27T12:40,2026-07-27T12:56,2026-07-27T12:52,"
           "2026-07-27T15:40,2026-07-27T15:51,2026-07-27T15:50,2026-07-27T16:12,Boeing 737 MAX 9,N17591\n")

    def setUp(self):
        import tempfile
        import history
        self.h = history
        history.init(tempfile.mkdtemp())

    def test_import_is_idempotent_and_local_times(self):
        r = self.h.import_flighty("x.csv", self.CSV.encode())
        self.assertEqual((r["added"], r["already"]), (2, 0))
        self.assertEqual(self.h.import_flighty("x.csv", self.CSV.encode())["added"], 0)
        a = self.h.summary(self.h.get("2026-10-10_UAL2283_EWR"))
        self.assertEqual((a["dep_delay"], a["arr_delay"], a["type"]), (-8.0, -20.0, "B739"))
        self.assertEqual(a["block_min"], 272.0)                     # EDT out, Curaçao (AST) in
        d = self.h.summary(self.h.get("2026-07-27_UAL1611_FLL"))
        self.assertEqual((d["diverted_to"], d["arr_delay"]), ("IAD", None))
        st = self.h.stats(self.h.flights())
        self.assertEqual((st["flights"], st["diverted"], st["on_time_pct"]), (2, 1, 100))

    def test_finished_calendar_flight_is_archived_friends_are_not(self):
        import server
        server.history = self.h
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0, off_a=T0 + 600))
        f["times"]["out"]["actual"] = T0 + 120
        f["times"]["in"]["actual"] = T0 + 6 * H
        mine = {"id": "c1", "ident": "UAL1234", "display": "UA 1234", "airline": "UAL", "flight_no": "1234",
                "dep": "EWR", "arr": "SFO", "sched_out": T0, "sched_in": T0 + 6 * H, "flight": f, "updated": T0 + 6 * H}
        friend = dict(mine, id="x-1", extra=True, kind="friend", ident="UAL99")
        server.STATE["legs"] = {"c1": mine, "x-1": friend}
        server.archive_finished(T0 + 6.1 * H)
        ids = [r["id"] for r in self.h.flights()]
        self.assertEqual(len(ids), 1)
        self.assertIn("UAL1234", ids[0])
        self.assertTrue(server.STATE["legs"]["c1"]["history_id"])


class Fr24Swap(unittest.TestCase):
    def test_same_inbound_number_new_tail(self):
        import server
        out_s = T0 + 10 * H
        L = {"id": "m", "display": "UA 2785", "ident": "UAL2785", "dep": "EWR", "arr": "MIA", "sched_out": out_s,
             "sched_in": out_s + 3 * H, "aero": {"tail": "N38467", "at": T0}}
        server.STATE["legs"] = {"m": L}
        f = {"times": {k: {"scheduled": out_s if k == "out" else None, "estimated": None, "actual": None}
                       for k in ("out", "off", "on", "in")}, "inbound_fa_id": "UAL2291-1", "tail": None, "aircraft_type": "B739"}
        pages = []

        class B:
            def fr24_rows(self, url, shot):
                pages.append(url)
                return [{"text": "16 Oct 2026 Newark (EWR) Miami (MIA) B39M (N37560)", "cells": ["B39M (N37560)"],
                         "ts": [str(int(out_s))], "regs": ["N37560"]}]
        calls = []
        real = server.aero_call
        server.aero_call = lambda *x, **k: calls.append(x)
        try:
            server.resolve_tail(B(), L, f, T0 + 60)
            self.assertEqual((f["tail"], L["tail_info"]["kind"]), ("N37560", "fr24"))
            self.assertEqual(pages, ["https://www.flightradar24.com/data/flights/ua2785"])
            server.resolve_tail(B(), L, f, T0 + 120)              # inside the interval: no new page
            self.assertEqual(len(pages), 1)
            f["aircraft_type"] = "B39M"                            # FlightAware shows a different type: check now
            server.resolve_tail(B(), L, f, T0 + 180)
            self.assertEqual(len(pages), 2)
            self.assertIn("aircraft type", L["fr24"]["why"])
        finally:
            server.aero_call = real
        self.assertEqual(calls, [])


class Startup(unittest.TestCase):
    def test_init_rules_with_old_saved_rules(self):
        """Rules saved by older versions (with settings since removed) must still load."""
        import server
        old = server.rules_mod.seed_from_config(server.CFG)
        for r in old.values():                                # 2.x saved My legs / Deadheads / Followed
            r["mine"], r["dh"], r["followed"] = r.pop("calendar"), r.pop("manual"), r.pop("friends")
        old["inbound_late"]["params"]["max_sched_turn"] = 60   # 2.10.0 and earlier had this
        old["nas_gs"]["followed"] = None                      # and NAS had no Followed switch
        old["pushback"]["dh"] = False                         # a Deadheads choice becomes the Manual choice
        old["nas_delay"]["on"] = True
        server.STATE["rules"], server.STATE["flags"] = old, {}
        server.init_rules()
        R = server.STATE["rules"]
        self.assertNotIn("max_sched_turn", R["inbound_late"]["params"])
        self.assertTrue(R["nas_gs"]["friends"])
        self.assertFalse(R["pushback"]["manual"])
        self.assertNotIn("dh", R["pushback"])
        self.assertFalse(R["nas_delay"]["on"])
        server.init_rules()                                   # restarting again keeps working

if __name__ == "__main__":
    unittest.main()


class Lifecycle(unittest.TestCase):
    def test_tracks_until_the_gate(self):
        os.environ.setdefault("FT_DATA", "/nonexistent")
        import server
        L = {"id": "a", "sched_out": T0 + H, "sched_in": T0 + 4 * H, "dep": "EWR", "arr": "SFO"}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=T0 + H))
        self.assertTrue(server.watched(L, T0))
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=T0 + H, off_a=T0 + 1.2 * H))
        self.assertFalse(server.finished(L, T0 + 1.3 * H))           # airborne: still tracked
        self.assertTrue(server.watched(L, T0 + 1.3 * H))
        self.assertEqual(server.poll_seconds(L, T0 + 1.3 * H), server.CFG["poll_air_minutes"] * 60)
        L["flight"]["times"]["in"]["actual"] = T0 + 4 * H
        self.assertTrue(server.finished(L, T0 + 4.1 * H))             # at the gate: done
        self.assertFalse(server.watched(dict(L, extra=True), T0 + 4.1 * H))

    def test_pick_by_date(self):
        b1 = boot(fa_id="A", out_s=T0)            # 2025-10-14 local NY
        b2 = boot(fa_id="B", out_s=T0 + 24 * H)
        cur, _ = fa.flights_from_bootstrap(b1)
        act = [fa.normalize(list(b2["flights"].values())[0])]
        from datetime import datetime
        from zoneinfo import ZoneInfo
        d2 = datetime.fromtimestamp(T0 + 24 * H, ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        self.assertEqual(fa.pick_by_date(cur, act, d2)["fa_id"], "B")
        self.assertIsNone(fa.pick_by_date(cur, act, "1999-01-01"))


class Inbound(unittest.TestCase):
    def test_only_rechecks_completed_inbound_on_swap(self):
        import server
        f, _ = fa.flights_from_bootstrap(boot(tail="N1"))
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="UAL567-1760000000-airline-0002", inbound_id=None))
        L = {"inbound": None}
        self.assertTrue(server.inbound_needs_check(L, f))           # never checked
        inb["checked_for_tail"] = "N1"
        L["inbound"] = inb
        self.assertTrue(server.inbound_needs_check(L, f))           # still en route / scheduled
        inb["times"]["in"]["actual"] = T0
        self.assertFalse(server.inbound_needs_check(L, f))          # at the gate: stop
        f["tail"] = "N2"
        self.assertTrue(server.inbound_needs_check(L, f))           # tail swap: look again
        f["tail"] = "N1"; f["inbound_fa_id"] = "UAL999-x"
        self.assertTrue(server.inbound_needs_check(L, f))           # different inbound linked


class TailChain(unittest.TestCase):
    def test_chain_then_own_transponder(self):
        import server
        live_tails = {"UAL890": "N777UA"}
        sources.fetch_live = lambda cs: {"lat": 1, "lon": 2, "tail": live_tails[cs]} if cs in live_tails else None

        class FB:
            client = None

            def load(self, url, name):
                if "UAL890" in url:  # the airplane's earlier leg, flying now
                    b = boot(fa_id="UAL890-x", out_s=T0 - 2 * H, origin="DEN", dest="ORD", inbound_id=None,
                             off_a=T0 - 1.8 * H)
                    b["flights"]["UAL890-x"]["ident"] = "UAL890"
                    return b, None
                return {"flights": {}}, None

        L = {"id": "c1", "ident": "UAL1234", "display": "UA 1234", "dep": "EWR", "arr": "SFO",
             "sched_out": T0 + 6 * H, "sched_in": T0 + 12 * H, "notified": {"watch": True}}
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0 + 6 * H, tail=None, inbound_id="UAL567-x"))
        L["flight"] = f
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="UAL567-x", out_s=T0 + 2 * H, origin="ORD", dest="EWR",
                                               inbound_id="UAL890-x"))
        inb["ident"] = "UAL567"
        L["inbound"] = inb
        server.resolve_tail(FB(), L, f, T0)
        self.assertEqual(f["tail"], "N777UA")
        self.assertEqual(L["tail_info"]["kind"], "chain")
        self.assertEqual([c["phase"] for c in L["chain"]], ["scheduled", "airborne"])
        a = [x for x in alerts.evaluate(L, CFG, T0, []) if x["key"] == "tail_identified"]
        self.assertEqual(len(a), 1)
        self.assertIn("in flight", a[0]["message"])

        # 80 minutes out, our own transponder shows a different airplane → swap
        live_tails["UAL1234"] = "N888UA"
        f2, _ = fa.flights_from_bootstrap(boot(out_s=T0 + 6 * H, tail=None, inbound_id="UAL567-x"))
        L["flight"] = f2
        server.resolve_tail(FB(), L, f2, T0 + 6 * H - 80 * 60)
        self.assertEqual(f2["tail"], "N888UA")
        a = [x for x in alerts.evaluate(L, CFG, T0 + 6 * H - 80 * 60, []) if x["key"] == "swap"]
        self.assertEqual(len(a), 1)
        self.assertIn("swap", a[0]["title"])
        self.assertIn("transponder", a[0]["message"])

    def test_new_inbound_forgets_chain_tail(self):
        import server
        sources.fetch_live = lambda cs: None

        class FB:
            client = None

            def load(self, url, name):
                return {"flights": {}}, None

        L = {"id": "c2", "ident": "UAL1", "sched_out": T0 + 6 * H, "sched_in": T0 + 9 * H, "inbound": None,
             "tail_info": {"tail": "N1", "kind": "chain", "root": "OLD", "source": "x"}}
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0 + 6 * H, tail=None, inbound_id="NEW"))
        server.resolve_tail(FB(), L, f, T0)
        self.assertIsNone(f.get("tail"))
        self.assertEqual(L["tail_info"], {})


class Followed(unittest.TestCase):
    def test_route_picks_the_right_leg(self):
        a = fa.normalize(list(boot(fa_id="A", out_s=T0 + 2 * H, origin="EWR", dest="ORD")["flights"].values())[0])
        b = fa.normalize(list(boot(fa_id="B", out_s=T0 + 5 * H, origin="ORD", dest="SFO")["flights"].values())[0])
        gone = fa.normalize(list(boot(fa_id="C", out_s=T0 - 2 * H, origin="ORD", dest="SFO",
                                      off_a=T0 - 1.8 * H)["flights"].values())[0])
        self.assertEqual(fa.pick_followed(gone, [a, b], "ORD", "SFO", "", T0)["fa_id"], "B")
        self.assertEqual(fa.pick_followed(gone, [a, b], "EWR", "ORD", "", T0)["fa_id"], "A")
        self.assertIsNone(fa.pick_followed(gone, [a, b], "EWR", "SFO", "", T0))

    def test_nas_for_any_flight(self):
        nas = [{"airport": "EWR", "kind": "gs", "label": "Ground stop", "reason": "", "detail": "", "key": "k"}]
        self.assertEqual(len(alerts.nas_for({"dep": "EWR", "arr": "SFO", "extra": True}, nas)), 1)
        self.assertEqual(len(alerts.nas_for({"dep": "EWR", "arr": "SFO"}, nas)), 1)
        self.assertEqual(alerts.nas_for({"dep": "BOS", "arr": "SFO"}, nas), [])

    EVENTS = [{"airportId": "EWR", "groundStop": {
        "id": "gs1", "impactingCondition": "thunderstorms", "startTime": "2026-10-14T16:19:00Z",
        "endTime": "2026-10-14T17:45:00Z", "probabilityOfExtension": "MEDIUM", "center": "ZNY",
        "includedFacilities": ["ZDC", "ZJX", "ZTL", "ZMA"], "includedFlights": "", "advisoryUrl": "https://x"},
        "groundDelay": None, "arrivalDelay": None, "airportClosure": None, "deicing": None,
        "departureDelay": {"reason": "VOL:Volume", "arrivalDeparture": {"type": "Departure", "min": "16 minutes",
                                                                       "max": "30 minutes", "trend": "Increasing"}}}]

    def test_ground_stop_scope(self):
        items = sources.parse_events(self.EVENTS)
        gs = [i for i in items if i["kind"] == "gs"][0]
        self.assertEqual((gs["scope"], gs["prob"], gs["key"]), (["ZDC", "ZJX", "ZTL", "ZMA"], "Medium", "EWR|gs|gs1"))
        dl = [i for i in items if i["kind"] == "delay"][0]
        self.assertEqual((dl["label"], dl["detail"], dl["reason"]), ("Departure delay trend", "16–30 min and increasing", "Volume"))
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="IN", out_s=T0, origin="TPA", dest="EWR", inbound_id=None))
        # GS at my departure airport: holds my inbound from TPA (ZJX); MCO->EWR: includes my departure
        out = alerts.nas_for({"dep": "EWR", "arr": "MCO", "inbound": inb}, items)
        imp = [n for n in out if n["kind"] == "gs"][0]["impact"]
        self.assertEqual([(i["who"], i["from"], i["center"], i["hit"]) for i in imp], [("inbound", "TPA", "ZJX", True)])
        air, _ = fa.flights_from_bootstrap(boot(fa_id="IN", out_s=T0, origin="TPA", dest="EWR", inbound_id=None,
                                               off_a=T0 + 600))
        imp = [n for n in alerts.nas_for({"dep": "EWR", "arr": "MCO", "inbound": air}, items) if n["kind"] == "gs"][0]["impact"]
        self.assertEqual([(i["hit"], i["airborne"]) for i in imp], [(False, True)])   # already flying: not held
        g = [n for n in alerts.nas_for({"dep": "EWR", "arr": "MCO"}, items) if n["kind"] == "gs"][0]
        self.assertIn("doesn't hold departures from EWR", alerts.nas_text(g, None))   # leaving a GS airport is fine
        self.assertFalse(any(p.get("nas") and "delay" in p["text"].lower()
                             for p in alerts.problems({"dep": "EWR", "arr": "MCO", "flight": None}, {}, items)))
        self.assertIn("already airborne", alerts.nas_text([n for n in alerts.nas_for(
            {"dep": "EWR", "arr": "MCO", "inbound": air}, items) if n["kind"] == "gs"][0], None))
        imp = [n for n in alerts.nas_for({"dep": "BOS", "arr": "EWR"}, items) if n["kind"] == "gs"][0]["impact"]
        self.assertEqual([(i["who"], i["hit"]) for i in imp], [("you", False)])   # ZBW isn't in scope: still shown

    def test_delay_trend_direction(self):
        items = sources.parse_events([{"airportId": "ORD", "departureDelay": {"reason": "VOL:Volume", "averageDelay": "15",
                                       "arrivalDeparture": {"type": "Departure", "min": "1", "max": "2", "trend": "Increasing"}}},
                                      {"airportId": "EWR", "arrivalDelay": {"reason": "WX", "arrivalDeparture": {"type": "Arrival"}}}])
        self.assertEqual(alerts.nas_for({"dep": "EWR", "arr": "ORD"}, items), [])           # ORD departures, EWR arrivals: not you
        self.assertEqual(sorted(n["airport"] for n in alerts.nas_for({"dep": "ORD", "arr": "EWR"}, items)), ["EWR", "ORD"])

    def test_ground_stop_push_extend_expand(self):
        L = {"id": "x", "dep": "BOS", "arr": "EWR", "display": "UA 1", "extra": True, "sched_out": T0 + H,
             "sched_in": T0 + 2 * H, "notified": {"watch": True}}
        L["flight"], _ = fa.flights_from_bootstrap(boot(origin="BOS", dest="EWR", out_s=T0 + H))
        items = sources.parse_events(self.EVENTS)
        keys = [a["title"] for a in alerts.evaluate(L, CFG, T0, items)]
        self.assertIn("EWR: Ground stop", keys)                 # pushes though BOS (ZBW) isn't in scope
        self.assertFalse(any("delay" in k.lower() for k in keys))  # FAA delay trends don't push by default
        msg = [a for a in alerts.evaluate(L, CFG, T0, []) + [] if a]  # nothing new
        self.assertEqual(msg, [])
        ev = json.loads(json.dumps(self.EVENTS))
        ev[0]["groundStop"]["endTime"] = "2026-10-14T18:30:00Z"
        ev[0]["groundStop"]["includedFacilities"].append("ZBW")
        a = alerts.evaluate(L, CFG, T0 + 60, sources.parse_events(ev))
        titles = [x["title"] for x in a]
        self.assertIn("EWR: ground stop extended", titles)
        self.assertIn("EWR: ground stop expanded", titles)
        self.assertIn("includes your departure from BOS (ZBW)", [x for x in a if "expanded" in x["title"]][0]["message"])


class Cleanup(unittest.TestCase):
    def test_old_screenshots_deleted(self):
        import tempfile
        import time as _t
        import server
        d = tempfile.mkdtemp()
        server.SHOT_DIR = d
        now = _t.time()
        live = {"id": "live", "sched_out": now + H, "sched_in": now + 4 * H}
        done = {"id": "done", "sched_out": now - 80 * H, "sched_in": now - 77 * H, "updated": now - 77 * H}
        server.STATE["legs"] = {"live": live, "done": done}
        for n in ("live", "live-inbound", "done", "done-chain1", "gone"):
            p = os.path.join(d, n + ".png")
            open(p, "wb").write(b"x")
            os.utime(p, (now - 72 * H, now - 72 * H))
        server.clean_screenshots(now)
        self.assertEqual(sorted(os.listdir(d)), ["live-inbound.png", "live.png"])


class AeroAPI(unittest.TestCase):
    """First look is AeroAPI from watch_hours out; page checks compare; AeroAPI again only on a change."""

    def setUp(self):
        import server
        import aeroapi
        self.server, self.aeroapi = server, aeroapi
        server.CFG.update(aeroapi_key="k", aeroapi_monthly_budget=5.0, aeroapi_cost_per_query=0.005,
                          aeroapi_max_per_leg=6, watch_hours=18)
        server.STATE["aero_calls"] = []
        server.STATE["debug"] = []
        self.calls, self.pages = [], []
        self.tail, self.inbound = None, "UAL567-1760400000-airline-1"
        self.out = T0 + 17 * H

        def query(key, ident, start, end):
            self.calls.append(ident)
            b = boot(fa_id="F-1", out_s=self.out, tail=self.tail, inbound_id=self.inbound)
            return [fa.normalize(list(b["flights"].values())[0])], 1
        aeroapi.query = query
        sources.fetch_live = lambda cs: None
        test = self

        class FB:
            client = None

            def load(self, url, name):
                test.pages.append(url)
                return boot(fa_id="F-1", out_s=test.out, tail=None, inbound_id=test.inbound), None
        self.browser = FB()

    def add(self, lid="a1", extra=False, out=None):
        out = out or self.out
        L = {"id": lid, "ident": "UAL1234", "display": "UA 1234", "dep": "EWR", "arr": "SFO", "extra": extra,
             "sched_out": out, "sched_in": out + 6 * H, "notified": {"watch": True}, "alerts": [], "next_check": 0}
        self.server.STATE["legs"][lid] = L
        return L

    def run_at(self, lid, t):
        import time as _t
        real = _t.time
        _t.time = lambda: t
        try:
            self.server.update_leg(self.browser, lid)
        finally:
            _t.time = real
        return self.server.STATE["legs"][lid]

    def test_first_lookup_is_aeroapi_then_pages_compare(self):
        self.add()
        L = self.run_at("a1", T0)                              # 17 h out: first look
        self.assertEqual(len(self.calls), 1)                   # one AeroAPI call...
        self.assertTrue(any("UAL567" in u for u in self.pages))  # ...then the pages right away, inbound included
        self.assertTrue(L.get("inbound"))
        self.assertTrue(L.get("compared"))
        sources_logged = [h.get("sources") for h in L.get("history") or []]
        self.assertTrue(any("AeroAPI" in (s or []) for s in sources_logged), sources_logged)
        self.run_at("a1", T0 + 120)
        self.assertEqual(len(self.calls), 1)

    def test_followed_wrong_airport_says_where_it_flies(self):
        L = self.add(extra=True)
        L["dep"] = "HOU"                                       # the flight really leaves EWR
        L = self.run_at("a1", T0)
        self.assertIsNone(L.get("flight"))
        self.assertEqual(L["aero"]["other_routes"], ["EWR→SFO"])
        self.assertIn("flies EWR→SFO, not HOU→SFO", L["note"])
        self.assertEqual(alerts.status_label(L, self.server.CFG), "Not found")

    def test_first_lookup_tail_has_source(self):
        self.tail = "N77"
        self.add()
        L = self.run_at("a1", T0)
        self.assertEqual(L["flight"]["tail"], "N77")
        self.assertEqual(L["flight"]["tail_source"], "AeroAPI (airline assignment)")

    def test_first_then_compare(self):
        self.add()
        self.run_at("a1", T0)
        self.run_at("a1", T0 + 120)                            # comparison by page check, no AeroAPI
        self.assertEqual(len(self.calls), 1)
        self.assertGreater(len(self.pages), 0)

    def test_nothing_before_watch_window(self):
        self.add(out=T0 + 30 * H)
        self.assertFalse(self.server.first_lookup_due(self.server.STATE["legs"]["a1"], T0))
        self.assertFalse(self.server.watched(self.server.STATE["legs"]["a1"], T0))

    def test_inbound_change_triggers_one_call(self):
        self.tail = "N1"
        self.add()
        self.run_at("a1", T0)
        self.inbound = "UAL903-1760390000-airline-9"                                  # page now links another airplane
        self.tail = "N2"
        self.run_at("a1", T0 + 120)
        self.run_at("a1", T0 + 2 * H)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.server.STATE["legs"]["a1"]["aero"]["tail"], "N2")

    def test_retries_only_while_no_tail(self):
        self.add()
        self.run_at("a1", T0)                                  # no tail yet
        self.run_at("a1", self.out - 5.9 * H)                  # 6 h retry
        self.tail = "N9"
        self.run_at("a1", self.out - 2.9 * H)                  # 3 h retry finds it
        self.run_at("a1", self.out - 1.4 * H)                  # tail known: no more
        self.assertEqual(len(self.calls), 3)
        L = self.server.STATE["legs"]["a1"]
        self.assertEqual(L["flight"]["tail"], "N9")
        self.assertEqual(L["flight"]["tail_source"], "AeroAPI (airline assignment)")

    def test_followed_undated_looks_up_then_waits(self):
        self.out = T0 + 30 * H
        L = self.add(lid="x1", extra=True, out=T0)
        L["sched_out"] = L["sched_in"] = T0
        self.assertTrue(self.server.first_lookup_due(L, T0))
        L = self.run_at("x1", T0)                              # learns it's 30 h away
        self.assertEqual((len(self.calls), len(self.pages)), (1, 0))
        self.assertEqual(L["sched_out"], self.out)
        self.assertFalse(self.server.watched(L, T0 + H))      # no screenshots until 18 h out
        self.assertTrue(self.server.watched(L, self.out - 17 * H))

    def test_budget_cap(self):
        self.server.CFG["aeroapi_monthly_budget"] = 0.01
        for i in range(4):
            self.add(lid=f"b{i}")
            self.run_at(f"b{i}", T0)
        self.assertEqual(len(self.calls), 2)

    def test_no_key_uses_pages(self):
        self.server.CFG["aeroapi_key"] = ""
        self.add()
        self.run_at("a1", T0)
        self.assertEqual(self.calls, [])
        self.assertGreater(len(self.pages), 0)

    def test_normalize(self):
        raw = {"fa_flight_id": "UAL1-x", "ident_icao": "UAL1", "ident_iata": "UA1", "registration": "N37502",
               "inbound_fa_flight_id": "UAL9-x", "origin": {"code_iata": "EWR", "code_icao": "KEWR",
               "timezone": "America/New_York"}, "destination": {"code_iata": "SFO"}, "gate_origin": "C71",
               "scheduled_out": "2026-10-14T18:05:00Z", "estimated_out": None, "actual_out": None}
        n = self.aeroapi.normalize(raw)
        self.assertEqual((n["tail"], n["inbound_fa_id"], n["origin"]["gate"]), ("N37502", "UAL9-x", "C71"))


class GateWindow(unittest.TestCase):
    def test_gate_waits_for_two_hours_but_delays_do_not(self):
        cfg = dict(CFG, gate_alert_hours=2)
        out = T0 + 8 * H
        L = {"id": "g", "ident": "UAL1234", "display": "UA 1234", "dep": "EWR", "arr": "SFO",
             "sched_out": out, "sched_in": out + 6 * H, "notified": {"watch": True}}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, gate="C71", out_e=out + 30 * 60))
        keys = [a["key"] for a in alerts.evaluate(L, cfg, T0, [])]      # 8 h out
        self.assertNotIn("dep_gate_assigned", keys)
        self.assertIn("delay", keys)                                     # important stuff still comes
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, gate="C80", out_e=out + 30 * 60))
        self.assertNotIn("dep_gate_assigned", [a["key"] for a in alerts.evaluate(L, cfg, T0 + H, [])])
        # the estimate is out + 30 min, so the 2-hour mark is out - 1.5 h
        self.assertNotIn("dep_gate_assigned", [x["key"] for x in alerts.evaluate(L, cfg, out - 1.6 * H, [])])
        a = [x for x in alerts.evaluate(L, cfg, out - 1.4 * H, []) if x["key"] == "dep_gate_assigned"]
        self.assertEqual(len(a), 1)                                      # first gate push at 2 h
        self.assertIn("C80", a[0]["title"])
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, gate="C90", out_e=out + 30 * 60))
        a = [x for x in alerts.evaluate(L, cfg, out - 1.0 * H, []) if x["key"] == "dep_gate_change"]
        self.assertIn("change", a[0]["title"])                           # later changes push



class InboundWindow(unittest.TestCase):
    def test_only_tight_scheduled_turns_push(self):
        cfg = dict(CFG, inbound_alert_minutes=60)
        out = T0 + 10 * H
        L = {"id": "i", "ident": "UAL1234", "display": "UA 1234", "dep": "EWR", "arr": "SFO",
             "sched_out": out, "sched_in": out + 6 * H, "notified": {"watch": True, "dep_gate": "C71"}}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out))

        def inbound(sched_in, est_in):
            inb, _ = fa.flights_from_bootstrap(boot(fa_id="IN", out_s=sched_in - 2 * H, origin="ORD",
                                                   dest="EWR", inbound_id=None, off_a=T0))
            inb["times"]["in"].update(scheduled=sched_in, estimated=est_in)
            return inb

        L["inbound"] = inbound(out - 3 * H, out - 10 * 60)      # scheduled 3 h before: progress pushes stay quiet...
        keys = [a["key"] for a in alerts.evaluate(L, cfg, T0, [])]
        self.assertEqual([k for k in keys if k.startswith("inbound")], ["inbound_late"])  # ...but a short turn always pushes
        L["notified"] = {"watch": True}
        L["notified"] = {"watch": True, "dep_gate": "C71"}
        L["inbound"] = inbound(out - 45 * 60, out - 10 * 60)    # scheduled 45 min before: pushes
        keys = [a["key"] for a in alerts.evaluate(L, cfg, T0, [])]
        self.assertIn("inbound_late", keys)
        self.assertIn("inbound_departed", keys)


class InboundCancel(unittest.TestCase):
    def test_cancel_pushes_even_on_long_turn(self):
        cfg = dict(CFG, inbound_alert_minutes=60)
        out = T0 + 10 * H
        L = {"id": "c", "ident": "UAL1234", "display": "UA 1234", "dep": "EWR", "arr": "SFO",
             "sched_out": out, "sched_in": out + 6 * H, "notified": {"watch": True, "dep_gate": "C71"}}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out))
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="IN", out_s=out - 6 * H, origin="ORD", dest="EWR",
                                               inbound_id=None, cancelled=True))
        inb["times"]["in"]["scheduled"] = out - 3 * H                     # 3-hour scheduled turn
        L["inbound"] = inb
        a = [x for x in alerts.evaluate(L, cfg, T0, []) if x["key"] == "inbound_cancelled"]
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["level"], "critical")


class LookupLog(unittest.TestCase):
    def test_changes_and_quiet_checks(self):
        import server
        L = {"id": "h", "ident": "UAL1", "dep": "EWR", "arr": "SFO", "sched_out": T0 + 5 * H,
             "sched_in": T0 + 11 * H}
        prev = server.snapshot(L)
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=T0 + 5 * H, gate="C71", tail="N1"))
        server.log_lookup(L, prev, T0, ["AeroAPI"])
        fields = {c["field"] for c in L["history"][0]["changes"]}
        self.assertTrue({"Gate", "Tail", "Scheduled out"} <= fields)
        same = server.snapshot(L)
        server.log_lookup(L, same, T0 + 60, ["FlightAware page data"])
        server.log_lookup(L, same, T0 + 120, ["FlightAware page data"])
        self.assertEqual(len(L["history"]), 2)
        self.assertEqual(L["history"][1]["quiet"], 2)
        L["flight"]["origin"]["gate"] = "C80"
        server.log_lookup(L, same, T0 + 180, ["FlightAware page data"])
        self.assertEqual(L["history"][-1]["changes"], [{"field": "Gate", "from": "C71", "to": "C80"}])


class FirstTailQuiet(unittest.TestCase):
    def test_first_tail_silent_unless_tracking_push_on(self):
        out = T0 + 10 * H
        for watch_push, extra, expect in ((False, False, 0), (True, True, 0), (True, False, 1)):
            L = {"id": "t", "ident": "UAL1", "display": "UA 1", "dep": "EWR", "arr": "SFO", "extra": extra,
                 "sched_out": out, "sched_in": out + 6 * H, "notified": {"watch": True}}
            L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, tail="N1"))
            L["flight"]["tail_source"] = "AeroAPI (airline assignment)"
            cfg = dict(CFG, notify_watch_start=watch_push)
            n = len([a for a in alerts.evaluate(L, cfg, T0, []) if a["key"] == "tail_identified"])
            self.assertEqual(n, expect, (watch_push, extra))
            L["flight"]["tail"] = "N2"                                    # a swap always pushes
            self.assertEqual(len([a for a in alerts.evaluate(L, cfg, T0 + 60, []) if a["key"] == "swap"]), extra is False and 1 or 0)



class SwapWindow(unittest.TestCase):
    def test_swaps_only_push_inside_two_hours(self):
        cfg = dict(CFG, swap_alert_hours=2)
        out = T0 + 8 * H
        L = {"id": "s", "ident": "UAL1", "display": "UA 1", "dep": "EWR", "arr": "SFO",
             "sched_out": out, "sched_in": out + 6 * H, "notified": {"watch": True, "tail": "N1"}}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, tail="N2"))
        self.assertEqual([a for a in alerts.evaluate(L, cfg, T0, []) if a["key"] == "swap"], [])   # 8 h out
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, tail="N3"))
        a = [x for x in alerts.evaluate(L, cfg, out - H, []) if x["key"] == "swap"]
        self.assertEqual(len(a), 1)
        self.assertIn("N2 → N3", a[0]["message"])


class Rules(unittest.TestCase):
    def setUp(self):
        import rules
        self.rules = rules
        self.R = rules.defaults()
        self.out = T0 + 5 * H

    def leg(self, **kw):
        L = {"id": "r", "ident": "UAL1", "display": "UA 1", "dep": "EWR", "arr": "SFO",
             "sched_out": self.out, "sched_in": self.out + 6 * H, "notified": {"watch": True, "dep_gate": "C1",
                                                                             "tail": "N1", "atype": "B39M"}}
        L.update(kw)
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=self.out, gate="C1", tail="N1"))
        return L

    def keys(self, L, t=T0, wx=None):
        return [a["key"] for a in alerts.evaluate(L, CFG, t, [], rules=self.R, wx=wx)]

    def test_new_events(self):
        L = self.leg()
        L["flight"]["origin"]["terminal"] = "C"
        self.keys(L)
        L["flight"]["origin"]["terminal"] = "A"
        L["flight"]["aircraft_type"] = "B738"
        L["flight"]["times"]["out"]["estimated"] = self.out - 20 * 60
        L["flight"]["route"] = "NEW ROUTE"
        k = self.keys(L, self.out - H)
        self.assertIn("aircraft_type", k)
        self.assertIn("early", k)
        L["notified"]["route"] = "OLD|"
        self.R["route_change"]["on"] = True
        self.R["terminal_change"]["hours"] = None
        L["flight"]["origin"]["terminal"] = "B"
        k = self.keys(L, self.out - H)
        self.assertIn("route_change", k)
        self.assertIn("terminal_change", k)

    def test_reinstated(self):
        L = self.leg()
        L["notified"]["cancelled"] = True
        self.assertIn("reinstated", self.keys(L))

    def test_weather(self):
        L = self.leg()
        self.R["wx_ifr"].update(on=True, hours=None)
        self.R["wx_gust"].update(on=True, hours=None)
        wx = {"KSFO": {"category": "IFR", "gust_kt": 35, "raw": "KSFO ..."}}
        k = self.keys(L, wx=wx)
        self.assertIn("wx_ifr", k)
        self.assertIn("wx_gust", k)
        self.assertEqual([x for x in self.keys(L, wx=wx) if x.startswith("wx")], [])   # once

    def test_flight_kind_filters(self):
        L = self.leg(extra=True)
        L["notified"]["cancelled"] = False
        L["flight"]["cancelled"] = True
        self.R["cancelled"]["friends"] = False
        self.assertNotIn("cancelled", self.keys(L))
        L2 = self.leg(dh=True)
        L2["flight"]["cancelled"] = True
        self.assertIn("cancelled", self.keys(L2))
        L3 = self.leg()
        L3["flight"]["cancelled"] = True
        self.R["cancelled"]["on"] = False
        self.assertNotIn("cancelled", self.keys(L3))

    def test_inbound_events(self):
        L = self.leg()
        L["notified"].pop("tail")
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="IN1", out_s=self.out - 3 * H, origin="ORD", dest="EWR",
                                               inbound_id=None, out_e=self.out - 3 * H + 40 * 60))
        inb["times"]["in"]["scheduled"] = self.out - 40 * 60
        inb["times"]["in"]["estimated"] = self.out - 40 * 60      # left late but making it up: not late
        L["inbound"] = inb
        self.assertNotIn("inbound_delayed", self.keys(json.loads(json.dumps(L))))
        inb["times"]["in"]["estimated"] = self.out - 10 * 60      # arriving 30 min late
        k = self.keys(L)
        self.assertIn("inbound_delayed", k)
        inb2, _ = fa.flights_from_bootstrap(boot(fa_id="IN2", out_s=self.out - 3 * H, origin="DEN", dest="EWR",
                                                inbound_id=None))
        L["inbound"] = inb2
        self.R["inbound_changed"].update(on=True, hours=None)
        self.assertIn("inbound_changed", self.keys(L))

    def test_clean_and_merge(self):
        r = self.rules.clean({"delay": {"on": False, "hours": "3", "params": {"threshold": "25"}},
                              "nas_gs": {"friends": True}, "bogus": {}}, self.R)
        self.assertEqual((r["delay"]["on"], r["delay"]["hours"], r["delay"]["params"]["threshold"]), (False, 3.0, 25))
        self.assertTrue(r["nas_gs"]["friends"])
        m = self.rules.merge(r, {"gate_alert_hours": 2})
        self.assertEqual(m["delay"]["params"]["threshold"], 25)
        seeded = self.rules.seed_from_config({"notify_watch_start": False, "inbound_alert_minutes": 45,
                                              "swap_alert_hours": 2, "min_turn_minutes": 35})
        self.assertFalse(seeded["watch_start"]["on"])
        self.assertEqual(seeded["inbound_delayed"]["params"]["max_sched_turn"], 45)
        self.assertNotIn("max_sched_turn", seeded["inbound_late"]["params"])


class ScheduleChange(unittest.TestCase):
    def test_added_removed_retimed(self):
        import time as _t
        import server
        pushed = []
        server.notify = lambda a: pushed.append(a["title"])
        now = _t.time()
        evs = [{"oid": "a", "start": now + 5 * H, "end": now + 8 * H, "allday": False, "summary": "1 EWR-ORD",
                "location": ""},
               {"oid": "b", "start": now + 20 * H, "end": now + 23 * H, "allday": False, "summary": "2 ORD-EWR",
                "location": ""}]
        server.schedule.load_file = lambda p: (b"x", now)
        server.schedule.parse_ics = lambda d, tz, n: [dict(e) for e in evs]
        server.STATE["legs"] = {}
        server.STATE["schedule"] = {"mtime": None, "error": None, "count": 0, "checked": None}
        server.CFG["airline_icao"] = "UAL"
        server.reload_schedule(force=True)                   # first load: silent
        self.assertEqual(pushed, [])
        evs[0]["start"] += 3600
        evs[0]["end"] += 3600
        evs.pop(1)
        evs.append({"oid": "c", "start": now + 30 * H, "end": now + 33 * H, "allday": False,
                    "summary": "3 EWR-DEN", "location": ""})
        server.reload_schedule(force=True)
        self.assertEqual(sorted(pushed), ["Schedule: flight added", "Schedule: flight removed",
                                          "Schedule: flight retimed"])


class Rotation(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.loads = []
        test = self
        # A ← B ← C(airborne) ← D(arrived)
        self.chain = {
            "B": dict(out_s=T0 + 1 * H, inbound_id="C"),
            "C": dict(out_s=T0 - 1 * H, inbound_id="D", off_a=T0 - 0.8 * H),
            "D": dict(out_s=T0 - 5 * H, inbound_id="E"),
        }

        class FB:
            client = None

            def load(self, url, name):
                fid = url.rsplit("/", 1)[-1]
                test.loads.append(fid)
                kw = test.chain.get(fid)
                if not kw:
                    return {"flights": {}}, None
                return boot(fa_id=fid, **kw), None
        self.browser = FB()

    def leg(self, inbound_kw):
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="A", **inbound_kw))
        return {"id": "r", "display": "UA 1", "inbound": inb}

    def test_walks_back_to_the_leg_in_flight_and_stops(self):
        L = self.leg(dict(out_s=T0 + 3 * H, inbound_id="B"))
        self.server.update_rotation(self.browser, L, T0)
        self.assertEqual([h["fa_id"] for h in L["rotation"]], ["A", "B", "C"])   # C airborne: stop
        self.assertEqual(self.loads, ["B", "C"])

    def test_stops_at_arrived_inbound(self):
        L = self.leg(dict(out_s=T0 - 3 * H, inbound_id="B"))
        L["inbound"]["times"]["in"]["actual"] = T0 - H
        self.server.update_rotation(self.browser, L, T0)
        self.assertEqual([h["fa_id"] for h in L["rotation"]], ["A"])
        self.assertEqual(self.loads, [])

    def test_finished_legs_are_not_reloaded(self):
        self.chain["C"] = dict(out_s=T0 - 3 * H, inbound_id="D")
        L = self.leg(dict(out_s=T0 + 3 * H, inbound_id="B"))
        self.server.update_rotation(self.browser, L, T0)
        L["rotation"][2]["times"]["in"]["actual"] = T0 - H                    # C has arrived
        self.loads.clear()
        self.server.update_rotation(self.browser, L, T0 + 20 * 60)
        self.assertEqual(self.loads, ["B"])                                   # only the open leg refreshes
        self.assertEqual(len(L["rotation"]), 3)


class PollAfterDelay(unittest.TestCase):
    def test_delay_keeps_fast_checks_from_the_original_time(self):
        import server
        server.CFG.update(poll_fast_minutes=3, poll_near_minutes=10, poll_far_minutes=30)
        out = T0 + 30 * 60                                                      # due out in 30 min
        L = {"id": "p", "sched_out": out, "sched_in": out + 3 * H}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, out_e=out + 1.5 * H))  # now 2 h out
        self.assertEqual(server.poll_seconds(L, T0), 180)


class NotifyLink(unittest.TestCase):
    def test_link_sources(self):
        import server
        server.ha = lambda *a, **k: {"data": {"slug": "83459ff1_flight_tracker"}}
        server.CFG["notification_url"] = ""
        server.find_ingress()
        self.assertEqual(server.INGRESS["path"], "/83459ff1_flight_tracker")
        server.CFG["notification_url"] = "http://192.168.1.51:8123/83459ff1_flight_tracker"
        server.find_ingress()
        self.assertEqual(server.INGRESS["path"], "/83459ff1_flight_tracker")

        def boom(*a, **k):
            raise OSError("no supervisor")
        server.ha = boom
        server.CFG["notification_url"] = ""
        os.environ["HOSTNAME"] = "83459ff1-flight-tracker"
        server.find_ingress()
        self.assertEqual(server.INGRESS["path"], "/83459ff1_flight_tracker")


class RotationFreshness(Rotation):
    def test_leg_saved_mid_flight_is_refetched_after_it_lands(self):
        self.chain["C"] = dict(out_s=T0 - 3 * H, inbound_id="D", off_a=T0 - 2.8 * H)
        L = self.leg(dict(out_s=T0 + 3 * H, inbound_id="B"))
        self.server.update_rotation(self.browser, L, T0 - 2 * H)              # C saved while airborne
        self.assertEqual(L["rotation"][-1]["fa_id"], "C")
        # C lands later; the copy we hold says arrived but was fetched before that
        L["rotation"][2]["times"]["in"]["actual"] = T0 - H
        self.chain["C"]["off_a"] = T0 - 2.8 * H
        self.loads.clear()
        self.server.update_rotation(self.browser, L, T0 - 0.5 * H)           # before: this copy was frozen forever
        self.assertIn("C", self.loads)
        L["rotation"][2]["times"]["in"]["actual"] = T0 - H                    # fetched after landing now
        self.loads.clear()
        self.server.update_rotation(self.browser, L, T0)
        self.assertNotIn("C", self.loads)                                     # final: never again

    def test_status_text_alone_does_not_mean_arrived(self):
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0))
        f["status_text"] = "En Route / Arriving in 30 minutes"
        self.assertNotEqual(fa.phase(f, T0), "arrived")
        f["status_text"] = "Arrived at gate"
        self.assertEqual(fa.phase(f, T0), "arrived")


class TrackThinning(unittest.TestCase):
    def test_long_track_keeps_its_start(self):
        pts = [[40 - i * 0.01, -74 - i * 0.02, 350, 450, T0 + i * 15] for i in range(2000)]
        t = fa.thin(pts, 300)
        self.assertEqual(len(t), 300)
        self.assertEqual(t[0], pts[0])            # departure end kept (this was being cut off)
        self.assertEqual(t[-1], pts[-1])
        raw = {"flightId": "x", "track": [{"coord": [p[1], p[0]], "alt": 350, "gs": 450, "timestamp": p[4]} for p in pts],
               "origin": {}, "destination": {}}
        self.assertEqual(fa.normalize(raw)["track"][0][0], 40.0)


class SameInbound(unittest.TestCase):
    def test_id_variants_are_the_same_flight(self):
        self.assertTrue(fa.same_flight("UAL147-1760398500-schedule-0001", "UAL147-1760398500-airline-0123p"))
        self.assertFalse(fa.same_flight("UAL147-1760398500-airline-1", "UAL148-1760398500-airline-1"))
        self.assertFalse(fa.same_flight("UAL147-1760398500-airline-1", "UAL147-1760484900-airline-1"))  # next day

    def test_no_second_aeroapi_call_for_the_same_inbound(self):
        import server
        server.CFG.update(aeroapi_key="k", aeroapi_monthly_budget=5.0, aeroapi_cost_per_query=0.005,
                          aeroapi_max_per_leg=6, watch_hours=18)
        L = {"id": "s", "ident": "UAL1", "sched_out": T0 + 6 * H, "sched_in": T0 + 9 * H,
             "aero": {"at": T0, "tail": "N797UA", "inbound_id": "UAL147-1760398500-schedule-0001", "calls": 1}}
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0 + 6 * H, inbound_id="UAL147-1760398500-airline-0123p"))
        self.assertEqual(server.aero_reason(L, f, T0 + 120), (None, None))


    def test_real_inbound_change_calls_once_unless_page_shows_same_tail(self):
        import server
        L = {"id": "s2", "ident": "UAL1", "sched_out": T0 + 6 * H, "sched_in": T0 + 9 * H,
             "aero": {"at": T0, "tail": "N797UA", "inbound_id": "UAL147-1760398500-schedule-0001", "calls": 1}}
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0 + 6 * H, tail=None, inbound_id="UAL903-1760390000-airline-9"))
        reason, _ = server.aero_reason(L, f, T0 + 120)
        self.assertIn("UAL903 vs AeroAPI UAL147", reason)
        f["tail"] = "N797UA"                                   # page shows the same airplane: no query
        self.assertEqual(server.aero_reason(L, f, T0 + 120), (None, None))
