"""The display snapshot: what reaches the monitor, and what never does."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops import display
from terracompute_ops.http_client import HttpRequest, HttpResponse
from terracompute_ops.state import CURRENT_SCHEMA_VERSION

NOW = datetime(2026, 9, 30, 6, 30, tzinfo=timezone.utc)
CARDS = [
    {"index": 0, "pci_bdf": "0000:01:00.0", "psu": "C"}, {"index": 1, "pci_bdf": "0000:24:00.0", "psu": "D"},
    {"index": 2, "pci_bdf": "0000:41:00.0", "psu": "D"}, {"index": 3, "pci_bdf": "0000:61:00.0", "psu": "C"},
    {"index": 4, "pci_bdf": "0000:81:00.0", "psu": "A"}, {"index": 5, "pci_bdf": "0000:a1:00.0", "psu": "A"},
    {"index": 6, "pci_bdf": "0000:c1:00.0", "psu": "B"}, {"index": 7, "pci_bdf": "0000:e1:00.0", "psu": "B"},
]
SECRET_LOOKING = ("GPU-285fcf47-c4b2", "1322823110529", "C.50352859", "renter-container", "synthetic-machine-read-key")


def config(root: Path, **overrides) -> display.Config:
    values = dict(state_dir=root / "state", actions_db=root / "actions.sqlite3", agent_status=root / "agent.json",
                  work_dir=root / "work", display_tz="America/Los_Angeles", cards=display._cards(CARDS),
                  psu_capacity_w={}, push_target="terracompute-display@10.50.0.2",
                  identity_file=root / "id", known_hosts_file=root / "known_hosts")
    values.update(overrides)
    return display.Config(**values)


def ssh_doc(*, missing=(), vfio=(), boot="boot-a", events=()):
    devices, gpus = [], []
    for card in CARDS:
        if card["index"] in missing:
            continue
        driver = "vfio-pci" if card["index"] in vfio else "nvidia"
        devices.append({"pci_bdf": card["pci_bdf"], "driver": driver})
        if driver == "nvidia":
            gpus.append({"pci_bdf": card["pci_bdf"], "uuid": f"GPU-285fcf47-c4b2-{card['index']}",
                         "serial": "1322823110529", "temperature_c": 60 + card["index"],
                         "pstate": "P2" if card["index"] % 2 else "P8"})
    return {"boot_id": boot, "events": list(events), "snapshot": {
        "gpu": {"expected_count": 8, "pci_devices": devices, "gpus": gpus},
        "docker": {"containers": [{"name": "C.50352859", "status": "Up"}, {"name": "renter-container"}]}}}


def prometheus_doc(occupancy: dict[int, int]):
    vast = [{"labels": {"__name__": "vastai_machine_gpu_occupancy", "gpu": str(i)}, "value": v} for i, v in occupancy.items()]
    vast.append({"labels": {"__name__": "vastai_machine_gpu_rented_on_demand"}, "value": sum(1 for v in occupancy.values() if v == 2)})
    vast.append({"labels": {"__name__": "vastai_machine_gpu_rented_bid_demand"}, "value": sum(1 for v in occupancy.values() if v == 1)})
    return {"snapshot": {"metrics": {"vast": vast, "dcgm": [], "dcgm_monitored": False}}}


BMC = {"snapshot": {"resources": [{"thermal": [
    {"kind": "fan", "name": "FAN6_1", "reading": 3500.0, "reading_rpm": None, "state": "Enabled"},
    {"kind": "fan", "name": "FAN2_1", "reading": 3600.0, "state": "Enabled"},
    {"kind": "fan", "name": "FAN5_1", "state": "Absent"},
    {"kind": "temperature", "name": "TEMP_INLET", "reading_celsius": 24.0, "state": "Enabled"}]}]}}
VAST = {"snapshot": {"machine": {"listed": True, "total_gpus": 8}}}


def inputs(**overrides) -> display.Inputs:
    values = dict(now=NOW, ssh=ssh_doc(), ssh_at=NOW - timedelta(minutes=2),
                  prometheus=prometheus_doc({0: 2, 1: 2, 2: 0, 3: 2, 4: 1, 5: 2, 6: 0, 7: 2}), bmc=BMC, vast=VAST)
    values.update(overrides)
    return display.Inputs(**values)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = config(self.tmp)

    def test_cards_sit_in_pairs_by_psu_with_what_the_host_reports(self):
        snap = display.build(self.config, inputs(ssh=ssh_doc(missing={3}, vfio={6})), {})
        by_index = {g["index"]: g for g in snap["gpus"]}
        self.assertEqual([g["psu"] for g in sorted(snap["gpus"], key=lambda g: g["position"])],
                         ["A", "A", "B", "B", "C", "C", "D", "D"])
        self.assertEqual(by_index[3]["state"], "missing")
        self.assertEqual(by_index[6]["state"], "vm")
        self.assertEqual(by_index[2]["state"], "idle")
        self.assertEqual(by_index[4]["state"], "rented")  # interruptible counts as rented
        self.assertEqual(by_index[1]["temp_c"], 61)
        self.assertIs(by_index[1]["active"], True)
        self.assertIs(by_index[0]["active"], False)
        self.assertIsNone(by_index[0]["util_pct"])
        self.assertIsNone(by_index[6]["temp_c"])
        self.assertEqual(snap["power"], {"wall_w": None, "inlet_c": 24.0, "fan_rpm": 3550})
        self.assertIs(snap["vast"]["listed"], True)

    def test_a_card_the_driver_lost_counts_as_missing(self):
        lost = ssh_doc()
        gpus = lost["snapshot"]["gpu"]["gpus"]
        lost["snapshot"]["gpu"]["gpus"] = [g for g in gpus if g["pci_bdf"] != "0000:24:00.0"]
        by_index = {g["index"]: g for g in display.build(self.config, inputs(ssh=lost), {})["gpus"]}
        self.assertEqual(by_index[1]["state"], "missing")
        self.assertEqual(by_index[0]["state"], "rented")
        # A probe where nvidia-smi saw nothing at all changes nothing.
        silent = ssh_doc()
        silent["snapshot"]["gpu"]["gpus"] = []
        states = {g["state"] for g in display.build(self.config, inputs(ssh=silent), {})["gpus"]}
        self.assertNotIn("missing", states)

    def test_nothing_identifying_leaves(self):
        text = json.dumps(display.build(self.config, inputs(), {}))
        for needle in SECRET_LOOKING:
            self.assertNotIn(needle, text)

    def test_changes_become_events_once(self):
        memory: dict = {}
        display.build(self.config, inputs(), memory)
        later = NOW + timedelta(seconds=30)
        occupancy = {0: 0, 1: 2, 2: 2, 3: 2, 4: 1, 5: 2, 6: 0, 7: 2}
        snap = display.build(self.config, inputs(now=later, ssh=ssh_doc(missing={5}), ssh_at=later,
                                                 prometheus=prometheus_doc(occupancy)), memory)
        kinds = {(e["kind"], e.get("gpu")) for e in snap["events"]}
        self.assertLessEqual({("rental_ended", 0), ("rental_started", 2), ("gpu_missing", 5)}, kinds)
        again = display.build(self.config, inputs(now=later + timedelta(seconds=30), ssh=ssh_doc(missing={5}),
                                                  ssh_at=later, prometheus=prometheus_doc(occupancy)), memory)
        self.assertEqual(len([e for e in again["events"] if e["kind"] == "rental_started"]), 1)

    def test_reboot_and_xid(self):
        memory: dict = {}
        first = display.build(self.config, inputs(boots=[("boot-z", NOW - timedelta(days=9)),
                                                         ("boot-a", NOW - timedelta(days=3))]), memory)
        self.assertEqual(first["machine"]["boot_number"], 2)
        self.assertEqual(first["machine"]["booted_at"], display._iso(NOW - timedelta(days=3)))
        xid = {"fault_family": "xid", "code": "79", "count": 1, "evidence": {"xid": 79, "pci_bdf": "0000:61:00.0"}}
        snap = display.build(self.config, inputs(ssh=ssh_doc(boot="boot-b", events=[xid])), memory)
        self.assertEqual(snap["machine"]["boot_number"], 3)
        events = {(e["kind"], e["text"]) for e in snap["events"]}
        self.assertIn(("reboot", "host booted (#3)"), events)
        self.assertIn(("xid", "Xid 79 on GPU 3"), events)
        again = display.build(self.config, inputs(ssh=ssh_doc(boot="boot-b", events=[xid])), memory)
        self.assertEqual(len([e for e in again["events"] if e["kind"] == "xid"]), 1)

    def test_backup_runs_become_events(self):
        memory: dict = {}
        ran = NOW - timedelta(hours=1)
        display.build(self.config, inputs(backup={"at": ran, "ok": True}), memory)
        snap = display.build(self.config, inputs(backup={"at": NOW - timedelta(minutes=1), "ok": False}), memory)
        self.assertEqual(snap["backup"]["last_ok"], False)
        self.assertIn("backup_failed", {e["kind"] for e in snap["events"]})

    def test_incidents_point_at_their_card(self):
        incident = {"key": "k", "title": "gpu_missing_from_pci", "severity": "error",
                    "opened_at": NOW - timedelta(minutes=12), "recovered_at": None,
                    "bdf": "0000:61:00.0", "uuid": None}
        snap = display.build(self.config, inputs(incidents=[incident], last_incident_at=NOW - timedelta(minutes=12)), {})
        self.assertEqual(snap["incidents"]["open"], [{"title": "gpu_missing_from_pci",
                                                       "opened_at": display._iso(NOW - timedelta(minutes=12)), "gpu": 3}])
        self.assertEqual(snap["incidents"]["days_without"], 0)

    def test_agent_status_and_money_pass_through(self):
        agent = {"state": "awaiting_approval", "round": None, "max_rounds": None, "activity": "waiting for approval",
                 "astra": "approve", "approval_expires_at": display._iso(NOW + timedelta(minutes=23)),
                 "events": [{"at": display._iso(NOW - timedelta(minutes=7)), "kind": "plan_sent",
                             "text": "plan sent for approval · 30 min"}]}
        money = {"reliability": 0.994, "on_demand_price": 0.4, "bid_price": 0.25, "today": 31.4, "month": 1184.6}
        snap = display.build(self.config, inputs(agent=agent, money=money), {})
        self.assertEqual(snap["agent"]["state"], "awaiting_approval")
        self.assertEqual(snap["agent"]["astra"], "approve")
        self.assertEqual(snap["vast"]["earnings"], {"per_hour_usd": 2.25, "today_usd": 31.4, "month_usd": 1184.6})
        self.assertIn("plan_sent", {e["kind"] for e in snap["events"]})

    def test_the_self_check_refuses_a_bad_snapshot(self):
        snap = display.build(self.config, inputs(), {})
        snap["gpus"][0]["state"] = "on fire"
        with self.assertRaisesRegex(display.DisplayError, "gpu"):
            display.check(snap)


class IncidentNameTests(unittest.TestCase):
    def test_names_read_well_on_a_small_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "incidents" / "b1"
            bundle.mkdir(parents=True)
            (bundle / "incident.json").write_text(json.dumps({"classification": {"label": "gpu-fallen-off-bus"}}))
            self.assertEqual(display._incident_name(root, "b1", "79", "xid"), "Xid 79 gpu-fallen-off-bus")
            self.assertEqual(display._incident_name(root, None, "154", "xid"), "Xid 154")
            self.assertEqual(display._incident_name(root, None, "fatal", "aer"), "AER fatal")
            self.assertEqual(display._incident_name(root, "b1", "vast_direct_total_differs_physical_total", "capacity"),
                             "gpu-fallen-off-bus")
            self.assertEqual(display._incident_name(root, None, None, "bmc"), "bmc")


class ConfigTests(unittest.TestCase):
    EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "display.json"

    # The package build copies only src, tests and target, so the example is not there.
    @unittest.skipUnless(EXAMPLE.is_file(), "examples/ is not part of the package build")
    def test_the_example_config_loads_with_cards_paired_by_psu(self):
        example = display.load_config(self.EXAMPLE)
        self.assertEqual([(c.position, c.psu) for c in example.cards],
                         [(0, "A"), (1, "A"), (2, "B"), (3, "B"), (4, "C"), (5, "C"), (6, "D"), (7, "D")])
        self.assertEqual(example.push_target, "terracompute-display@10.50.0.2")

    def test_cards_are_validated(self):
        for bad, message in (([{"index": 0, "pci_bdf": "01:00.0", "psu": "A"}], "pci_bdf"),
                             ([{"index": 0, "pci_bdf": "0000:01:00.0", "psu": "a"}], "psu"),
                             (CARDS[:1] * 2, "unique"), ([], "1 to 8")):
            with self.assertRaisesRegex(display.DisplayError, message):
                display._cards(bad)


def actions_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE tc_action_cycles (cycle_id TEXT, proposal_id TEXT, stage TEXT, command TEXT, result TEXT,
            created_utc TEXT, finished_utc TEXT);
        CREATE TABLE tc_action_notes (name TEXT, value TEXT, set_utc TEXT);
        CREATE TABLE tc_action_observe_loops (state TEXT, rounds INTEGER, started_utc TEXT, updated_utc TEXT);
        CREATE TABLE tc_action_schedule (name TEXT, due_utc TEXT, count INTEGER);
        CREATE TABLE tc_action_conversations (root TEXT, question TEXT, round INTEGER, created_utc TEXT, updated_utc TEXT);
        CREATE TABLE tc_action_outbox (id INTEGER PRIMARY KEY, sent_utc TEXT, text TEXT);
    """)
    return db


