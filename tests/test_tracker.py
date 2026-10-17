from unittest import mock
from datetime import datetime, timezone
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

# the tests stub fetch_live; route lookups and the all-aircraft call go through it (no network)
sources.fetch_route = lambda *a, **k: None
sources.fetch_live_all = lambda cs: (lambda x: [x] if x else [])(sources.fetch_live(cs))

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
        # a GDP at your departure airport only holds your inbound, and it's already airborne: no push
        self.assertEqual(len([x for x in a if x["key"].startswith("nas")]), 0)
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

    def test_diversion_counts_as_one_journey(self):
        extra = ("2026-07-27,UAL,1611,IAD,EWR,,D8,,C101,false,,2026-07-27T12:46,2026-07-27T21:51,2026-07-27T17:17,"
                 "2026-07-27T22:21,2026-07-27T18:01,2026-07-27T23:06,2026-07-27T15:50,2026-07-27T23:16,Boeing 737 MAX 9,N17591\n")
        self.h.import_flighty("x.csv", (self.CSV + extra).encode())
        lk = self.h.links()
        div = self.h.summary(self.h.get("2026-07-27_UAL1611_FLL"), lk)
        cont = self.h.summary(self.h.get("2026-07-27_UAL1611_IAD"), lk)
        self.assertEqual(round(div["arr_delay"]), 446)              # into EWR against the original 15:50
        self.assertEqual((cont["arr_delay"], cont["dep_delay"], cont["continuation_of"]), (None, None, div["id"]))
        self.assertEqual(div["final_ap"], "EWR")
        self.assertLess(div["distance_nm"], 900)                    # FLL-IAD, not FLL-EWR
        st = self.h.stats(self.h.flights(), lk)
        self.assertEqual((st["flights"], st["on_time_pct"]), (3, 50))

    def test_clear_export_backup_restore(self):
        import tempfile
        self.h.import_flighty("x.csv", self.CSV.encode())
        self.h.upsert({"id": "2026-10-12_UAL1_EWR", "date": "2026-10-12", "ident": "UAL1", "dep": "EWR", "arr": "SFO",
                       "source": "tracker", "times": {"out": {"s": T0, "a": T0}}}, pts=[[40.6, -74.1, 0, 0, T0]])
        csvb = self.h.export_csv()
        blob = self.h.make_backup({"manual": []})
        self.assertEqual(self.h.clear("flighty"), 2)                 # imports gone, tracked flight stays
        self.assertEqual([r["id"] for r in self.h.flights()], ["2026-10-12_UAL1_EWR"])
        self.assertEqual(self.h.import_flighty("e.csv", csvb)["added"], 2)   # the export re-imports
        self.h.clear()
        self.assertEqual(self.h.flights(), [])
        self.h.init(tempfile.mkdtemp())                              # a new box
        res, extra = self.h.restore_backup(blob)
        self.assertEqual((res["added"], extra["manual"]), (3, []))
        self.assertEqual(len(self.h.track("2026-10-12_UAL1_EWR")), 1)

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


class InboundAdsb(unittest.TestCase):
    def test_swap_seen_on_inbound_transponder(self):
        import server
        out_s = T0 + 2 * H
        f = {"times": {k: {"scheduled": out_s if k == "out" else None, "estimated": None, "actual": None}
                       for k in ("out", "off", "on", "in")}, "inbound_fa_id": "UAL2291-1", "tail": "N38467"}
        inb = {"ident": "UAL2291", "display_ident": "UA2291", "origin": {"iata": "PHX"}, "destination": {"iata": "EWR"},
               "times": {"out": {"scheduled": T0 - H, "estimated": None, "actual": T0 - H}, "off": {"scheduled": None, "estimated": None, "actual": T0 - 0.8 * H},
                         "on": {"scheduled": None, "estimated": None, "actual": None}, "in": {"scheduled": T0 + H, "estimated": None, "actual": None}}}
        L = {"id": "a", "display": "UA 2785", "ident": "UAL2785", "dep": "EWR", "arr": "MIA", "sched_out": out_s,
             "sched_in": out_s + 3 * H, "flight": f, "inbound": inb, "tail_info": {"tail": "N38467", "kind": "aeroapi"}}
        server.STATE["legs"] = {"a": L}
        server.CFG["adsb_inbound_minutes"] = 3
        while not server.REFRESH_Q.empty():
            server.REFRESH_Q.get_nowait()
        real = server.sources.fetch_live
        server.sources.fetch_live = lambda ident, *a, **k: {"lat": 1, "lon": 2, "tail": "N37560"}
        try:
            server.inbound_adsb(T0)
            self.assertEqual(server.REFRESH_Q.get_nowait(), "a")                  # different tail: full check now
            h = L["history"][-1]
            self.assertEqual((h["sources"], h["seen"][0]["tail"]), (["adsb.lol"], "N37560"))
            server.inbound_adsb(T0 + 60)                                          # inside the interval: nothing
            self.assertEqual(len(L["history"]), 1)
            server.inbound_adsb(T0 + 200)                                         # same answer: one log line, counted
            self.assertEqual((len(L["history"]), L["history"][-1]["quiet"]), (1, 2))
            server.resolve_tail(None, L, f, T0 + 210)                             # the full check takes it
            self.assertEqual((f["tail"], L["tail_info"]["kind"]), ("N37560", "chain"))
        finally:
            server.sources.fetch_live = real


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



