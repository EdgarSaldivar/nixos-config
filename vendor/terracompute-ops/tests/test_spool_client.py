from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terracompute_ops.spool_client import (
    MAX_PROMPT_BYTES,
    Answer,
    SpoolInvestigator,
    SpoolUnavailable,
)
from terracompute_ops.work_owner import WorkOwner

HASH = "a" * 64
TICKET = "d" + "b" * 48
BOOT = "11111111-2222-4333-8444-555555555555"
LOOP = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class SpoolInvestigatorTests(unittest.TestCase):
    def owner(self) -> WorkOwner:
        return WorkOwner("17049", TICKET, LOOP, BOOT, HASH,
                         incident_id="key-0000:a1:00.0", episode_id=3)

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
            "evidence_hash", "severity", "prompt", "kind", "investigation_id", "effort",
            "requested",
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

    def test_owned_request_is_bounded_and_cancellation_is_producer_owned(self) -> None:
        owner = self.owner()
        self.assertTrue(self.ask(owner=owner, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
        request = json.loads((self.pending / f"{TICKET}.json").read_text())
        self.assertEqual(request["schema_version"], 6)
        self.assertEqual(request["owner"], owner.document())
        self.assertEqual(self.spool.progress(owner).phase, "queued")
        self.spool.request_cancellation(owner)
        intent = self.completed / f"cancel-{TICKET}.json"
        self.assertEqual(stat.S_IMODE(intent.stat().st_mode), 0o640)
        self.assertEqual(set(json.loads(intent.read_text())), {"schema_version", "owner"})
        self.assertFalse((self.root / "requests" / "claimed").exists())

    def test_owned_result_peek_replay_and_ack_after_persistence(self) -> None:
        owner = self.owner()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.answer(schema_version=2, owner=owner.document(), dispatch_state="not_dispatched")
        self.assertEqual(self.spool.peek(TICKET, owner=owner, expires_at=expiry).status, "completed")
        self.assertEqual(self.spool.peek(TICKET, owner=owner, expires_at=expiry).status, "completed")
        self.assertTrue((self.completed / f"{TICKET}.json").exists())
        self.assertTrue(self.spool.acknowledge(TICKET, owner=owner, expires_at=expiry))
        self.assertIsNone(self.spool.peek(TICKET, owner=owner, expires_at=expiry))

    def test_owned_result_mismatch_fails_closed_but_can_be_discarded(self) -> None:
        owner = self.owner()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.answer(schema_version=2, owner=owner.document(), evidence_hash="c" * 64)
        self.assertEqual(self.spool.peek(TICKET, owner=owner, expires_at=expiry).reason,
                         "result-owner-mismatch")
        with self.assertRaises(SpoolUnavailable):
            self.spool.acknowledge(TICKET, owner=owner, expires_at=expiry)
        self.spool.discard(TICKET)
        self.assertIsNone(self.spool.peek(TICKET, owner=owner, expires_at=expiry))

    def test_cancellation_or_expiry_after_completion_hides_the_report(self) -> None:
        owner = self.owner()
        self.answer(schema_version=2, owner=owner.document())
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.spool.peek(TICKET, owner=owner, expires_at=past).reason,
                         "owner-expired")
        self.spool.request_cancellation(owner)
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        answer = self.spool.peek(TICKET, owner=owner, expires_at=future)
        self.assertEqual((answer.status, answer.text, answer.reason),
                         ("rejected", "", "owner-cancelled"))
        self.assertTrue(self.spool.acknowledge(TICKET, owner=owner, expires_at=future))
        self.assertFalse((self.completed / f"cancel-{TICKET}.json").exists())

    def test_symlink_and_hardlink_results_are_rejected(self) -> None:
        outside = self.root / "outside"
        outside.write_text("{}")
        path = self.completed / f"{TICKET}.json"
        path.symlink_to(outside)
        with self.assertRaises(SpoolUnavailable):
            self.spool.peek(TICKET)
        path.unlink()
        os.link(outside, path)
        with self.assertRaises(SpoolUnavailable):
            self.spool.peek(TICKET)


if __name__ == "__main__":
    unittest.main()
