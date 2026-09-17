"""The read-only target reads: strict parsing, evidence, and nothing mutating."""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from terracompute_ops.inspection import (
    MAX_LINE_CHARS,
    MAX_LINES,
    READ_TOPICS,
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


if __name__ == "__main__":
    unittest.main()