class AgentStatusTests(unittest.TestCase):
    def test_a_waiting_plan_with_astra_verdict_and_no_private_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = actions_db(Path(tmp) / "a.sqlite3")
            self.addCleanup(db.close)
            created = display._iso(NOW - timedelta(minutes=7))
            db.execute("INSERT INTO tc_action_cycles VALUES ('c','p','awaiting_answer','rm -rf /secret-plan',NULL,?,NULL)",
                       (created,))
            db.execute("INSERT INTO tc_action_outbox (sent_utc, text) VALUES (?, ?)",
                       (display._iso(NOW - timedelta(minutes=8)), "Astra approves: the plan reads nvidia bus 61 details"))
            db.execute("INSERT INTO tc_action_conversations VALUES ('r','why is gpu 3 private question',4,?,?)",
                       (created, created))
            status = display.agent_status(db, NOW)
            self.assertEqual(status["state"], "awaiting_approval")
            self.assertEqual(status["approval_expires_at"], display._iso(NOW + timedelta(minutes=23)))
            self.assertEqual(status["astra"], "approve")
            text = json.dumps(status)
            for private in ("rm -rf", "secret-plan", "private question", "bus 61"):
                self.assertNotIn(private, text)

    def test_investigating_shows_rounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = actions_db(Path(tmp) / "a.sqlite3")
            self.addCleanup(db.close)
            now = display._iso(NOW)
            db.execute("INSERT INTO tc_action_observe_loops VALUES ('open', 3, ?, ?)", (now, now))
            status = display.agent_status(db, NOW)
            self.assertEqual((status["state"], status["round"], status["max_rounds"], status["activity"]),
                             ("investigating", 3, 6, "reading the machine"))
            db.execute("INSERT INTO tc_action_schedule VALUES ('conversation:r', ?, 1)", (now,))
            db.execute("INSERT INTO tc_action_conversations VALUES ('r','q',7,?,?)", (now, now))
            status = display.agent_status(db, NOW)
            self.assertEqual((status["round"], status["max_rounds"]), (7, 10))

    def test_an_expired_plan_is_not_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = actions_db(Path(tmp) / "a.sqlite3")
            self.addCleanup(db.close)
            old = display._iso(NOW - timedelta(minutes=45))
            db.execute("INSERT INTO tc_action_cycles VALUES ('c','p','awaiting_answer','x',NULL,?,NULL)", (old,))
            self.assertEqual(display.agent_status(db, NOW)["state"], "idle")

    def test_a_missing_database_table_means_idle(self):
        status = display.agent_status(sqlite3.connect(":memory:"), NOW)
        self.assertEqual(status["state"], "idle")


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def request(self, request: HttpRequest, **_kwargs) -> HttpResponse:
        self.urls.append(request.url)
        for prefix, (status, payload) in self.responses.items():
            if request.url.startswith(prefix):
                return HttpResponse(status, {"Content-Type": "application/json"}, json.dumps(payload).encode())
        return HttpResponse(404, {}, b"{}")