class Stdds(unittest.TestCase):
    """Real STDDS messages captured from the live feed."""
    def setUp(self):
        import swim
        swim.STATE["status"] = "connected"
        swim.EVENTS.clear(); swim.LIVE.clear(); swim.WATCH.clear()
        self.here = os.path.dirname(os.path.abspath(__file__))

    def feed(self, name):
        import swim
        with open(os.path.join(self.here, name)) as fh:
            swim.handle_stdds(fh.read())

    def test_n_number(self):
        import swim
        self.assertEqual([swim.n_number(h) for h in ("aaf006", "a00001", "adf7c7", "4ca123")],
                         ["N80347", "N1", "N99999", None])

    def test_approach_radar_position_with_tail(self):
        import swim
        swim.WATCH.add("N80347")
        self.feed("stdds_tais.xml")
        live = swim.live_all("N80347")
        self.assertEqual(len(live), 1)
        a = live[0]
        self.assertEqual((a["tail"], a["hex"], a["alt_ft"], a["type"]), ("N80347", "aaf006", 2300, "C172"))
        self.assertAlmostEqual(a["lat"], 45.51658)
        self.assertIn("approach radar", a["src"])

    def test_surface_events_land_on_the_flight(self):
        import server
        import swim
        swim.WATCH.add("DAL517")
        self.feed("stdds_smes_event.xml")
        so = datetime(2026, 10, 16, 21, 10, tzinfo=timezone.utc).timestamp()
        now = datetime(2026, 10, 17, 2, 50, tzinfo=timezone.utc).timestamp()
        f, _ = fa.flights_from_bootstrap(boot(out_s=so, origin="ATL", dest="SFO", off_a=so + 900))
        L = {"id": "d1", "ident": "DAL517", "dep": "ATL", "arr": "SFO", "sched_out": so}
        server.swim_check(L, f, "SFO")
        server.swim_overlay(L, f, now)
        z = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        self.assertEqual(f["times"]["on"]["actual"], z("2026-10-17T02:46:54.639Z"))
        self.assertEqual(f["time_src"]["on"]["actual"], "SFO surface radar")
        sa = f["swim"]["surf_arr"]
        self.assertEqual((sa["landed_rwy"], sa["runway_exit"], sa["status"]),
                         ("10L/28R", z("2026-10-17T02:47:41.646Z"), "onramp"))
        self.assertIn("DAL517", swim.PENDING)

    def test_tower_departure_clearance(self):
        import server
        import swim
        swim.WATCH.add("SWA3238")
        self.feed("stdds_tdes.xml")
        so = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc).timestamp()
        f, _ = fa.flights_from_bootstrap(boot(out_s=so, origin="MDW", dest="MKE"))
        L = {"id": "d2", "ident": "SWA3238", "dep": "MDW", "arr": "MKE", "sched_out": so}
        server.swim_check(L, f, "MKE")
        server.swim_overlay(L, f, so)
        self.assertEqual(f["swim"]["tower_dep"]["gate"], "A16")

    def test_unknown_targets_have_no_callsign(self):
        import swim
        _, reps = swim.parse_surface("""<a:asdexMsg xmlns:a="x"><airport>KEWR</airport><positionReport full="true"><track>9</track>
            <flightId><aircraftId>UNKN</aircraftId></flightId><flightInfo><tgtType>unknown</tgtType></flightInfo>
            <position><latitude>40.69</latitude><longitude>-74.17</longitude></position></positionReport></a:asdexMsg>""")
        self.assertNotIn("cs", reps[0])

    def test_datalink_clearance(self):
        import server
        import swim
        swim.WATCH.add("SWA9001")
        self.feed("stdds_pdc.xml")
        ev = [e for e in swim.events("SWA9001") if e["type"] == "TDLSCSPMessage"][0]
        p = ev["pdc"]
        self.assertEqual((p["tail"], p["cleared_to"], p["departure"], p["altitude"], p["dep_freq"], p["runway"], p["squawk"], p["at"]),
                         ("N494WN", "KMCO", "ILEXY4.ZENZI", "4000", "127.225", "18L/36R", "2531", "2026-10-17T04:27:38Z"))
        self.assertTrue(p["route"].startswith("KAUS ILEXY4 ZENZI DAS"))
        sw = {}
        server._swim_take(sw, ev)
        self.assertEqual(sw["pdc"]["response"], "WILCO")

    def test_surface_positions_real(self):
        import swim
        swim.WATCH_AIRPORTS.clear(); swim.WATCH_AIRPORTS.add("KSEA")
        swim.SURFACE.clear()
        self.feed("stdds_asdex.xml")
        self.assertGreater(len(swim.surface("KSEA")), 1)


class SurfaceWanted(unittest.TestCase):
    def test_zoomed_airport_shows_even_en_route(self):
        import server
        import swim
        swim.STDDS_STATE["status"] = "connected"
        now = T0
        f, _ = fa.flights_from_bootstrap(boot(out_s=now - 2 * H, origin="EWR", dest="SFO", off_a=now - H))
        server.STATE["legs"]["sw1"] = {"id": "sw1", "ident": "UAL1234", "dep": "EWR", "arr": "SFO",
                                        "sched_out": now - 2 * H, "sched_in": now + 4 * H, "flight": f}
        with mock.patch.object(server, "watched", return_value=True):
            self.assertEqual(server.api_surface("sw1", now)["targets"], [])          # en route: nothing by itself
            self.assertEqual(server.api_surface("sw1", now, "EWR")["airport"], "EWR")  # zoomed on EWR
            self.assertNotIn("airport", server.api_surface("sw1", now, "ORD"))      # not this flight's airport
        del server.STATE["legs"]["sw1"]


class AfterLanding(unittest.TestCase):
    def test_taxi_in_keeps_estimate_and_takes_airline_gate_time(self):
        import server
        land = T0
        f, _ = fa.flights_from_bootstrap(boot(out_s=land - 2 * H, origin="EWR", dest="IAD", off_a=land - H))
        f["times"]["in"]["estimated"] = land + 7 * 60          # FlightAware's own guess
        L = {"id": "al", "ident": "UAL1784", "dep": "EWR", "arr": "IAD",
             "swim": {"seen": 1, "on": land, "in_shown": land + 4 * 60}}
        server.swim_overlay(L, f, land + 60)
        self.assertEqual(f["times"]["in"]["estimated"], land + 4 * 60)   # held, not FlightAware's
        L["swim"]["airline"] = {"src": "UAL", "in": land + 5 * 60, "at": land + 30}
        server.swim_overlay(L, f, land + 90)
        self.assertEqual((f["times"]["in"]["estimated"], f["time_src"]["in"]["estimated"]), (land + 5 * 60, "United"))
        self.assertEqual(server._tower_state("AMA_TAXI_IN"), "taxiing in")


