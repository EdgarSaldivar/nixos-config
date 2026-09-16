from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from terracompute_ops.maintenance import RECOVERY_STABLE_SECONDS
from terracompute_ops.watchdog_runtime import (
    MAX_HANDOFF_RESPONSE_BYTES,
    MAX_HEALTHCHECKS_RESPONSE_BYTES,
    MAX_HEARTBEAT_BYTES,
    MAX_TELEGRAM_RESPONSE_BYTES,
    AlertSendReceipt,
    HandoffReceipt,
    HeartbeatExporter,
    HeartbeatReceiver,
    HealthchecksHTTPResponse,
    HealthchecksPingURL,
    HealthchecksPinger,
    HealthchecksWatchdogRuntime,
    StdlibHealthchecksTransport,
    WatchdogNotifier,
    WatchdogOutbox,
    WatchdogRuntime,
    WatchdogRuntimeError,
    WatchdogSenderIdentity,
    read_healthchecks_ping_url,
)


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
DEADLINE = 110.0
PING_UUID = "12345678-1234-4234-8234-123456789abc"
PING_URL = f"https://hc-ping.com/{PING_UUID}"


def utc_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def heartbeat(
    sequence: int,
    when: datetime,
    *,
    boot_id: str = "controller-a",
    progress: datetime | None = None,
) -> dict[str, object]:
    progress = progress or when
    return {
        "schema_version": 1,
        "machine_id": "17049",
        "controller_boot_id": boot_id,
        "sequence": sequence,
        "sent_at": utc_text(when),
        "collection_progress_at": utc_text(progress),
        "notification_progress_at": utc_text(progress),
    }


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


class ReceiveFake:
    def __init__(self, values: list[bytes | None]):
        self.values = values
        self.calls: list[tuple[float, int]] = []

    def receive(self, *, deadline: float, max_bytes: int) -> bytes | None:
        self.calls.append((deadline, max_bytes))
        return self.values.pop(0)


class ExportFake:
    def __init__(self):
        self.calls: list[tuple[bytes, float, int]] = []

    def export_atomic(
        self, payload: bytes, *, deadline: float, max_output_bytes: int
    ) -> HandoffReceipt:
        self.calls.append((payload, deadline, max_output_bytes))
        return HandoffReceipt(True, b"accepted")