class MoneyTests(unittest.TestCase):
    def test_reliability_prices_and_earnings(self):
        transport = FakeTransport({
            "https://console.vast.ai/api/v0/machines/": (200, {"machines": [
                {"id": 17049, "reliability2": 0.994, "listed_gpu_cost": 0.4, "min_bid_price": 0.25, "hostname": "x"}]}),
            "https://console.vast.ai/api/v0/users/me/machine-earnings/": (200, {
                "summary": {"total_gpu": 30.0, "total_stor": 1.2, "total_bwu": 0.1, "total_bwd": 0.1},
                "username": "someone", "email": "someone@example.com"}),
        })
        money = display.read_money("synthetic-machine-read-key", NOW, timezone.utc, transport)
        self.assertEqual((money["reliability"], money["on_demand_price"], money["today"]), (0.994, 0.4, 31.4))
        self.assertNotIn("someone", json.dumps(money))
        self.assertTrue(any("machid=17049" in url for url in transport.urls))

    def test_refusals_leave_money_unknown(self):
        money = display.read_money("synthetic-machine-read-key", NOW, timezone.utc,
                                   FakeTransport({"https://console.vast.ai/": (403, {"error": "forbidden"})}))
        self.assertIsNone(money["today"])
        self.assertIsNone(money.get("reliability"))