class NasFromSwim(unittest.TestCase):
    """Ground stops and delay programs from SWIM woven with the NAS status page."""
    def setUp(self):
        import server
        server.NAS_IDS.clear()

    def test_gdp_parsed_and_merged(self):
        import server
        import swim
        here = os.path.dirname(os.path.abspath(__file__))
        swim.NAS.clear(); swim.NAS_CANCELLED.clear()
        with open(os.path.join(here, "swim_gdp.xml")) as fh:
            swim._flow(fh.read())
        g = swim.NAS[("JFK", "gdp")]
        self.assertEqual((g["avg"], g["max"], g["reason"], g["scope"][0]), ("59.9", "133", "Thunderstorms", "ZLA"))
        now = g["start"] + 600
        page = [{"airport": "JFK", "kind": "gdp", "label": "Ground delay program", "key": "JFK|gdp|page1",
                 "advisory": "https://x/adv", "start": g["start"], "end": g["end"]},
                {"airport": "BWI", "kind": "delay", "label": "Departure delay trend", "key": "BWI|delay|Departure"}]
        # SWIM first: the program appears before the page has it
        out = server.merge_nas([], dict(swim.NAS), {}, None, now)
        self.assertEqual([(n["airport"], n["source"]) for n in out], [("JFK", "FAA SWIM")])
        key = out[0]["key"]
        # then the page catches up: same program, same key (no second alert), page extras kept
        out = server.merge_nas(page, dict(swim.NAS), {}, now, now)
        jfk = [n for n in out if n["airport"] == "JFK"][0]
        self.assertEqual((jfk["key"], jfk["advisory"], jfk["detail"]), (key, "https://x/adv", "avg 60 min, max 133 min"))
        self.assertIn("BWI", [n["airport"] for n in out])          # delay trends still from the page

    def test_real_ground_stop(self):
        """LAS ground stop 05:30–06:30Z (staffing), departures from ZLA ZOA ZAB, captured live."""
        import swim
        swim.NAS.clear(); swim.NAS_CANCELLED.clear()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "swim_gs.xml")) as fh:
            swim._flow(fh.read())
        g = swim.NAS[("LAS", "gs")]
        z = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        self.assertEqual((g["start"], g["end"], g["scope"], g["reason"]),
                         (z("2026-10-17T05:30:00Z"), z("2026-10-17T06:30:00Z"), ["ZLA", "ZOA", "ZAB"], "Staffing"))
        self.assertEqual(g["detail"], "avg 34 min, max 207 min, expected avg 93 min")

    def test_swim_cancel_beats_stale_page(self):
        import server
        page = [{"airport": "SAN", "kind": "gs", "label": "Ground stop", "key": "SAN|gs|1"}]
        out = server.merge_nas(page, {}, {("SAN", "gs"): T0 + 100}, T0, T0 + 120)
        self.assertEqual(out, [])
        # the page is read again a few minutes later and still lists it: stays gone
        out = server.merge_nas(page, {}, {("SAN", "gs"): T0 + 100}, T0 + 300, T0 + 320)
        self.assertEqual(out, [])
        # a new ground stop that started after the cancel does show
        new = [{"airport": "SAN", "kind": "gs", "label": "Ground stop", "key": "SAN|gs|2", "start": T0 + 900}]
        self.assertEqual(len(server.merge_nas(new, {}, {("SAN", "gs"): T0 + 100}, T0 + 1000, T0 + 1000)), 1)

