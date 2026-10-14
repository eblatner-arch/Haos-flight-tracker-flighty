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


CFG = {"airline_icao": "UAL", "delay_threshold_minutes": 15, "delay_step_minutes": 10, "min_turn_minutes": 35,
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
