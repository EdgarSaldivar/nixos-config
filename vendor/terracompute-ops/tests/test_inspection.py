"""The read-only target reads: strict parsing, evidence, and nothing mutating."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from terracompute_ops.inspection import (
    MAX_LINE_CHARS,
    MAX_LINES,
    OBSERVE_TIMEOUT_SECONDS,
    READ_TOPICS,
    TargetObserver,
    TargetReader,
    parse_read,
    summarize,
)
from terracompute_ops.monitor_restart import ActorError, EvidenceStore, SSHActorClient

REQUEST = "00000000-0000-4000-8000-000000000001"
START = datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)


def read_document(topic: str, request_id: str = REQUEST, **changes) -> dict:
    document = {
        "schema_version": 1,
        "operation": "inspect",
        "id": request_id,
        "component": topic,
        "machine_id": 17049,
        "ok": True,
        "observed_at": "2026-09-17T06:00:00Z",
        "hostname": "terracompute",
        "board": "ROME2D32GM-2T",
        "boot_id": "f183fc28-44d0-4e80-a294-1da917fd1a76",
        "topic": topic,
        "lines": ["GPU-1111, 330423, python, 4050 MiB"],
        "truncated": False,
    }
    document.update(changes)
    return document


class FakeReadClient:
    def __init__(self, documents=None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.documents = documents or {}
        self.error: Exception | None = None

    def run(self, operation: str, request_id: str) -> dict:
        self.calls.append((operation, request_id))
        if self.error is not None:
            raise self.error
        topic = operation.split(" ", 1)[1]
        return self.documents.get(topic, read_document(topic, request_id))


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


class InspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.evidence = EvidenceStore(self.db, self.clock)
        self.client = FakeReadClient()
        self.reader = TargetReader(
            self.client, self.evidence, request_id_factory=lambda: REQUEST, clock=self.clock
        )

    def test_a_read_is_parsed_and_kept_as_evidence(self) -> None:
        result = self.reader.read("gpu-processes", subject="incident-1")
        self.assertEqual((result.topic, result.ok, result.truncated), ("gpu-processes", True, False))
        self.assertEqual(result.lines, ("GPU-1111, 330423, python, 4050 MiB",))
        self.assertEqual(self.client.calls, [("inspect gpu-processes", REQUEST)])
        row = self.db.execute(
            "SELECT kind, subject, document_json FROM tc_action_evidence"
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("target-read", "incident-1"))
        self.assertEqual(json.loads(row[2])["topic"], "gpu-processes")

    def test_only_catalogued_topics_are_sent(self) -> None:
        for bad in ("/etc/shadow", "tenant-logs", "gpu-processes; id", "restart"):
            with self.subTest(bad=bad):
                with self.assertRaises(ActorError):
                    self.reader.read(bad)
        self.assertEqual(self.client.calls, [])

    def test_answers_that_do_not_match_the_request_are_rejected(self) -> None:
        cases = {
            "wrong topic": dict(read_document("containers"), topic="gpu-processes"),
            "wrong operation": read_document("containers", operation="status"),
            "wrong id": read_document("containers", id="00000000-0000-4000-8000-000000000002"),
            "wrong machine": read_document("containers", machine_id=17050),
            "lines not a list": read_document("containers", lines="oops"),
            "too many lines": read_document("containers", lines=["x"] * (MAX_LINES + 1)),
            "line too long": read_document("containers", lines=["x" * (MAX_LINE_CHARS + 1)]),
            "control characters": read_document("containers", lines=["a\x00b"]),
            "truncated not a bool": read_document("containers", truncated="yes"),
            "no timestamp": read_document("containers", observed_at=None),
        }
        for name, document in cases.items():
            with self.subTest(name):
                with self.assertRaises(ActorError):
                    parse_read(document, "containers", REQUEST)

    def test_a_refused_read_is_reported_not_raised(self) -> None:
        document = read_document("kernel-gpu-log", ok=False, reason="timeout", lines=[])
        result = parse_read(document, "kernel-gpu-log", REQUEST)
        self.assertEqual((result.ok, result.failure, result.lines), (False, "timeout", ()))

    def test_read_all_reports_a_broken_channel_per_topic(self) -> None:
        self.client.error = ActorError("actor_timeout")
        answers = self.reader.read_all()
        self.assertEqual(sorted(answers), sorted(READ_TOPICS))
        self.assertTrue(all(answer == "unavailable: ActorError" for answer in answers.values()))
        self.assertIn("unavailable", summarize(answers))

    def test_summary_is_bounded_and_names_every_topic(self) -> None:
        documents = {
            topic: read_document(topic, lines=[f"{topic} line {index}" for index in range(MAX_LINES)],
                                 truncated=True)
            for topic in READ_TOPICS
        }
        self.client.documents = documents
        summary = summarize(self.reader.read_all())
        for topic in READ_TOPICS:
            self.assertIn(f"## {topic}", summary)
        self.assertIn("earlier lines dropped", summary)
        self.assertLessEqual(len(summary), 64 * 1024)

    def test_the_ssh_client_builds_only_catalogued_commands(self) -> None:
        client = SSHActorClient(
            ssh_binary="/usr/bin/ssh", target="terracompute-actor@10.50.0.2",
            identity_file="/run/credentials/identity", known_hosts_file="/run/credentials/hosts",
        )
        captured: list[list[str]] = []

        def fake_run(argv, timeout):
            captured.append(argv)
            return read_document(argv[-1].split(" ")[1], argv[-1].split(" ")[2])

        import terracompute_ops.monitor_restart as module
        original = module._run_bounded_json
        module._run_bounded_json = fake_run
        self.addCleanup(lambda: setattr(module, "_run_bounded_json", original))
        client.run("inspect gpu-processes", REQUEST)
        self.assertEqual(captured[-1][-1], f"inspect gpu-processes {REQUEST}")
        client.run("status", REQUEST)
        self.assertEqual(captured[-1][-1], f"status dcgm-exporter {REQUEST}")
        for bad in ("inspect", "inspect /etc/shadow", "inspect a b", "shell", "status extra",
                    "inspect gpu-processes; id"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    client.run(bad, REQUEST)


class SessionTransportTests(unittest.TestCase):
    """The controller carrying an agent-authored command to the target.

    The one property that matters: the command rides stdin, and the SSH command line is
    three fixed tokens with nothing model-derived in them. If that ever stops being true,
    an agent's text reaches a shell, and the whole design is undone.
    """

    def client(self) -> SSHActorClient:
        return SSHActorClient(
            ssh_binary="/usr/bin/ssh", target="terracompute-actor@10.50.0.2",
            identity_file="/run/credentials/identity", known_hosts_file="/run/credentials/hosts",
        )

    def test_the_script_rides_stdin_and_never_the_command_line(self) -> None:
        import terracompute_ops.monitor_restart as module
        captured: dict[str, object] = {}

        def fake_run(argv, timeout, stdin_bytes=None):
            captured["argv"] = argv
            captured["timeout"] = timeout
            captured["stdin"] = stdin_bytes
            return {"ok": True, "lines": [], "truncated": False}

        original = module._run_bounded_json
        module._run_bounded_json = fake_run
        self.addCleanup(lambda: setattr(module, "_run_bounded_json", original))

        script = "docker restart dcgm-exporter; echo done"
        client = self.client()
        client.session(script, REQUEST, writable=True)
        # The command line names the host and the request id, and carries no part of the
        # script -- not even a fragment of it.
        self.assertEqual(captured["argv"][-1], f"session host {REQUEST}")
        self.assertNotIn("docker", " ".join(captured["argv"]))
        self.assertNotIn("restart", " ".join(captured["argv"]))
        # The script is the stdin payload, exactly and only.
        self.assertEqual(captured["stdin"], script.encode("utf-8"))
        # Observation and management are visibly different requests, and observation is
        # the default: a write is a thing you ask for, not a thing you get by omission.
        client.session("cat /proc/uptime", REQUEST)
        self.assertEqual(captured["argv"][-1], f"observe host {REQUEST}")

    def test_a_malformed_session_never_leaves_this_process(self) -> None:
        import terracompute_ops.monitor_restart as module
        original = module._run_bounded_json
        self.addCleanup(lambda: setattr(module, "_run_bounded_json", original))
        called: list[object] = []
        module._run_bounded_json = lambda *a, **k: called.append(a) or {"ok": True}
        client = self.client()
        for script, request in (
            ("", REQUEST),
            ("   \n", REQUEST),
            ("echo fine", "not-a-uuid"),
            ("echo \x00 bad", REQUEST),
            ("x" * (module.MAX_SESSION_SCRIPT_BYTES + 1), REQUEST),
        ):
            with self.subTest(script=script[:12], request=request):
                with self.assertRaises(ValueError):
                    client.session(script, request)

    def test_the_runner_pipes_stdin_and_reads_the_reply_for_real(self) -> None:
        """A real subprocess, to prove the stdin/stdout loop does not deadlock.

        The child reads its whole stdin, then writes a JSON object reporting how many
        bytes it saw -- so both directions must complete for the test to pass. A large
        payload forces more than one write, which is where a naive loop stalls.
        """
        import terracompute_ops.monitor_restart as module
        payload = ("echo hi\n" * 20000).encode("utf-8")  # ~160 KiB, several pipe buffers
        self.assertGreater(len(payload), 64 * 1024)
        # wc -c counts stdin exactly, where $(cat) would strip the trailing newline.
        program = (
            'n=$(wc -c | tr -d " "); '
            r'printf %s "{\"ok\": true, \"bytes\": $n, \"lines\": [], '
            r'\"truncated\": false}"'
        )
        document = module._run_bounded_json(
            ["/bin/sh", "-c", program], timeout=30.0, stdin_bytes=payload
        )
        self.assertTrue(document["ok"])
        self.assertEqual(document["bytes"], len(payload))

    def test_the_runner_still_honours_the_output_cap_with_stdin(self) -> None:
        import terracompute_ops.monitor_restart as module
        program = "cat >/dev/null; yes X | head -c 200000"
        with self.assertRaises(ActorError) as raised:
            module._run_bounded_json(
                ["/bin/sh", "-c", program], timeout=30.0, stdin_bytes=b"ignored"
            )
        self.assertEqual(str(raised.exception), "actor_output_limit")


if __name__ == "__main__":
    unittest.main()


class TargetObserverTests(unittest.TestCase):
    """The reads a model wrote, run under the profile that cannot write."""

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.clock = lambda: datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
        self.evidence = EvidenceStore(self.db, self.clock)
        self.calls: list[tuple] = []

    def observer(self, reply):
        outer = self

        class Client:
            def session(self, script, request_id, *, writable=False, timeout=None):
                outer.calls.append((script, request_id, writable, timeout))
                if isinstance(reply, Exception):
                    raise reply
                return dict(reply, id=request_id)

        return TargetObserver(
            Client(), self.evidence, request_id_factory=lambda: "11111111-1111-4111-8111-111111111111"
        )

    def envelope(self, **changes):
        return dict({
            "schema_version": 1, "operation": "observe", "component": "host",
            "machine_id": 17049, "ok": True, "lines": ["nvidia0 held by pid 101"],
            "truncated": False, "exit_code": 0,
        }, **changes)

    def test_a_read_runs_read_only_under_its_own_deadline(self) -> None:
        result = self.observer(self.envelope()).observe("ls -l /proc/*/fd")
        self.assertTrue(result.ok)
        self.assertIn("nvidia0 held by pid 101", result.text())
        _script, _id, writable, timeout = self.calls[0]
        self.assertFalse(writable, "a model-authored read could write")
        self.assertEqual(timeout, OBSERVE_TIMEOUT_SECONDS)

    def test_a_command_that_failed_does_not_read_as_an_empty_success(self) -> None:
        """The host reports ok for anything that ran, whatever it exited with."""
        result = self.observer(self.envelope(lines=[], exit_code=2)).observe("cat /nope")
        self.assertTrue(result.ok)
        self.assertIn("exit status 2", result.text())

    def test_an_answer_from_somewhere_else_never_enters_the_transcript(self) -> None:
        result = self.observer(self.envelope(machine_id=17050)).observe("uptime")
        self.assertFalse(result.ok)
        self.assertIn("did not run", result.text())

    def test_an_unreachable_host_is_a_result_not_a_crash(self) -> None:
        result = self.observer(OSError("no route")).observe("uptime")
        self.assertFalse(result.ok)
        self.assertIn("did not run", result.text())

    def test_why_a_read_failed_is_evidence_and_is_kept(self) -> None:
        """A sysfs read that HANGS on the device under suspicion says something about
        that device. Reported as a bare class name it said nothing at all.

        This happened: `readlink /sys/bus/pci/devices/0000:a1:00.0/driver` blocked past
        the timeout on the very GPU being diagnosed, and the model was told only that
        the read "did not run".
        """
        result = self.observer(ActorError("actor_timeout")).observe(
            "readlink -f /sys/bus/pci/devices/0000:a1:00.0/driver"
        )
        self.assertFalse(result.ok)
        self.assertIn("actor_timeout", result.text())

    def test_a_failure_to_keep_the_copy_does_not_re_run_the_command(self) -> None:
        """The caller persists the output only once this returns.

        An error escaping here leaves the read queued, so the same command runs on the
        host again next pass -- and an oversized document is deterministic, so that is
        not a retry, it is every tick until the deadline.
        """
        observer = self.observer(self.envelope())
        refused = []

        def record(kind, subject, document):
            refused.append(document)
            if len(refused) == 1:
                raise ValueError("evidence document is too large")
            return "ref"

        observer.evidence.record = record
        result = observer.observe("cat /var/log/huge")
        self.assertTrue(result.ok, "a rejected copy lost the read as well")
        self.assertEqual(len(refused), 2, "nothing was kept about the read at all")
        self.assertIn("output not kept", str(refused[1]))

    def test_a_non_zero_exit_survives_being_cut_to_fit(self) -> None:
        """Everything downstream keeps the tail of a long read, so a line appended at
        the end is exactly what goes missing."""
        result = self.observer(
            self.envelope(lines=["noise"] * 50, exit_code=2)
        ).observe("cat /nope")
        self.assertIn("exit status 2", result.text()[:200])

    def test_every_read_is_recorded_before_it_is_handed_over(self) -> None:
        self.observer(self.envelope()).observe("uptime", subject="incident:x")
        rows = self.db.execute(
            "SELECT kind, subject FROM tc_action_evidence WHERE kind='target-observe'"
        ).fetchall()
        self.assertEqual(rows, [("target-observe", "incident:x")])