class AirportLayout(unittest.TestCase):
    def test_osm_to_features(self):
        import server
        data = {"elements": [
            {"type": "way", "tags": {"aeroway": "runway", "ref": "04L/22R"},
             "geometry": [{"lat": 40.67, "lon": -74.18}, {"lat": 40.70, "lon": -74.16}]},
            {"type": "way", "tags": {"aeroway": "taxilane", "ref": "P"},
             "geometry": [{"lat": 40.69, "lon": -74.18}, {"lat": 40.691, "lon": -74.179}]},
            {"type": "way", "tags": {"aeroway": "terminal", "name": "Terminal A"},
             "geometry": [{"lat": 40.68, "lon": -74.17}, {"lat": 40.681, "lon": -74.17}, {"lat": 40.681, "lon": -74.171}]},
            {"type": "node", "lat": 40.6801, "lon": -74.1702, "tags": {"aeroway": "gate", "ref": "A12"}},
            {"type": "node", "lat": 40.6801, "lon": -74.1702, "tags": {"aeroway": "holding_position"}},
            {"type": "way", "tags": {"aeroway": "windsock"}, "geometry": [{"lat": 1, "lon": 1}, {"lat": 2, "lon": 2}]},
        ]}
        f = server._layout_from_osm(data)
        self.assertEqual([(x["k"], x.get("ref") or x.get("name")) for x in f],
                         [("runway", "04L/22R"), ("taxiway", "P"), ("terminal", "Terminal A"), ("gate", "A12")])
        self.assertEqual(f[3]["g"], [40.6801, -74.1702])

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
        # our own flight shows up on the ground at EWR; the earlier leg in the air
        sources.fetch_live = lambda cs, *a: ({"lat": 40.69, "lon": -74.17, "ground": True, "tail": live_tails[cs]} if cs == "UAL1234"
                                             else {"lat": 1, "lon": 2, "tail": live_tails[cs]}) if cs in live_tails else None

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
        sources.fetch_live = lambda cs, *a: None

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
        self.assertNotIn("held", alerts.nas_text(g, None))   # no "not held" notes
        self.assertFalse(any(p.get("nas") and "delay" in p["text"].lower()
                             for p in alerts.problems({"dep": "EWR", "arr": "MCO", "flight": None}, {}, items)))
        self.assertNotIn("airborne", alerts.nas_text([n for n in alerts.nas_for(
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
        sources.fetch_live = lambda cs, *a: None
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
        self.assertIn("as EWR→SFO, not HOU→SFO", L["note"])
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
        server.schedule.parse_ics = lambda d, tz, n, days=60: [dict(e) for e in evs]
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


class OwnTransponderEarlierLeg(unittest.TestCase):
    def test_same_number_earlier_leg_ignored(self):
        import server
        out_s = T0 + H
        f = {"times": {k: {"scheduled": out_s if k == "out" else None, "estimated": None, "actual": None}
                       for k in ("out", "off", "on", "in")}, "inbound_fa_id": "UAL210-1", "tail": None}
        L = {"id": "o", "display": "UA 1268", "ident": "UAL1268", "dep": "EWR", "arr": "MCO", "sched_out": out_s,
             "adsb_inbound": {"tail": "N27526", "at": T0, "root": "UAL210-1", "source": "inbound"}}
        real_all, real_route = sources.fetch_live_all, sources.fetch_route
        sources.fetch_route = lambda *a, **k: ["DFW", "EWR", "MCO"]
        try:
            sources.fetch_live_all = lambda cs: [{"lat": 40.69, "lon": -74.17, "ground": True, "tail": "N47298"}]
            ac, why, odd = server.own_live(L, f, T0)
            self.assertIsNone(ac)
            self.assertIn("DFW→EWR", why)
            sources.fetch_live_all = lambda cs: [{"lat": 40.69, "lon": -74.17, "ground": True, "tail": "N27526"}]
            ac, why, odd = server.own_live(L, f, T0)
            self.assertEqual(ac["tail"], "N27526")
            # still in the air on the earlier leg: not at EWR
            sources.fetch_live_all = lambda cs: [{"lat": 35.0, "lon": -90.0, "ground": False, "alt_ft": 35000, "tail": "N47298"}]
            self.assertIsNone(server.own_live(L, f, T0)[0])
        finally:
            sources.fetch_live_all, sources.fetch_route = real_all, real_route


class OwnTransponderNoRoute(unittest.TestCase):
    def test_inbound_mismatch_without_route(self):
        import server
        out_s = T0 + H
        f = {"times": {k: {"scheduled": out_s if k == "out" else None, "estimated": None, "actual": None}
                       for k in ("out", "off", "on", "in")}, "inbound_fa_id": "UAL210-1", "tail": None}
        L = {"id": "o2", "display": "UA 1268", "ident": "UAL1268", "dep": "EWR", "arr": "MCO", "sched_out": out_s,
             "adsb_inbound": {"tail": "N27526", "at": T0, "root": "UAL210-1", "source": "inbound"}}
        real_all = sources.fetch_live_all
        try:
            sources.fetch_live_all = lambda cs: [{"lat": 40.69, "lon": -74.17, "ground": True, "tail": "N47298"}]
            ac, why, odd = server.own_live(L, f, T0)  # adsb.lol knows no route for the number
            self.assertIsNone(ac)
            self.assertIn("N27526", why)
            self.assertEqual(odd, "N47298")  # handed to AeroAPI as a possible swap
        finally:
            sources.fetch_live_all = real_all


class StaleRoute(unittest.TestCase):
    def test_route_without_this_leg_is_ignored(self):
        import server
        out_s = T0 + H
        f = {"times": {k: {"scheduled": out_s if k == "out" else None, "estimated": None, "actual": None}
                       for k in ("out", "off", "on", "in")}, "inbound_fa_id": "X-1", "tail": None}
        L = {"id": "s", "display": "UA 2328", "ident": "UAL2328", "dep": "EWR", "arr": "LAX", "sched_out": out_s}
        real_all, real_route = sources.fetch_live_all, sources.fetch_route
        sources.fetch_route = lambda *a, **k: ["RSW", "EWR", "SFO"]   # stale: our leg is EWR→LAX
        sources.fetch_live_all = lambda cs: [{"lat": 40.69, "lon": -74.17, "ground": True, "tail": "N1"}]
        try:
            ac, why, odd = server.own_live(L, f, T0)
            self.assertEqual(ac["tail"], "N1")  # no inbound transponder to compare and no trusted route: taken
        finally:
            sources.fetch_live_all, sources.fetch_route = real_all, real_route


class SameNumberFromFlightAware(unittest.TestCase):
    def test_flightaware_history_confirms_or_overrides_route(self):
        import server
        out_s = T0 + H
        L = {"id": "fa", "ident": "UAL1268", "dep": "EWR", "arr": "MCO", "sched_out": out_s}
        dfw = {"origin": {"iata": "DFW"}, "destination": {"iata": "EWR"},
               "times": {k: {"scheduled": None, "estimated": None, "actual": None} for k in ("out", "off", "on", "in")}}
        dfw["times"]["in"]["scheduled"] = out_s - 80 * 60
        mine = {"origin": {"iata": "EWR"}, "destination": {"iata": "MCO"},
                "times": {k: {"scheduled": out_s if k == "out" else None, "estimated": None, "actual": None} for k in ("out", "off", "on", "in")}}
        server.note_same_number(L, [dfw, mine])
        self.assertEqual(L["same_number_earlier"]["leg"]["from"], "DFW")
        server.note_same_number(L, [])  # nothing new: keep what we had
        self.assertEqual(L["same_number_earlier"]["leg"]["from"], "DFW")
        L2 = {"id": "fa2", "ident": "UAL1268", "dep": "EWR", "arr": "MCO", "sched_out": out_s}
        dfw["times"]["in"]["scheduled"] = out_s - 30 * 3600  # yesterday's: not today's earlier leg
        server.note_same_number(L2, [dfw, mine])
        self.assertIsNone(L2["same_number_earlier"]["leg"])
        # route says DFW-EWR-MCO, FlightAware shows no earlier leg today: route ignored
        f = {"times": {k: {"scheduled": out_s if k == "out" else None, "estimated": None, "actual": None}
                       for k in ("out", "off", "on", "in")}, "inbound_fa_id": "X-1", "tail": None}
        real_all, real_route = sources.fetch_live_all, sources.fetch_route
        sources.fetch_route = lambda *a, **k: ["DFW", "EWR", "MCO"]
        sources.fetch_live_all = lambda cs: [{"lat": 40.69, "lon": -74.17, "ground": True, "tail": "N1"}]
        try:
            ac, why, odd = server.own_live(L2, f, T0)
            self.assertEqual(ac["tail"], "N1")
        finally:
            sources.fetch_live_all, sources.fetch_route = real_all, real_route



class NasOnlyBeforeTakeoff(unittest.TestCase):
    def leg(self, **kw):
        L = {"id": "n", "ident": "UAL249", "display": "UA 249", "dep": "ORD", "arr": "EWR", "sched_out": T0 + H,
             "sched_in": T0 + 3 * H, "notified": {"watch": True}}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=T0 + H, origin="ORD", dest="EWR", **kw))
        return L

    def test_arrival_ground_stop(self):
        gs = [{"airport": "EWR", "kind": "gs", "label": "Ground stop", "reason": "thunderstorms", "detail": "",
               "scope": ["ZAU"], "key": "EWR|gs|1"}]
        L = self.leg()                                   # still at the gate: pushed
        self.assertEqual([x["key"] for x in alerts.evaluate(L, CFG, T0, gs) if x["key"].startswith("nas")], ["nas_gs"])
        L = self.leg(off_a=T0 - 600)                     # airborne: nothing
        self.assertEqual([x for x in alerts.evaluate(L, CFG, T0, gs) if x["key"].startswith("nas")], [])


class Diversion(unittest.TestCase):
    def test_cancelled_after_takeoff_and_new_destination(self):
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0, origin="LAX", dest="MDT", off_a=T0 + 600))
        f["cancelled"] = True                              # AeroAPI's view of the original LAX→EWR
        fa.fix_diversion(f, "EWR")
        self.assertFalse(f["cancelled"])
        self.assertEqual((f["diverted_to"], f["planned_arr"]), ("MDT", "EWR"))
        L = {"id": "d", "ident": "UAL2127", "display": "UA 2127", "dep": "LAX", "arr": "EWR", "sched_out": T0,
             "sched_in": T0 + 5 * H, "notified": {"watch": True, "cancelled": True}, "flight": f}
        a = alerts.evaluate(L, CFG, T0 + 3600, [])
        keys = [x["key"] for x in a]
        self.assertIn("diverted", keys)
        self.assertNotIn("reinstated", keys)
        self.assertEqual([x for x in a if x["key"] == "diverted"][0]["title"], "UA 2127 diverted to MDT")
        self.assertEqual(alerts.status_label(L, CFG), "Diverted")
        import history
        rec = history.from_leg(L, "manual")
        self.assertEqual((rec["arr"], rec["diverted_to"]), ("EWR", "MDT"))