class SenderFake:
    def __init__(self, outcomes: list[object] | None = None):
        self.identity = WatchdogSenderIdentity("minas-watchdog-bot")
        self.outcomes = list(outcomes or [])
        self.alerts = []
        self.calls: list[tuple[float, int]] = []

    def send_alert(self, alert, *, deadline: float, max_response_bytes: int):
        self.alerts.append(alert)
        self.calls.append((deadline, max_response_bytes))
        outcome = self.outcomes.pop(0) if self.outcomes else AlertSendReceipt(len(self.alerts))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class HealthchecksTransportFake:
    def __init__(self, outcomes: list[object] | None = None):
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[str, float, int]] = []

    def request(self, path: str, *, deadline: float, max_response_bytes: int):
        self.calls.append((path, deadline, max_response_bytes))
        outcome = self.outcomes.pop(0) if self.outcomes else HealthchecksHTTPResponse(200, b"OK")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class WatchdogRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".watchdog-runtime-test-",
            dir=Path.cwd(),
            ignore_cleanup_errors=True,
        )
        self.root = Path(self.temp.name).absolute()
        self.now = NOW
        self.clock = lambda: self.now
        self.monotonic = lambda: 100.0
        self.outbox = WatchdogOutbox(self.root / "outbox.sqlite3")

    def tearDown(self) -> None:
        self.outbox.close()
        for directory, dirnames, filenames in os.walk(self.root, topdown=False):
            for filename in filenames:
                (Path(directory) / filename).chmod(0o600)
            for dirname in dirnames:
                path = Path(directory) / dirname
                if not path.is_symlink():
                    path.chmod(0o700)
            Path(directory).chmod(0o700)
        self.temp.cleanup()

    def runtime(self, source: ReceiveFake, sender: SenderFake) -> WatchdogRuntime:
        receiver = HeartbeatReceiver(transport=source, monotonic=self.monotonic)
        notifier = WatchdogNotifier(self.outbox, sender, monotonic=self.monotonic)
        return WatchdogRuntime(
            receiver,
            self.root / "evaluator.json",
            self.outbox,
            notifier,
            clock=self.clock,
            monotonic=self.monotonic,
        )

    def tick(self, runtime: WatchdogRuntime, when: datetime):
        self.now = when
        return runtime.tick(deadline=DEADLINE)

    def test_exact_atomic_file_export_and_injected_transport_are_bounded(self) -> None:
        handoff = self.root / "configured-name.json"
        publisher = HeartbeatExporter(handoff_path=handoff, monotonic=self.monotonic)
        document = heartbeat(1, NOW)
        receipt = publisher.export(document, deadline=DEADLINE)
        self.assertTrue(receipt.accepted)
        self.assertEqual(json.loads(handoff.read_bytes()), document)
        self.assertEqual(handoff.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "controller-heartbeat.json").exists())

        transport = ExportFake()
        remote = HeartbeatExporter(transport=transport, monotonic=self.monotonic)
        remote.export(document, deadline=DEADLINE)
        self.assertEqual(transport.calls[0][1:], (DEADLINE, MAX_HANDOFF_RESPONSE_BYTES))
        self.assertLessEqual(len(transport.calls[0][0]), MAX_HEARTBEAT_BYTES)

    def test_three_missed_cadences_and_stale_work_are_distinct_and_immediate(self) -> None:
        sender = SenderFake()
        handoff = self.root / "received-heartbeat.json"
        HeartbeatExporter(handoff_path=handoff, monotonic=self.monotonic).export(
            heartbeat(1, NOW), deadline=DEADLINE
        )
        runtime = WatchdogRuntime(
            HeartbeatReceiver(handoff_path=handoff, monotonic=self.monotonic),
            self.root / "evaluator.json",
            self.outbox,
            WatchdogNotifier(self.outbox, sender, monotonic=self.monotonic),
            clock=self.clock,
            monotonic=self.monotonic,
        )
        self.assertEqual(self.tick(runtime, NOW).evaluation.status, "healthy")
        for seconds in (30, 60, 90):
            result = self.tick(runtime, NOW + timedelta(seconds=seconds))
            self.assertEqual(result.evaluation.status, "healthy")
        missing = self.tick(runtime, NOW + timedelta(seconds=91))
        self.assertEqual(missing.evaluation.status, "missing")
        self.assertEqual(missing.delivery.sent, 1)
        self.assertEqual(sender.alerts[-1].status, "missing")
        self.assertEqual(sender.alerts[-1].priority, "critical")
        self.assertFalse(sender.alerts[-1].silent)

        stale_sender = SenderFake()
        stale_source = ReceiveFake(
            [encoded(heartbeat(1, NOW, progress=NOW - timedelta(seconds=91)))]
        )
        stale_root = self.root / "stale"
        stale_outbox = WatchdogOutbox(stale_root / "outbox.sqlite3")
        try:
            stale_runtime = WatchdogRuntime(
                HeartbeatReceiver(transport=stale_source, monotonic=self.monotonic),
                stale_root / "state.json",
                stale_outbox,
                WatchdogNotifier(stale_outbox, stale_sender, monotonic=self.monotonic),
                clock=lambda: NOW,
                monotonic=self.monotonic,
            )
            stale = stale_runtime.tick(deadline=DEADLINE)
            self.assertEqual(stale.evaluation.status, "stale_work")
            self.assertTrue(stale.evaluation.accepted)
            self.assertEqual(stale_sender.alerts[0].status, "stale_work")
            self.assertEqual(stale_sender.alerts[0].priority, "critical")
        finally:
            stale_outbox.close()

    def test_first_three_missing_windows_are_a_durable_startup_grace(self) -> None:
        sender = SenderFake()
        runtime = self.runtime(ReceiveFake([None] * 5), sender)

        for seconds in (0, 30, 60, 90):
            tick = self.tick(runtime, NOW + timedelta(seconds=seconds))
            self.assertEqual(tick.evaluation.status, "starting")
            self.assertEqual(tick.evaluation.reason, "startup_grace")
            self.assertIsNone(tick.queued_event_id)
            self.assertEqual(tick.delivery.sent, 0)

        restarted = self.runtime(ReceiveFake([None]), sender)
        missing = self.tick(restarted, NOW + timedelta(seconds=91))
        self.assertEqual(missing.evaluation.status, "missing")
        self.assertIsNotNone(missing.queued_event_id)
        self.assertEqual([alert.status for alert in sender.alerts], ["missing"])

    def test_unchanged_file_handoff_is_no_new_heartbeat_after_receiver_restart(self) -> None:
        sender = SenderFake()
        handoff = self.root / "received-heartbeat.json"
        HeartbeatExporter(handoff_path=handoff, monotonic=self.monotonic).export(
            heartbeat(7, NOW), deadline=DEADLINE
        )
        first = WatchdogRuntime(
            HeartbeatReceiver(handoff_path=handoff, monotonic=self.monotonic),
            self.root / "evaluator.json",
            self.outbox,
            WatchdogNotifier(self.outbox, sender, monotonic=self.monotonic),
            clock=self.clock,
            monotonic=self.monotonic,
        )
        self.assertEqual(self.tick(first, NOW).evaluation.status, "healthy")

        restarted = WatchdogRuntime(
            HeartbeatReceiver(handoff_path=handoff, monotonic=self.monotonic),
            self.root / "evaluator.json",
            self.outbox,
            WatchdogNotifier(self.outbox, sender, monotonic=self.monotonic),
            clock=self.clock,
            monotonic=self.monotonic,
        )
        unchanged = self.tick(restarted, NOW + timedelta(seconds=30))
        self.assertEqual(
            (
                unchanged.evaluation.status,
                unchanged.evaluation.accepted,
                unchanged.evaluation.reason,
            ),
            ("healthy", False, "no_new_heartbeat"),
        )
        self.assertIsNone(unchanged.queued_event_id)
        self.assertEqual(sender.alerts, [])

    def test_replay_state_and_transition_outbox_survive_restart(self) -> None:
        failing = SenderFake([OSError("local fake failure")])
        first = self.runtime(ReceiveFake([encoded(heartbeat(7, NOW))]), failing)
        self.assertEqual(self.tick(first, NOW).evaluation.status, "healthy")

        replay = self.runtime(
            ReceiveFake([encoded(heartbeat(7, NOW + timedelta(seconds=30)))]), failing
        )
        rejected = self.tick(replay, NOW + timedelta(seconds=30))
        self.assertEqual(rejected.evaluation.reason, "sequence_replay")
        self.assertIsNotNone(rejected.queued_event_id)
        event_id = rejected.queued_event_id
        assert event_id is not None
        self.assertEqual(self.outbox.event_state(event_id)["attempts"], 1)

        self.outbox.close()
        self.outbox = WatchdogOutbox(self.root / "outbox.sqlite3")
        restarted_sender = SenderFake([OSError("still unavailable")])
        restarted = self.runtime(
            ReceiveFake([encoded(heartbeat(7, NOW + timedelta(seconds=60)))]),
            restarted_sender,
        )
        still_rejected = self.tick(restarted, NOW + timedelta(seconds=60))
        self.assertEqual(still_rejected.evaluation.reason, "sequence_replay")
        self.assertIsNone(still_rejected.queued_event_id)
        self.assertEqual(self.outbox.event_state(event_id)["attempts"], 2)

        controller_restarted = self.runtime(
            ReceiveFake(
                [encoded(heartbeat(0, NOW + timedelta(seconds=90), boot_id="controller-b"))]
            ),
            SenderFake(),
        )
        accepted = self.tick(controller_restarted, NOW + timedelta(seconds=90))
        self.assertTrue(accepted.evaluation.accepted)
        self.assertEqual(accepted.evaluation.status, "recovering")

    def test_failed_or_nonsemantic_delivery_retries_before_acknowledgement(self) -> None:
        sender = SenderFake(
            [
                AlertSendReceipt(10, delivery_semantics="http-200"),
                AlertSendReceipt(11),
            ]
        )
        runtime = self.runtime(ReceiveFake([None] * 4), sender)
        self.tick(runtime, NOW)
        first = self.tick(runtime, NOW + timedelta(seconds=91))
        event_id = first.queued_event_id
        assert event_id is not None
        state = self.outbox.event_state(event_id)
        self.assertEqual((first.delivery.failed, state["attempts"]), (1, 1))
        self.assertIsNone(state["acknowledged_at"])

        early = self.tick(runtime, NOW + timedelta(seconds=120))
        self.assertEqual(early.delivery.sent, 0)
        retried = self.tick(runtime, NOW + timedelta(seconds=121))
        state = self.outbox.event_state(event_id)
        self.assertEqual(retried.delivery.sent, 1)
        self.assertIsNotNone(state["acknowledged_at"])
        self.assertEqual(state["telegram_message_id"], 11)
        self.assertEqual(sender.calls[-1], (DEADLINE, MAX_TELEGRAM_RESPONSE_BYTES))

    def test_retry_remains_durable_after_six_attempts_until_telegram_accepts(self) -> None:
        sender = SenderFake([OSError("unavailable")] * 6)
        runtime = self.runtime(ReceiveFake([None] * 8), sender)
        self.tick(runtime, NOW)
        schedule = (91, 121, 181, 301, 541, 841)
        event_id = None
        final = None
        for seconds in schedule:
            final = self.tick(runtime, NOW + timedelta(seconds=seconds))
            event_id = event_id or final.queued_event_id
        assert event_id is not None and final is not None
        state = self.outbox.event_state(event_id)
        self.assertEqual(state["attempts"], 6)
        self.assertEqual(state["next_attempt_at"], utc_text(NOW + timedelta(seconds=1141)))
        self.assertIsNone(state["acknowledged_at"])
        self.assertEqual(final.delivery.exhausted, 0)

        accepted = self.tick(runtime, NOW + timedelta(seconds=1141))
        state = self.outbox.event_state(event_id)
        self.assertEqual(accepted.delivery.sent, 1)
        self.assertEqual(state["attempts"], 6)
        self.assertIsNotNone(state["acknowledged_at"])
        self.assertIsNone(state["next_attempt_at"])

    def test_restart_reactivates_preexisting_six_attempt_recovery_event(self) -> None:
        cursor = self.outbox.connection.execute(
            """INSERT INTO watchdog_transition_outbox(
                 machine_id,status,reason,transition_name,priority,observed_at,
                 message,attempts,next_attempt_at)
               VALUES('17049','healthy','heartbeat_healthy','recovered','recovery',?,?,6,NULL)""",
            (
                utc_text(NOW),
                "Terracompute independent watchdog: machine 17049 controller heartbeat "
                "recovered after stable window (heartbeat_healthy).",
            ),
        )
        event_id = int(cursor.lastrowid)
        self.outbox.connection.commit()
        self.outbox.close()

        self.outbox = WatchdogOutbox(self.root / "outbox.sqlite3")
        sender = SenderFake()
        notifier = WatchdogNotifier(self.outbox, sender, monotonic=self.monotonic)
        result = notifier.send_due(now=NOW, deadline=DEADLINE)

        self.assertEqual(result.sent, 1)
        state = self.outbox.event_state(event_id)
        self.assertEqual(state["attempts"], 6)
        self.assertIsNotNone(state["acknowledged_at"])
        self.assertEqual(sender.alerts[0].priority, "recovery")

    def test_critical_delivery_precedes_recovery_when_both_are_due(self) -> None:
        sender = SenderFake([OSError("hold recovery"), AlertSendReceipt(20), AlertSendReceipt(21)])
        source = ReceiveFake([None, None])
        runtime = self.runtime(source, sender)
        self.tick(runtime, NOW)
        missing = self.tick(runtime, NOW + timedelta(seconds=91))
        assert missing.queued_event_id is not None
        # Turn the first event into a due recovery-priority row, then enqueue a later
        # critical event.  Selection must still send the critical event first.
        self.outbox.connection.execute(
            """UPDATE watchdog_transition_outbox
               SET priority='recovery',next_attempt_at=? WHERE event_id=?""",
            (utc_text(NOW + timedelta(seconds=121)), missing.queued_event_id),
        )
        self.outbox.connection.execute(
            """INSERT INTO watchdog_transition_outbox(
                 machine_id,status,reason,transition_name,priority,observed_at,
                 message,next_attempt_at)
               VALUES('17049','invalid','malformed','invalid_started','critical',?,?,?)""",
            (
                utc_text(NOW + timedelta(seconds=121)),
                "Terracompute independent watchdog: machine 17049 heartbeat rejected (malformed).",
                utc_text(NOW + timedelta(seconds=121)),
            ),
        )
        self.outbox.connection.commit()
        self.now = NOW + timedelta(seconds=121)
        result = runtime.notifier.send_due(now=self.now, deadline=DEADLINE)
        self.assertEqual(result.sent, 2)
        self.assertEqual([alert.priority for alert in sender.alerts[-2:]], ["critical", "recovery"])

    def test_recovery_is_not_sent_until_five_minute_stability(self) -> None:
        sender = SenderFake()
        values = [encoded(heartbeat(1, NOW)), None]
        runtime = self.runtime(ReceiveFake(values), sender)
        self.tick(runtime, NOW)
        missing_at = NOW + timedelta(seconds=91)
        self.tick(runtime, missing_at)
        self.assertEqual([alert.status for alert in sender.alerts], ["missing"])

        recovery_start = NOW + timedelta(seconds=120)
        runtime.receiver.transport.values.extend(
            encoded(heartbeat(sequence, recovery_start + timedelta(seconds=offset)))
            for sequence, offset in enumerate(
                range(0, RECOVERY_STABLE_SECONDS + 1, 30), start=2
            )
        )
        for offset in range(0, RECOVERY_STABLE_SECONDS, 30):
            tick = self.tick(runtime, recovery_start + timedelta(seconds=offset))
            self.assertEqual(tick.evaluation.status, "recovering")
            self.assertEqual([alert.status for alert in sender.alerts], ["missing"])
        recovered = self.tick(
            runtime, recovery_start + timedelta(seconds=RECOVERY_STABLE_SECONDS)
        )
        self.assertEqual(recovered.evaluation.transition, "recovered")
        self.assertEqual([alert.status for alert in sender.alerts], ["missing", "healthy"])
        self.assertEqual(sender.alerts[-1].priority, "recovery")

    def test_payloads_are_credential_free_and_sender_has_no_input_capability(self) -> None:
        document = heartbeat(1, NOW)
        document["telegram_token"] = "redacted"
        exporter = HeartbeatExporter(
            handoff_path=self.root / "heartbeat.json", monotonic=self.monotonic
        )
        with self.assertRaisesRegex(WatchdogRuntimeError, "heartbeat_fields_invalid"):
            exporter.export(document, deadline=DEADLINE)

        sender = SenderFake()
        runtime = self.runtime(ReceiveFake([None, None]), sender)
        self.tick(runtime, NOW)
        tick = self.tick(runtime, NOW + timedelta(seconds=91))
        assert tick.queued_event_id is not None
        rendered = json.dumps(
            {
                "alert": asdict(sender.alerts[0]),
                "outbox": self.outbox.event_state(tick.queued_event_id),
            },
            sort_keys=True,
        ).lower()
        for forbidden in ("credential", "password", "secret", "tenant", "token", "rental"):
            self.assertNotIn(forbidden, rendered)
        self.assertEqual(sender.identity.role, "independent-watchdog-notifier")
        self.assertFalse(sender.identity.consumes_updates)
        self.assertFalse(sender.identity.can_approve)
        self.assertFalse(hasattr(runtime.notifier, "get_updates"))
        self.assertFalse(hasattr(runtime.notifier, "approve"))

        sender.get_updates = lambda: ()
        with self.assertRaisesRegex(WatchdogRuntimeError, "sender_capability_invalid"):
            WatchdogNotifier(self.outbox, sender, monotonic=self.monotonic)


class HealthchecksWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".healthchecks-watchdog-test-",
            dir=Path.cwd(),
            ignore_cleanup_errors=True,
        )
        self.root = Path(self.temp.name).absolute()
        self.now = NOW
        self.monotonic_value = 100.0

    def tearDown(self) -> None:
        for directory, dirnames, filenames in os.walk(self.root, topdown=False):
            for filename in filenames:
                (Path(directory) / filename).chmod(0o600)
            for dirname in dirnames:
                path = Path(directory) / dirname
                if not path.is_symlink():
                    path.chmod(0o700)
            Path(directory).chmod(0o700)
        self.temp.cleanup()

    def healthchecks_runtime(
        self,
        values: list[bytes | None],
        transport: HealthchecksTransportFake,
        name: str,
    ) -> HealthchecksWatchdogRuntime:
        monotonic = lambda: self.monotonic_value
        return HealthchecksWatchdogRuntime(
            HeartbeatReceiver(transport=ReceiveFake(values), monotonic=monotonic),
            self.root / name / "evaluator.json",
            HealthchecksPinger(
                HealthchecksPingURL(PING_URL),
                transport=transport,
                monotonic=monotonic,
            ),
            clock=lambda: self.now,
            monotonic=monotonic,
        )

    def tick(self, runtime: HealthchecksWatchdogRuntime, when: datetime):
        self.now = when
        return runtime.tick(deadline=DEADLINE)

    def test_url_is_strict_and_redacted_everywhere_it_can_render(self) -> None:
        ping_url = HealthchecksPingURL(PING_URL)
        pinger = HealthchecksPinger(ping_url, transport=HealthchecksTransportFake())
        self.assertNotIn(PING_UUID, repr(ping_url))
        self.assertNotIn(PING_UUID, str(ping_url))
        self.assertNotIn(PING_UUID, repr(pinger))
        invalid_urls = (
            f"http://hc-ping.com/{PING_UUID}",
            f"https://example.com/{PING_UUID}",
            f"https://hc-ping.com:443/{PING_UUID}",
            f"https://hc-ping.com/{PING_UUID}/fail",
            f"https://hc-ping.com/{PING_UUID}?status=up",
            f"https://user@hc-ping.com/{PING_UUID}",
            f"https://HC-PING.COM/{PING_UUID}",
            f"https://hc-ping.com/{PING_UUID.upper()}",
        )
        for value in invalid_urls:
            with self.subTest(value=value), self.assertRaises(WatchdogRuntimeError) as caught:
                HealthchecksPingURL(value)
            self.assertEqual(str(caught.exception), "healthchecks_url_invalid")
            self.assertNotIn(PING_UUID, repr(caught.exception))

    def test_credential_file_is_bounded_regular_private_and_redacted(self) -> None:
        credential = self.root / "ping-url"
        credential.write_text(PING_URL + "\n", encoding="ascii")
        credential.chmod(0o600)
        parsed = read_healthchecks_ping_url(credential)
        self.assertNotIn(PING_UUID, repr(parsed))

        credential.chmod(0o644)
        with self.assertRaisesRegex(WatchdogRuntimeError, "healthchecks_credential_invalid"):
            read_healthchecks_ping_url(credential)
        credential.chmod(0o600)
        link = self.root / "ping-url-link"
        link.symlink_to(credential)
        with self.assertRaisesRegex(WatchdogRuntimeError, "healthchecks_credential_unavailable"):
            read_healthchecks_ping_url(link)

        with mock.patch(
            "terracompute_ops.watchdog_runtime.os.geteuid", return_value=os.geteuid() + 1
        ), self.assertRaisesRegex(
            WatchdogRuntimeError, "healthchecks_credential_invalid"
        ):
            read_healthchecks_ping_url(credential)

        invalid = self.root / "invalid-url"
        invalid.write_text(PING_URL + "?leak=" + PING_UUID, encoding="ascii")
        invalid.chmod(0o600)
        with self.assertRaises(WatchdogRuntimeError) as caught:
            read_healthchecks_ping_url(invalid)
        self.assertNotIn(PING_UUID, repr(caught.exception))

    def test_exact_success_and_failure_requests_and_semantic_response(self) -> None:
        transport = HealthchecksTransportFake()
        pinger = HealthchecksPinger(
            HealthchecksPingURL(PING_URL),
            transport=transport,
            monotonic=lambda: self.monotonic_value,
        )
        success = pinger.ping(success=True, deadline=DEADLINE)
        failure = pinger.ping(success=False, deadline=DEADLINE)
        self.assertTrue(success.success)
        self.assertFalse(failure.success)
        self.assertEqual(
            transport.calls,
            [
                (f"/{PING_UUID}", DEADLINE, MAX_HEALTHCHECKS_RESPONSE_BYTES),
                (f"/{PING_UUID}/fail", DEADLINE, MAX_HEALTHCHECKS_RESPONSE_BYTES),
            ],
        )

        for response in (
            HealthchecksHTTPResponse(204, b"OK"),
            HealthchecksHTTPResponse(302, b"OK"),
            HealthchecksHTTPResponse(500, b"OK"),
            HealthchecksHTTPResponse(200, b"ok"),
            HealthchecksHTTPResponse(200, b"OK\n"),
            HealthchecksHTTPResponse(200, b"O" * 17),
        ):
            rejecting = HealthchecksPinger(
                HealthchecksPingURL(PING_URL),
                transport=HealthchecksTransportFake([response]),
                monotonic=lambda: self.monotonic_value,
            )
            with self.subTest(response=response), self.assertRaises(WatchdogRuntimeError):
                rejecting.ping(success=True, deadline=DEADLINE)

        leaking = HealthchecksPinger(
            HealthchecksPingURL(PING_URL),
            transport=HealthchecksTransportFake([OSError(PING_URL)]),
            monotonic=lambda: self.monotonic_value,
        )
        with self.assertRaises(WatchdogRuntimeError) as caught:
            leaking.ping(success=True, deadline=DEADLINE)
        self.assertEqual(str(caught.exception), "healthchecks_transport_failed")
        self.assertNotIn(PING_UUID, repr(caught.exception))

    def test_startup_is_silent_and_all_evaluated_statuses_map_per_tick(self) -> None:
        startup_transport = HealthchecksTransportFake()
        startup = self.healthchecks_runtime([None], startup_transport, "startup")
        result = self.tick(startup, NOW)
        self.assertEqual(result.evaluation.status, "starting")
        self.assertIsNone(result.ping)
        self.assertEqual(startup_transport.calls, [])

        healthy_transport = HealthchecksTransportFake()
        healthy = self.healthchecks_runtime(
            [encoded(heartbeat(1, NOW)), None], healthy_transport, "healthy"
        )
        self.assertEqual(self.tick(healthy, NOW).evaluation.status, "healthy")
        self.assertEqual(
            self.tick(healthy, NOW + timedelta(seconds=30)).evaluation.status,
            "healthy",
        )
        self.assertEqual(
            [path for path, _deadline, _limit in healthy_transport.calls],
            [f"/{PING_UUID}", f"/{PING_UUID}"],
        )

        invalid_transport = HealthchecksTransportFake()
        invalid = self.healthchecks_runtime([encoded({})], invalid_transport, "invalid")
        self.assertEqual(self.tick(invalid, NOW).evaluation.status, "invalid")

        stale_transport = HealthchecksTransportFake()
        stale = self.healthchecks_runtime(
            [encoded(heartbeat(1, NOW, progress=NOW - timedelta(seconds=91)))],
            stale_transport,
            "stale",
        )
        self.assertEqual(self.tick(stale, NOW).evaluation.status, "stale_work")

        recovery_transport = HealthchecksTransportFake()
        recovery = self.healthchecks_runtime(
            [None, None, encoded(heartbeat(1, NOW + timedelta(seconds=120)))],
            recovery_transport,
            "recovery",
        )
        self.assertEqual(self.tick(recovery, NOW).evaluation.status, "starting")
        self.assertEqual(
            self.tick(recovery, NOW + timedelta(seconds=91)).evaluation.status,
            "missing",
        )
        self.assertEqual(
            self.tick(recovery, NOW + timedelta(seconds=120)).evaluation.status,
            "recovering",
        )
        recovery.receiver.transport.values.extend(
            encoded(heartbeat(sequence, NOW + timedelta(seconds=offset)))
            for sequence, offset in enumerate(range(150, 421, 30), start=2)
        )
        for offset in range(150, 420, 30):
            self.assertEqual(
                self.tick(recovery, NOW + timedelta(seconds=offset)).evaluation.status,
                "recovering",
            )
        recovered = self.tick(recovery, NOW + timedelta(seconds=420))
        self.assertEqual(recovered.evaluation.status, "healthy")
        recovery_paths = [path for path, _deadline, _limit in recovery_transport.calls]
        self.assertEqual(recovery_paths[0], f"/{PING_UUID}/fail")
        self.assertTrue(all(path == f"/{PING_UUID}/fail" for path in recovery_paths[:-1]))
        self.assertEqual(recovery_paths[-1], f"/{PING_UUID}")
        for transport in (invalid_transport, stale_transport):
            self.assertEqual(transport.calls[0][0], f"/{PING_UUID}/fail")

    def test_deadline_is_checked_before_and_after_transport(self) -> None:
        pinger = HealthchecksPinger(
            HealthchecksPingURL(PING_URL),
            transport=HealthchecksTransportFake(),
            monotonic=lambda: 111.0,
        )
        with self.assertRaisesRegex(WatchdogRuntimeError, "deadline_invalid"):
            pinger.ping(success=True, deadline=DEADLINE)

        values = iter((100.0, 111.0))
        pinger = HealthchecksPinger(
            HealthchecksPingURL(PING_URL),
            transport=HealthchecksTransportFake(),
            monotonic=lambda: next(values),
        )
        with self.assertRaisesRegex(WatchdogRuntimeError, "deadline_expired"):
            pinger.ping(success=True, deadline=DEADLINE)

    def test_stdlib_transport_pins_origin_method_body_and_response_bound(self) -> None:
        class Response:
            def __init__(self, status, length, body):
                self.status = status
                self.length = length
                self.body = body

            def getheader(self, name):
                return self.length if name == "Content-Length" else None

            def read(self, limit):
                self.limit = limit
                return self.body

            def close(self):
                return None

        class Connection:
            instances = []
            response_status = 200
            response_length = "2"
            response_body = b"OK"

            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.sock = None
                self.response = Response(
                    self.response_status,
                    self.response_length,
                    self.response_body,
                )
                self.requests = []
                self.__class__.instances.append(self)

            def connect(self):
                return None

            def request(self, *args, **kwargs):
                self.requests.append((args, kwargs))

            def getresponse(self):
                return self.response

            def close(self):
                return None

        with mock.patch(
            "terracompute_ops.watchdog_runtime.http.client.HTTPSConnection", Connection
        ):
            transport = StdlibHealthchecksTransport(monotonic=lambda: 100.0)
            response = transport.request(
                f"/{PING_UUID}",
                deadline=DEADLINE,
                max_response_bytes=MAX_HEALTHCHECKS_RESPONSE_BYTES,
            )
        connection = Connection.instances[0]
        self.assertEqual(connection.args[:2], ("hc-ping.com", 443))
        self.assertEqual(connection.kwargs["timeout"], 10.0)
        self.assertEqual(connection.requests[0][0], ("GET", f"/{PING_UUID}"))
        self.assertIsNone(connection.requests[0][1]["body"])
        self.assertEqual(connection.response.limit, MAX_HEALTHCHECKS_RESPONSE_BYTES + 1)
        self.assertEqual(response, HealthchecksHTTPResponse(200, b"OK"))

        Connection.instances.clear()
        Connection.response_status = 302
        with mock.patch(
            "terracompute_ops.watchdog_runtime.http.client.HTTPSConnection", Connection
        ):
            pinger = HealthchecksPinger(
                HealthchecksPingURL(PING_URL),
                transport=StdlibHealthchecksTransport(monotonic=lambda: 100.0),
                monotonic=lambda: 100.0,
            )
            with self.assertRaisesRegex(WatchdogRuntimeError, "response_rejected"):
                pinger.ping(success=True, deadline=DEADLINE)
        self.assertEqual(len(Connection.instances), 1)
        self.assertEqual(len(Connection.instances[0].requests), 1)

        Connection.instances.clear()
        Connection.response_status = 200
        Connection.response_length = str(MAX_HEALTHCHECKS_RESPONSE_BYTES + 1)
        with mock.patch(
            "terracompute_ops.watchdog_runtime.http.client.HTTPSConnection", Connection
        ):
            transport = StdlibHealthchecksTransport(monotonic=lambda: 100.0)
            with self.assertRaisesRegex(WatchdogRuntimeError, "response_too_large"):
                transport.request(
                    f"/{PING_UUID}",
                    deadline=DEADLINE,
                    max_response_bytes=MAX_HEALTHCHECKS_RESPONSE_BYTES,
                )


if __name__ == "__main__":
    unittest.main()
