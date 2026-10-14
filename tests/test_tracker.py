"""Run with: python3 -m unittest discover -s tests"""

import os
import sys
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


CFG = {"airline_icao": "UAL", "gate_alert_hours": 0, "inbound_alert_minutes": 0, "delay_threshold_minutes": 15, "delay_step_minutes": 10, "min_turn_minutes": 35,
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
        self.assertEqual([x["key"] for x in a], ["watch", "dep_gate"])
        self.assertIn("C71", a[1]["message"])
        self.assertEqual(alerts.evaluate(L, CFG, T0 + 60, []), [])  # nothing new → nothing pushed

        L["flight"], _ = fa.flights_from_bootstrap(boot(gate="C80", out_e=T0 + 10 * H + 25 * 60, tail="N12345"))
        keys = [x["key"] for x in alerts.evaluate(L, CFG, T0 + 120, [])]
        self.assertEqual(keys, ["dep_gate", "delay", "tail"])
        # small wobble in the delay is not news
        L["flight"], _ = fa.flights_from_bootstrap(boot(gate="C80", out_e=T0 + 10 * H + 30 * 60, tail="N12345"))
        self.assertEqual(alerts.evaluate(L, CFG, T0 + 180, []), [])
        # back on time
        L["flight"], _ = fa.flights_from_bootstrap(boot(gate="C80", out_e=T0 + 10 * H + 5 * 60, tail="N12345"))
        a = alerts.evaluate(L, CFG, T0 + 240, [])
        self.assertEqual([x["key"] for x in a], ["delay"])
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
        self.assertIn("turn", keys)
        self.assertIn("inbound_off", keys)
        self.assertEqual([x for x in a if x["key"] == "nas"][0]["title"], "EWR: Ground delay program")
        self.assertEqual(len([x for x in a if x["key"] == "nas"]), 1)
        self.assertEqual(alerts.evaluate(L, CFG, T0 + 60, nas), [])
        probs = [p["text"] for p in alerts.problems(L, CFG, nas)]
        self.assertTrue(any("turn" in p for p in probs))
        self.assertIn("EWR: Ground delay program", probs)

    def test_cancel(self):
        L = self.leg()
        L["notified"] = {"watch": True, "dep_gate": "C71"}
        L["flight"], _ = fa.flights_from_bootstrap(boot(cancelled=True))
        a = alerts.evaluate(L, CFG, T0, [])
        self.assertEqual([x["key"] for x in a], ["cancelled"])
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


if __name__ == "__main__":
    unittest.main()


class Lifecycle(unittest.TestCase):
    def test_stops_at_takeoff_and_by_date(self):
        os.environ.setdefault("FT_DATA", "/nonexistent")
        import server
        L = {"id": "a", "sched_out": T0 + H, "sched_in": T0 + 4 * H, "dep": "EWR", "arr": "SFO"}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=T0 + H))
        self.assertTrue(server.watched(L, T0))
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=T0 + H, off_a=T0 + 1.2 * H))
        self.assertTrue(server.finished(L, T0 + 1.3 * H))
        self.assertFalse(server.watched(L, T0 + 1.3 * H))
        X = dict(L, extra=True)
        self.assertFalse(server.watched(X, T0 + 1.3 * H))

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
        a = [x for x in alerts.evaluate(L, CFG, T0, []) if x["key"] == "tail"]
        self.assertEqual(len(a), 1)
        self.assertIn("in flight", a[0]["message"])

        # 80 minutes out, our own transponder shows a different airplane → swap
        live_tails["UAL1234"] = "N888UA"
        f2, _ = fa.flights_from_bootstrap(boot(out_s=T0 + 6 * H, tail=None, inbound_id="UAL567-x"))
        L["flight"] = f2
        server.resolve_tail(FB(), L, f2, T0 + 6 * H - 80 * 60)
        self.assertEqual(f2["tail"], "N888UA")
        a = [x for x in alerts.evaluate(L, CFG, T0 + 6 * H - 80 * 60, []) if x["key"] == "tail"]
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

    def test_nas_only_for_own_legs(self):
        nas = [{"airport": "EWR", "kind": "gs", "label": "Ground stop", "reason": "", "detail": "", "key": "k"}]
        self.assertEqual(alerts.nas_for({"dep": "EWR", "arr": "SFO", "extra": True}, nas), [])
        self.assertEqual(len(alerts.nas_for({"dep": "EWR", "arr": "SFO"}, nas)), 1)


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
        self.tail, self.inbound = None, "IN-1"
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
        self.assertEqual((len(self.calls), len(self.pages)), (1, 0))
        self.assertEqual(L["source"], "AeroAPI")
        self.run_at("a1", T0 + 120)

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
        self.inbound = "IN-2"                                  # page now links another airplane
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
        self.assertNotIn("dep_gate", keys)
        self.assertIn("delay", keys)                                     # important stuff still comes
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, gate="C80", out_e=out + 30 * 60))
        self.assertNotIn("dep_gate", [a["key"] for a in alerts.evaluate(L, cfg, T0 + H, [])])
        # the estimate is out + 30 min, so the 2-hour mark is out - 1.5 h
        self.assertNotIn("dep_gate", [x["key"] for x in alerts.evaluate(L, cfg, out - 1.6 * H, [])])
        a = [x for x in alerts.evaluate(L, cfg, out - 1.4 * H, []) if x["key"] == "dep_gate"]
        self.assertEqual(len(a), 1)                                      # first gate push at 2 h
        self.assertIn("C80", a[0]["title"])
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=out, gate="C90", out_e=out + 30 * 60))
        a = [x for x in alerts.evaluate(L, cfg, out - 1.0 * H, []) if x["key"] == "dep_gate"]
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

        L["inbound"] = inbound(out - 3 * H, out - 10 * 60)      # scheduled 3 h before: late, but no push
        keys = [a["key"] for a in alerts.evaluate(L, cfg, T0, [])]
        self.assertFalse(any(k.startswith("inbound") or k == "turn" for k in keys))
        L["notified"] = {"watch": True, "dep_gate": "C71"}
        L["inbound"] = inbound(out - 45 * 60, out - 10 * 60)    # scheduled 45 min before: pushes
        keys = [a["key"] for a in alerts.evaluate(L, cfg, T0, [])]
        self.assertIn("turn", keys)
        self.assertIn("inbound_off", keys)