class RouteDiversion(unittest.TestCase):
    def test_route_ending_elsewhere(self):
        import server
        self.assertEqual(server.route_end("KLAX DCT RIC261040 DCT KMDT"), "MDT")
        self.assertIsNone(server.route_end("DUFEE ELX HAAKK DOXXY SOSIC KEEHO J584 SLT FQM3"))
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0, origin="LAX", dest="EWR", off_a=T0 + 600))
        fa.fix_diversion(f, "EWR", server.route_end("KLAX DCT RIC261040 DCT KMDT"))
        self.assertEqual(f.get("diverted_to"), "MDT")
        g, _ = fa.flights_from_bootstrap(boot(out_s=T0, origin="LAX", dest="EWR", off_a=T0 + 600))
        fa.fix_diversion(g, "EWR", server.route_end("KLAX DCT KEWR"))
        self.assertIsNone(g.get("diverted_to"))


class ManualAero(unittest.TestCase):
    def test_manual_check(self):
        import server
        L = {"id": "ma", "ident": "UAL2127", "display": "UA 2127", "dep": "LAX", "arr": "EWR", "sched_out": T0,
             "sched_in": T0 + 5 * H, "aero": {"calls": 99}}
        server.STATE["legs"] = {"ma": L}
        real_on, real_call = server.aero_on, server.aero_call
        server.aero_on = lambda: True
        server.aero_call = lambda L, now, reason, **k: {"status_text": "En Route", "tail": "N21108",
                                                        "destination": {"iata": "MDT"}}
        try:
            code, body = server.manual_aero("ma")
            self.assertEqual((code, body["tail"]), (200, "N21108"))
            seen = server.STATE["legs"]["ma"]["history"][-1]["seen"][0]
            self.assertEqual((seen["tail"], seen["note"]), ("N21108", "En Route · to MDT"))
        finally:
            server.aero_on, server.aero_call = real_on, real_call


class HistoryList(unittest.TestCase):
    def test_pick_recovery_leg(self):
        links = ["https://www.flightaware.com/live/flight/UAL2608/history/20261016/2003Z/KIAH/KABE",
                 "https://www.flightaware.com/live/flight/UAL2608/history/20261017/0123Z/KABE/KEWR",
                 "https://www.flightaware.com/live/flight/UAL2608/history/20261015/2003Z/KIAH/KEWR"]
        self.assertEqual(fa.pick_history_link(links, "KABE", "KEWR", "2026-10-16"), links[1])
        self.assertIsNone(fa.pick_history_link(links, "KABE", "KEWR", "2026-10-14"))


class SameCallsignTwice(unittest.TestCase):
    def test_pick_by_tail_then_position(self):
        import server
        two = [{"lat": 41.9, "lon": -87.9, "ground": False, "tail": "N1"},   # near ORD
               {"lat": 40.7, "lon": -74.5, "ground": False, "tail": "N2"}]   # near EWR
        real = sources.fetch_live_all
        sources.fetch_live_all = lambda cs: list(two)
        try:
            self.assertEqual(server.pick_live("UAL1", {"tail": "N2"})["tail"], "N2")
            fl = {"track": [[40.6, -75.0, 30000, 400, T0]]}
            self.assertEqual(server.pick_live("UAL1", fl)["tail"], "N2")
            fl = {"origin": {"iata": "ORD"}, "destination": {"iata": "MKE"}}
            self.assertEqual(server.pick_live("UAL1", fl)["tail"], "N1")
        finally:
            sources.fetch_live_all = real


class AutoRecovery(unittest.TestCase):
    def test_follow_and_calendar_wins(self):
        import server
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0, origin="IAH", dest="ABE", off_a=T0 + 600))
        f["times"]["on"]["actual"] = T0 + 3 * H
        fa.fix_diversion(f, "EWR")
        L = {"id": "c-2608", "ident": "UAL2608", "display": "UA 2608", "flight_no": "2608", "dep": "IAH", "arr": "EWR",
             "sched_out": T0, "sched_in": T0 + 4 * H, "flight": f, "notified": {}}
        server.STATE["legs"] = {"c-2608": L}
        server.STATE["manual"] = []
        sent = []
        real = server.notify
        server.notify = lambda a: sent.append(a) or (True, "")
        try:
            server.auto_recovery(T0 + 3 * H)
            auto = [x for x in server.STATE["manual"] if x.get("auto")]
            self.assertEqual([(x["ident"], x["dep"], x["arr"]) for x in auto], [("UA2608", "ABE", "EWR")])
            server.auto_recovery(T0 + 3 * H + 60)                     # not twice
            self.assertEqual(len(server.STATE["manual"]), 1)
            # the work calendar now lists UA2608 ABE→EWR: the automatic copy goes
            server.STATE["legs"]["c-rec"] = {"id": "c-rec", "flight_no": "2608", "dep": "ABE", "arr": "EWR",
                                             "sched_out": T0 + 5 * H, "sched_in": T0 + 6 * H}
            server.auto_recovery(T0 + 3 * H + 120)
            self.assertEqual(server.STATE["manual"], [])
        finally:
            server.notify = real


