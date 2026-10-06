"""Durable producer ownership and acceptance before spool acknowledgement.

tc_action_work retains requests/results, expiry and cancellation intentions. The
coordinator supplies a context before publication and revalidates it on every
read; cached results remain replayable after an interrupted service tick.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from .spool_client import Answer, SpoolUnavailable
from .work_owner import WorkOwner


def utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class OwnedWork:
    def __init__(self, db: sqlite3.Connection, clock, valid):
        self.db, self.clock, self.valid = db, clock, valid
        self.context: dict | None = None
        self.context_provider = None
        self.spools: dict[int, OwnedSpool] = {}
        db.execute("""CREATE TABLE IF NOT EXISTS tc_action_work (
            request_id TEXT PRIMARY KEY, owner_json TEXT NOT NULL,
            context_json TEXT NOT NULL, expires_utc TEXT NOT NULL,
            state TEXT NOT NULL, answer_json TEXT, cancel_pending INTEGER NOT NULL DEFAULT 0,
            created_utc TEXT NOT NULL, updated_utc TEXT NOT NULL)""")
        db.execute("CREATE TABLE IF NOT EXISTS tc_action_work_cursor (id INTEGER PRIMARY KEY CHECK(id=1), last_id TEXT NOT NULL)")
        db.execute("INSERT OR IGNORE INTO tc_action_work_cursor VALUES(1,'')")
        db.commit()

    def wrap(self, spool):
        if isinstance(spool, OwnedSpool):
            return spool
        key = id(spool)
        if key not in self.spools:
            self.spools[key] = OwnedSpool(spool, self)
        return self.spools[key]

    def row(self, ticket: str):
        cursor = self.db.cursor()
        cursor.row_factory = sqlite3.Row
        return cursor.execute("SELECT * FROM tc_action_work WHERE request_id=?", (ticket,)).fetchone()

    def reconcile(self) -> None:
        last = self.db.execute('SELECT last_id FROM tc_action_work_cursor WHERE id=1').fetchone()[0]
        active = "state IN ('publishing','pending','answered','obsolete')"
        rows = self.db.execute(f"SELECT request_id FROM tc_action_work WHERE {active} AND request_id>? ORDER BY request_id LIMIT 256", (last,)).fetchall()
        if len(rows) < 256:
            rows += self.db.execute(f"SELECT request_id FROM tc_action_work WHERE {active} AND request_id<=? ORDER BY request_id LIMIT ?", (last, 256-len(rows))).fetchall()
        # Durable rotation prevents retained uncertain work from starving newer work.
        if rows:
            self.db.execute('UPDATE tc_action_work_cursor SET last_id=? WHERE id=1', (rows[-1][0],))
            self.db.commit()
        for (ticket,) in rows:
            row = self.row(ticket)
            context = json.loads(row["context_json"])
            expired = self.clock() >= datetime.fromisoformat(row["expires_utc"].replace("Z", "+00:00"))
            if row["state"] != "obsolete" and (row["state"] == "publishing" or expired or not self.valid(context)):
                self.db.execute("UPDATE tc_action_work SET state='obsolete',cancel_pending=1,updated_utc=? WHERE request_id=?", (utc(self.clock()), ticket))
                self.db.commit()
            row = self.row(ticket)
            if row["state"] == "obsolete":
                for wrapper in self.spools.values():
                    wrapper.retire(row)


class OwnedSpool:
    def __init__(self, spool, work: OwnedWork):
        self.spool, self.work = spool, work

    def __getattr__(self, name):
        return getattr(self.spool, name)

    def ask(self, ticket: str, **kwargs):
        row = self.work.row(ticket)
        if row is not None:
            return False  # Published once; runtime owns any claim/in-flight replay.
        context = (self.work.context_provider(ticket, kwargs)
                   if kwargs.get("kind") == "converse" and self.work.context_provider
                   else self.work.context)
        if not context or not self.work.valid(context):
            raise SpoolUnavailable("current-owner-unavailable")
        if context.get("episode"):
            kwargs["incident_id"] = context["subject"]
        owner = WorkOwner("17049", ticket, context["loop_id"], context["boot_id"],
                          kwargs["evidence_hash"],
                          incident_id=kwargs["incident_id"] if context.get("episode") else None,
                          episode_id=context.get("episode"),
                          operator_generation=None if context.get("episode") else context["loop_id"])
        expiry = self.work.clock() + timedelta(minutes=20)
        self.work.db.execute("""INSERT INTO tc_action_work VALUES(?,?,?,?,'publishing',NULL,0,?,?)""",
            (ticket, json.dumps(owner.document()), json.dumps(context), utc(expiry),
             utc(self.work.clock()), utc(self.work.clock())))
        self.work.db.commit()
        try:
            result = self.spool.ask(ticket, **kwargs, owner=owner, expires_at=expiry)
        except Exception:
            # Publication may already have happened. Retain intent; no duplicate turn.
            self.work.db.execute("UPDATE tc_action_work SET state='obsolete',cancel_pending=1 WHERE request_id=?", (ticket,))
            self.work.db.commit()
            raise
        self.work.db.execute("UPDATE tc_action_work SET state='pending' WHERE request_id=?", (ticket,))
        self.work.db.commit()
        return result

    def collect(self, ticket: str):
        row = self.work.row(ticket)
        if row is None:
            # Unknown legacy results never acquire authority in an owned coordinator.
            return None
        context = json.loads(row["context_json"])
        owner = WorkOwner.parse(json.loads(row["owner_json"]))
        expiry = datetime.fromisoformat(row["expires_utc"].replace("Z", "+00:00"))
        if row["state"] == "obsolete" or not self.work.valid(context) or self.work.clock() >= expiry:
            self.work.db.execute("UPDATE tc_action_work SET state='obsolete',cancel_pending=1 WHERE request_id=?", (ticket,))
            self.work.db.commit()
            self.retire(self.work.row(ticket))
            return Answer("rejected", "", "owner-obsolete")
        if row["answer_json"]:
            raw = json.loads(row["answer_json"])
            self._ack(ticket, owner, expiry)
            return Answer(**raw)
        answer = self.spool.peek(ticket, owner=owner, expires_at=expiry)
        if answer is None:
            return None
        if not self.work.valid(context):
            answer = Answer("rejected", "", "owner-obsolete")
        self.work.db.execute("UPDATE tc_action_work SET state='answered',answer_json=?,updated_utc=? WHERE request_id=?",
            (json.dumps(dict(status=answer.status, text=answer.text, reason=answer.reason)), utc(self.work.clock()), ticket))
        self.work.db.commit()
        self._ack(ticket, owner, expiry)
        return answer

    def _ack(self, ticket, owner, expiry) -> None:
        try:
            self.spool.acknowledge(ticket, owner=owner, expires_at=expiry)
        except SpoolUnavailable:
            self.spool.discard(ticket, owner=owner)
        self.work.db.execute("UPDATE tc_action_work SET state='accepted' WHERE request_id=? AND state='answered'", (ticket,))
        self.work.db.commit()

    def retire(self, row) -> None:
        owner = WorkOwner.parse(json.loads(row["owner_json"]))
        expiry = datetime.fromisoformat(row["expires_utc"].replace("Z", "+00:00"))
        try:
            if row["cancel_pending"]:
                self.spool.request_cancellation(owner)
                self.work.db.execute("UPDATE tc_action_work SET cancel_pending=0 WHERE request_id=?", (row["request_id"],))
                self.work.db.commit()
            answer = self.spool.peek(row["request_id"], owner=owner, expires_at=expiry)
            if answer is not None:
                # Obsolete state is already durable; its output has no authority.
                try:
                    self.spool.acknowledge(row["request_id"], owner=owner, expires_at=expiry)
                except SpoolUnavailable:
                    self.spool.discard(row["request_id"], owner=owner)
                self.work.db.execute("UPDATE tc_action_work SET state='retired' WHERE request_id=?", (row["request_id"],))
                self.work.db.commit()
        except SpoolUnavailable:
            pass  # Retry retained intention; never claim the runtime stopped.
