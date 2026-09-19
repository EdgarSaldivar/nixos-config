from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from terracompute_ops.spool_client import (
    MAX_PROMPT_BYTES,
    Answer,
    SpoolInvestigator,
    SpoolUnavailable,
)

HASH = "a" * 64
TICKET = "d" + "b" * 48


class SpoolInvestigatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.pending = self.root / "requests" / "pending"
        self.completed = self.root / "results" / "completed"
        for path in (self.pending, self.completed):
            path.mkdir(mode=0o700, parents=True)
        self.spool = SpoolInvestigator(
            self.root / "requests", self.root / "results", self.root / "staging"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ask(self, ticket: str = TICKET, **changes) -> bool:
        arguments = {
            "incident_id": "key-0000:a1:00.0",
            "evidence_hash": HASH,
            "severity": "critical",
            "prompt": "What is wrong with the machine?",
        }
        arguments.update(changes)
        return self.spool.ask(ticket, **arguments)

    def answer(self, ticket: str = TICKET, **changes) -> None:
        document = {
            "schema_version": 1, "request_id": ticket, "machine_id": "17049",
            "incident_id": "key-0000:a1:00.0", "evidence_hash": HASH,
            "severity": "critical", "status": "completed", "reason": None,
            "episode_id": 1, "reported_tokens": 10, "overshoot_tokens": 0,
            "report": '{"summary": "it is broken"}', "completed_utc": "2026-09-17T06:00:00Z",
        }
        document.update(changes)
        (self.completed / f"{ticket}.json").write_text(json.dumps(document))

    # -- asking -------------------------------------------------------------------

    def test_a_question_arrives_whole_and_private(self) -> None:
        self.assertTrue(self.ask())
        published = self.pending / f"{TICKET}.json"
        # Readable by the group the runtime shares with us: it is not the owner, and
        # a request it cannot open is a request it can only quarantine.
        self.assertEqual(stat.S_IMODE(published.stat().st_mode), 0o640)
        document = json.loads(published.read_text())
        self.assertEqual(set(document), {
            "schema_version", "request_id", "machine_id", "incident_id",
            "evidence_hash", "severity", "prompt", "kind", "investigation_id",
        })
        self.assertEqual(document["machine_id"], "17049")
        # Nothing half-written is ever left where the investigator looks.
        self.assertEqual([path.name for path in self.pending.iterdir()], [f"{TICKET}.json"])

    def test_a_strict_umask_cannot_change_the_mode_the_runtime_requires(self) -> None:
        """The runtime wants exactly 0600, and a umask takes bits off the owner too."""
        previous = os.umask(0o277)
        try:
            self.ask()
        finally:
            os.umask(previous)
        self.assertEqual(
            stat.S_IMODE((self.pending / f"{TICKET}.json").stat().st_mode), 0o640
        )

    def test_a_question_is_built_outside_the_investigators_spool(self) -> None:
        """A half-written file where the runtime looks would be quarantined, not read."""
        real = os.rename
        seen: list[str] = []

        def watch(source, destination):
            # What the runtime would find if it looked at this instant.
            seen.extend(path.name for path in self.pending.iterdir())
            return real(source, destination)

        os.rename = watch
        try:
            self.ask()
        finally:
            os.rename = real
        self.assertEqual(seen, [], "built the question where the runtime would claim it")
        self.assertEqual([path.name for path in self.pending.iterdir()], [f"{TICKET}.json"])

    def test_a_staging_directory_we_do_not_own_is_used_as_it_is(self) -> None:
        """In production the deployment provisions it; re-moding it would fail."""
        self.spool.staging.mkdir(mode=0o770, parents=True)
        before = stat.S_IMODE(self.spool.staging.stat().st_mode)
        self.assertTrue(self.ask())
        self.assertEqual(stat.S_IMODE(self.spool.staging.stat().st_mode), before)

    def test_asking_twice_does_not_ask_twice(self) -> None:
        self.assertTrue(self.ask())
        self.assertFalse(self.ask(), "published a second copy of a waiting question")
        self.assertTrue(self.spool.waiting(TICKET))

    def test_a_claimed_question_is_no_longer_waiting(self) -> None:
        self.ask()
        (self.pending / f"{TICKET}.json").unlink()  # The runtime claimed it.
        self.assertFalse(self.spool.waiting(TICKET))

    def test_nothing_unacceptable_is_ever_written(self) -> None:
        for reason, changes in (
            ("incident", {"incident_id": "key/../etc"}),
            ("hash", {"evidence_hash": "not-a-digest"}),
            ("severity", {"severity": "urgent"}),
            ("empty prompt", {"prompt": "   "}),
            ("nul in prompt", {"prompt": "what\x00now"}),
            ("huge prompt", {"prompt": "x" * (MAX_PROMPT_BYTES + 1)}),
            ("unknown kind", {"kind": "improvise"}),
        ):
            with self.assertRaises(ValueError, msg=reason):
                self.ask(**changes)
        for ticket in ("", "../escape", "d" * 65, "-leading"):
            with self.assertRaises(ValueError, msg=ticket):
                self.ask(ticket)
        self.assertEqual(list(self.pending.iterdir()), [])
        self.assertEqual(list(self.spool.staging.glob("*")), [])

    def test_a_spool_that_cannot_be_written_is_not_fatal(self) -> None:
        (self.root / "requests" / "pending").chmod(0o500)
        try:
            with self.assertRaises(SpoolUnavailable):
                self.ask()
        finally:
            (self.root / "requests" / "pending").chmod(0o700)
        self.assertEqual(list(self.spool.staging.glob("*")), [], "left litter behind")

    # -- collecting ---------------------------------------------------------------

    def test_no_answer_is_not_an_answer(self) -> None:
        self.assertIsNone(self.spool.collect(TICKET))

    def test_an_answer_is_read_once(self) -> None:
        self.answer()
        answer = self.spool.collect(TICKET)
        self.assertEqual(answer, Answer("completed", '{"summary": "it is broken"}', None))
        self.assertIsNone(self.spool.collect(TICKET), "the answer was read twice")

    def test_an_answer_that_is_not_ours_is_refused(self) -> None:
        for reason, changes in (
            ("another request", {"request_id": "d" + "c" * 48}),
            ("another machine", {"machine_id": "99999"}),
        ):
            self.answer(**changes)
            answer = self.spool.collect(TICKET)
            self.assertEqual(answer.status, "unavailable", reason)
            self.assertIsNone(self.spool.collect(TICKET))

    def test_an_unreadable_answer_does_not_come_back_every_pass(self) -> None:
        (self.completed / f"{TICKET}.json").write_text("{not json")
        self.assertEqual(self.spool.collect(TICKET).reason, "result-unreadable")
        self.assertIsNone(self.spool.collect(TICKET))

    def test_an_answer_carries_its_reason_when_it_failed(self) -> None:
        self.answer(status="unavailable", reason="auth-or-quota-unavailable", report="")
        answer = self.spool.collect(TICKET)
        self.assertEqual((answer.status, answer.reason), ("unavailable", "auth-or-quota-unavailable"))

    def test_an_oversized_answer_is_refused_unread(self) -> None:
        (self.completed / f"{TICKET}.json").write_text("x" * (256 * 1024))
        with self.assertRaises(SpoolUnavailable):
            self.spool.collect(TICKET)


if __name__ == "__main__":
    unittest.main()