class DepartureProgramScope(unittest.TestCase):
    def test_gdp_at_departure_needs_inbound_in_scope(self):
        gdp = [{"airport": "SFO", "kind": "gdp", "label": "Ground delay program", "reason": "other", "detail": "",
                "scope": ["CYEG", "CYVR", "CYYC"], "key": "SFO|gdp|1"}]
        L = {"id": "u1", "ident": "UAL1", "display": "UA 1", "dep": "SFO", "arr": "SIN", "sched_out": T0 + H,
             "sched_in": T0 + 18 * H, "notified": {"watch": True}}
        L["flight"], _ = fa.flights_from_bootstrap(boot(out_s=T0 + H, origin="SFO", dest="SIN"))
        nas = lambda a: [x for x in a if x["key"].startswith("nas")]  # noqa: E731
        self.assertEqual(nas(alerts.evaluate(L, CFG, T0, gdp)), [])          # no inbound known: no push
        inb, _ = fa.flights_from_bootstrap(boot(fa_id="IN-1", out_s=T0 - 2 * H, origin="LAX", dest="SFO", inbound_id=None))
        L["inbound"] = inb
        self.assertEqual(nas(alerts.evaluate(L, CFG, T0 + 60, gdp)), [])     # inbound from LAX: not in scope


class Swim(unittest.TestCase):
    FLOW = """<ns5:tfmDataService xmlns:ns5="urn:us:gov:dot:faa:atm:tfm:tfmdataservice" xmlns:ns12="urn:us:gov:dot:faa:atm:tfm:flowinformation"
      xmlns:ns9="urn:us:gov:dot:faa:atm:tfm:ficommonmessages2" xmlns:ns7="urn:us:gov:dot:faa:atm:tfm:tfmdatacoreelements"><ns5:fiOutput>
      <ns12:fiMessage msgType="TMI_FLIGHT_LIST"><ns12:tmiFlightDataList><ns12:flightData><ns9:flight>
      <ns7:aircraftId>SWA239</ns7:aircraftId><ns7:gufi>KT7832361Q</ns7:gufi><ns7:igtd>2026-10-16T23:25:00Z</ns7:igtd>
      <ns7:departurePoint><ns7:airport>ATL</ns7:airport></ns7:departurePoint><ns7:arrivalPoint><ns7:airport>MCO</ns7:airport></ns7:arrivalPoint>
      </ns9:flight></ns12:flightData></ns12:tmiFlightDataList></ns12:fiMessage></ns5:fiOutput></ns5:tfmDataService>"""
    FLTD = """<ds:tfmDataService xmlns:ds="urn:us:gov:dot:faa:atm:tfm:tfmdataservice" xmlns:fdm="urn:us:gov:dot:faa:atm:tfm:flightdatacommonmessages">
      <ds:fltdOutput><fdm:fltdMessage acid="UAL2127" depArpt="KLAX" arrArpt="KMDT" msgType="flightPlanAmendmentInformation"
      sourceTimeStamp="2026-10-16T23:40:00Z"><fdm:amendmentData/></fdm:fltdMessage></ds:fltdOutput></ds:tfmDataService>"""

    def test_parse_flow_and_flight_data(self):
        import swim
        ev = swim.parse(self.FLOW)
        self.assertEqual([(e["acid"], e["dep"], e["arr"], e["igtd"]) for e in ev], [("SWA239", "ATL", "MCO", "2026-10-16T23:25:00Z")])
        ev = swim.parse(self.FLTD)
        self.assertEqual([(e["acid"], e["dep"], e["arr"], e["type"]) for e in ev],
                         [("UAL2127", "KLAX", "KMDT", "flightPlanAmendmentInformation")])

    def test_real_tfms_samples(self):
        """Payloads captured from the live SWIM feed: many flights per message, ICAO and FAA codes."""
        import swim
        here = os.path.dirname(os.path.abspath(__file__))
        def load(n):
            with open(os.path.join(here, n)) as fh:
                return {(e["acid"], e["type"]): e for e in swim.parse(fh.read())}
        arr = load("swim_arrival.xml")
        e = arr[("N279D", "arrivalInformation")]
        self.assertEqual((e["dep"], e["arr"], e["off"], e["on"]), ("KBUR", "KMRY", "2026-10-17T01:02:00Z", None))
        self.assertEqual(arr[("SWA2836", "trackInformation")]["eta"], "2026-10-17T01:59:35Z")
        b = load("swim_boundary.xml")[("UAL1218", "boundaryCrossingUpdate")]
        self.assertEqual((b["dep"], b["arr"], b["gufi"], b["igtd"]), ("KSEA", "KDEN", "KS009927Gk", "2026-10-17T01:05:00Z"))
        d = [e for e in load("swim_departure.xml").values() if e["acid"] == "JBU2823" and e["type"] == "departureInformation"][0]
        self.assertEqual((d["dep"], d["arr"], d["off"], d["diverted"]), ("KFLL", "KMSY", "2026-10-17T01:49:00Z", False))
        m = load("swim_modify.xml")[("PDT5817", "FlightModify")]
        self.assertEqual((m["src"], m["status"], m["airline"]["out"], m["airline"]["in"]),
                         ("AAL", "PLANNED", "2026-10-17T03:21:00Z", "2026-10-17T04:40:00Z"))

    def test_departure_timeout_is_ignored(self):
        import server
        import swim
        swim.STATE["status"] = "connected"
        swim.WATCH.clear(); swim.WATCH.add("UAL2608")
        swim.EVENTS.clear()
        swim.handle('''<x xmlns:fdm="f"><fdm:fltdMessage acid="UAL2608" arrArpt="KEWR" depArpt="KABE"
          fdTrigger="UPDATE_DEPARTURE_TIMEOUT" msgType="FlightTimes" sourceFacility="TFMS" sourceTimeStamp="2026-10-17T01:54:20Z">
          <fdm:t><nxcm:eta xmlns:nxcm="n" etaType="ESTIMATED" timeValue="2026-10-17T02:01:00Z"/></fdm:t></fdm:fltdMessage></x>''')
        self.assertEqual(len(swim.events("UAL2608")), 1)
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0, origin="ABE", dest="EWR"))
        L = {"id": "s2", "ident": "UAL2608", "dep": "ABE", "arr": "EWR", "sched_out": T0}
        self.assertEqual(server.swim_check(L, f, "EWR"), {})

    def test_continuation_keeps_original_igtd(self):
        """UA2127 LAX→EWR diverted to MDT: the MDT→EWR plan still carries LAX's departure (8 h earlier)."""
        import server
        import swim
        swim.STATE["status"] = "connected"
        swim.WATCH.clear(); swim.WATCH.add("UAL2127")
        swim.EVENTS.clear()
        igtd = datetime.fromtimestamp(T0 - 8 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tomorrow = datetime.fromtimestamp(T0 + 16 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        swim.handle(f'''<x xmlns:fdm="f" xmlns:n="n"><fdm:fltdMessage acid="UAL2127" arrArpt="KEWR" depArpt="KMDT"
          msgType="departureInformation" sourceFacility="KZNY" sourceTimeStamp="2026-10-17T02:30:00Z"><n:igtd>{igtd}</n:igtd>
          <n:timeOfDeparture estimated="false">2026-10-17T02:29:00Z</n:timeOfDeparture></fdm:fltdMessage>
          <fdm:fltdMessage acid="UAL2127" arrArpt="KEWR" depArpt="KMDT" msgType="FlightSectors" sourceFacility="TFMS">
          <n:igtd>{tomorrow}</n:igtd></fdm:fltdMessage></x>''')
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0, origin="MDT", dest="EWR"))
        L = {"id": "s3", "ident": "UAL2127", "dep": "MDT", "arr": "EWR", "sched_out": T0}
        self.assertEqual(server.swim_check(L, f, "EWR").get("off"), "2026-10-17T02:29:00Z")

    def test_tfdm_feed(self):
        import swim
        feeds = swim._feeds({"swim_username": "u", "swim_password": "p", "swim_queue": "q1",
                             "swim_tfdm_queue": "q2", "swim_tfdm_vpn": "TFDMVPN"})
        self.assertEqual([(f["name"], f["queue"], f["vpn"], f["user"]) for f in feeds],
                         [("TFMS", "q1", "TFMS", "u"), ("TFDM", "q2", "TFDMVPN", "u")])
        self.assertEqual(len(swim._feeds({"swim_username": "u", "swim_password": "p", "swim_queue": "q1"})), 1)
        swim.WATCH.clear(); swim.WATCH.add("UAL2127")
        n = swim.TFDM_STATE["matched"]
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tfdm_2127.xml")) as fh:
            swim.handle_tfdm(fh.read())
        self.assertEqual(swim.TFDM_STATE["matched"], n + 1)
        self.assertIn("NasMessage", swim.TFDM_TYPES)
        ev = [e for e in swim.events("UAL2127") if e.get("src") == "TFDM"][-1]
        self.assertEqual((ev["dep"], ev["arr"], ev["tfdm"]["arr"]["runwayArrivalTime"]["estimated"]),
                         ("KMDT", "KEWR", "2026-10-17T02:49:57Z"))

    def test_real_2127_continuation_departure(self):
        """Captured live: UA2127 MDT→EWR (after the LAX→EWR diversion) wheels-up 02:19Z, original igtd 17:15Z."""
        import server
        import swim
        swim.STATE["status"] = "connected"
        swim.WATCH.clear(); swim.WATCH.add("UAL2127")
        swim.EVENTS.clear()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "swim_2127_departure.xml")) as fh:
            swim.handle(fh.read())
        so = datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc).timestamp()
        f, _ = fa.flights_from_bootstrap(boot(out_s=so, origin="MDT", dest="EWR"))
        L = {"id": "s4", "ident": "UAL2127", "dep": "MDT", "arr": "EWR", "sched_out": so}
        sw = server.swim_check(L, f, "EWR")
        self.assertEqual((sw.get("off"), sw.get("eta"), sw.get("diverted_to")),
                         ("2026-10-17T02:19:00Z", "2026-10-17T02:57:30Z", None))

    def test_real_2127_overlay(self):
        """The live UA2127 MDT→EWR sequence: FAA takeoff, United's push time, the tower's landing
        estimate and ATC's shortcut all land on the flight; the route shows ATC's amended one."""
        import server
        import swim
        here = os.path.dirname(os.path.abspath(__file__))
        swim.STATE["status"] = "connected"
        swim.WATCH.clear(); swim.WATCH.add("UAL2127")
        swim.EVENTS.clear()
        for name in ("swim_2127_departure.xml", "swim_2127_united.xml", "tfdm_2127.xml", "swim_2127_reroute.xml"):
            with open(os.path.join(here, name)) as fh:
                (swim.handle_tfdm if name.startswith("tfdm") else swim.handle)(fh.read())
        so = datetime(2026, 10, 17, 1, 27, tzinfo=timezone.utc).timestamp()
        now = datetime(2026, 10, 17, 2, 31, tzinfo=timezone.utc).timestamp()
        f, _ = fa.flights_from_bootstrap(boot(out_s=so, origin="MDT", dest="EWR"))
        f["route"] = "KMDT GAAPP SEG FQM FQM3 KEWR"
        L = {"id": "s5", "ident": "UAL2127", "dep": "MDT", "arr": "EWR", "sched_out": so}
        with mock.patch.object(server.time, "time", return_value=now):
            server.swim_check(L, f, "EWR")
            server.swim_overlay(L, f, now)
        z = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        t = f["times"]
        self.assertEqual(t["off"]["actual"], z("2026-10-17T02:19:00Z"))
        self.assertEqual(t["out"]["actual"], z("2026-10-17T01:53:00Z"))      # United's gateDeparture
        self.assertEqual(t["on"]["estimated"], z("2026-10-17T02:49:57Z"))    # Newark tower's estimate
        self.assertGreater(t["in"]["estimated"], t["on"]["estimated"])
        self.assertEqual(f["route"], "KMDT LVZ245023 RACKI FQM3 KEWR")
        self.assertEqual(f["route_source"], "FAA")
        self.assertEqual(f["swim"]["eta_src"], "tower")
        self.assertEqual(f["swim"]["airline"]["src"], "UAL")
        self.assertIn("UAL2127", swim.PENDING)
        # saved on the leg: a later check with no new messages keeps it all
        swim.EVENTS.clear()
        g, _ = fa.flights_from_bootstrap(boot(out_s=so, origin="MDT", dest="EWR"))
        server.swim_check(L, g, "EWR")
        server.swim_overlay(L, g, now)
        self.assertEqual(g["times"]["off"]["actual"], z("2026-10-17T02:19:00Z"))
        self.assertEqual(g["route"], "KMDT LVZ245023 RACKI FQM3 KEWR")

    def test_tfdm_departure_side(self):
        import swim
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "tfdm_las.xml")) as fh:
            ev = [e for e in swim.parse_tfdm(fh.read()) if e["dep"] == "KLAS"][0]
        d = ev["tfdm"]["dep"]
        self.assertEqual((d["runwayAssigned"], d["departureFix"], d["departureDelay"]["predictedDelay"],
                          d["runwayDepartureTime"]["estimated"], d["departureTaxiTime"]["totalEstimatedTaxiOutTime"]),
                         ("01R", "SSKEE", 8.0, "2026-10-17T13:32:00Z", 23.0))

    def test_surface_positions(self):
        import swim
        xml = """<ns2:asdexMsg xmlns:ns2="urn:x"><airport>KEWR</airport>
          <positionReport full="true"><track>101</track><position><latitude>40.69</latitude><longitude>-74.17</longitude></position>
          <movement><speed>12</speed><heading>40</heading></movement><flightId><aircraftId>UAL2127</aircraftId></flightId>
          <flightInfo><tgtType>aircraft</tgtType><acType>B752</acType></flightInfo></positionReport>
          <positionReport full="false"><track>102</track><position><latitude>40.68</latitude><longitude>-74.18</longitude></position></positionReport>
          </ns2:asdexMsg>"""
        swim.WATCH_AIRPORTS.clear(); swim.WATCH_AIRPORTS.add("KEWR")
        swim.SURFACE.clear()
        swim.handle_stdds(xml)
        swim.handle_stdds(xml.replace("<latitude>40.69</latitude>", "<latitude>40.70</latitude>").replace(' full="true"', ' full="false"'))
        got = {t["track"]: t for t in swim.surface("KEWR")}
        self.assertEqual((got["101"]["lat"], got["101"]["cs"], got["101"]["type"]), (40.70, "UAL2127", "B752"))
        swim.WATCH_AIRPORTS.clear()
        swim.handle_stdds(xml.replace("40.69", "41.0"))
        self.assertEqual(swim.surface("KEWR")[0]["lat"] in (40.70, 40.68), True)  # not watched: ignored

    def test_route_trajectory_from_flight_sectors(self):
        """FlightSectors carries the FAA's trajectory (lat/lon every few minutes) and the fixes on it."""
        import server
        import swim
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "swim_sectors.xml")) as fh:
            ev = [e for e in swim.parse(fh.read()) if e["acid"] == "UAL2608" and e["type"] == "FlightSectors"][0]
        self.assertEqual(len(ev["traj"]), 92)
        self.assertEqual(ev["traj_fixes"][1], [30.09, -95.26, "KNTKY"])
        sw = {}
        server._swim_take(sw, dict(ev, got=1.0))
        f, _ = fa.flights_from_bootstrap(boot(origin="IAH", dest="EWR"))
        L = {"id": "t", "ident": "UAL2608", "dep": "IAH", "arr": "EWR", "swim": dict(sw, seen=1)}
        server.swim_overlay(L, f, T0)
        self.assertEqual(f["swim_route"]["pts"][-1], [40.69, -74.17])

    def test_edct_from_slot_assignment(self):
        """TMI_UPDATE slot assignment: CTF173 HND→PHX, EDCT 06:13Z, arrival slot 07:15Z (captured live)."""
        import server
        import swim
        swim.STATE["status"] = "connected"
        swim.WATCH.clear(); swim.WATCH.add("CTF173")
        swim.EVENTS.clear()
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "swim_edct.xml")) as fh:
            swim.handle(fh.read())
        ev = [e for e in swim.events("CTF173") if e["type"] == "EDCT"][0]
        self.assertEqual((ev["dep"], ev["arr"], ev["edct"], ev["cta"], ev["element"]),
                         ("HND", "PHX", "2026-10-17T06:13:00Z", "2026-10-17T07:15:00Z", "PHX"))
        so = datetime(2026, 10, 17, 5, 45, tzinfo=timezone.utc).timestamp()
        f, _ = fa.flights_from_bootstrap(boot(out_s=so, origin="HND", dest="PHX"))
        L = {"id": "e1", "ident": "CTF173", "display": "CTF 173", "dep": "HND", "arr": "PHX", "sched_out": so}
        pushed = []
        with mock.patch.object(server, "push_event", lambda *a, **k: pushed.append(a)):
            server.swim_check(L, f, "PHX")
            server.swim_overlay(L, f, so)
        self.assertEqual(f["times"]["off"]["estimated"], datetime(2026, 10, 17, 6, 13, tzinfo=timezone.utc).timestamp())
        self.assertEqual(f["time_src"]["off"]["estimated"], "FAA EDCT")
        self.assertEqual(pushed[0][0], "edct")
        with mock.patch.object(server, "push_event", lambda *a, **k: pushed.append(a)):
            server.swim_check(L, f, "PHX")          # nothing new: no second push
        self.assertEqual(len(pushed), 1)

    def test_amendment_marks_diversion(self):
        import server
        import swim
        swim.STATE["status"] = "connected"
        swim.WATCH.clear(); swim.WATCH.add("UAL2127")
        swim.EVENTS.clear()
        swim.handle(self.FLTD)
        f, _ = fa.flights_from_bootstrap(boot(out_s=T0, origin="LAX", dest="EWR", off_a=T0 + 600))
        L = {"id": "s1", "ident": "UAL2127", "dep": "LAX", "arr": "EWR", "sched_out": T0}
        sw = server.swim_check(L, f, "EWR")
        self.assertEqual((sw.get("diverted_to"), sw.get("new")), ("MDT", True))
        self.assertFalse(server.swim_check(L, f, "EWR").get("new"))   # logged once
        swim.STATE["status"] = "off"