class PushTests(unittest.TestCase):
    def test_push_uses_the_display_key_and_reports_refusals(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, b"stored snapshot from ...", b"")

        cfg = config(Path("/nonexistent"))
        self.assertEqual(display.push(cfg, b"{}", runner), "stored snapshot from ...")
        command, kwargs = calls[0]
        self.assertEqual(command[-1], "terracompute-display@10.50.0.2")
        self.assertIn("IdentityFile=/nonexistent/id", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(kwargs["input"], b"{}")

        def refusing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, b"", b"refused: schema must be terracompute.display/1\n")

        with self.assertRaisesRegex(display.DisplayError, "refused: schema"):
            display.push(cfg, b"{}", refusing)


class PublishTests(unittest.TestCase):
    def test_a_dry_run_reads_state_read_only_and_writes_the_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            (state / "incidents").mkdir(parents=True)
            db = sqlite3.connect(state / "state.sqlite3")
            db.executescript("""
                CREATE TABLE terracompute_observation_artifacts (artifact_id INTEGER PRIMARY KEY, machine_id TEXT,
                    source TEXT, observed_utc TEXT, sha256 TEXT, document_json BLOB, capture_class TEXT);
                CREATE TABLE incidents (dedup_key TEXT, bundle_name TEXT, fault_family TEXT, severity TEXT,
                    status TEXT, first_occurrence_utc TEXT, recovered_utc TEXT);
                CREATE TABLE observations (id INTEGER PRIMARY KEY, source TEXT, source_utc TEXT, boot_id TEXT,
                    incident_key TEXT, evidence_json TEXT);
            """)
            db.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION}")
            for source, doc in (("ssh", ssh_doc()), ("prometheus", prometheus_doc({i: 2 for i in range(8)})),
                                ("bmc", BMC), ("vast", VAST)):
                db.execute("INSERT INTO terracompute_observation_artifacts (machine_id, source, observed_utc, "
                           "document_json) VALUES ('17049', ?, ?, ?)",
                           (source, display._iso(NOW - timedelta(minutes=1)), json.dumps(doc)))
            db.execute("INSERT INTO observations (source, source_utc, boot_id) VALUES ('ssh', ?, 'boot-a')",
                       (display._iso(NOW - timedelta(days=2)),))
            db.commit()
            db.close()
            before = (state / "state.sqlite3").read_bytes()

            def systemctl(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, "Result=success\nExecMainStatus=0\n"
                                                   "ExecMainExitTimestamp=@1790740000\n", "")

            cfg = config(root)
            snap = display.publish(cfg, NOW, dry_run=True, runner=systemctl)
            self.assertEqual(sum(1 for g in snap["gpus"] if g["state"] == "rented"), 8)
            self.assertEqual(snap["machine"]["boot_number"], 1)
            self.assertTrue(snap["backup"]["last_ok"])
            self.assertEqual(json.loads((root / "work" / "snapshot.json").read_text()), snap)
            self.assertEqual((state / "state.sqlite3").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
