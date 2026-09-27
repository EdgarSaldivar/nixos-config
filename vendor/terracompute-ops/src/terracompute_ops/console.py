"""A local operator console for the actions service, and the record of what it said.

Two halves:

- :class:`OutboxRecorder` wraps the Telegram client inside the actions service. Every
  message it sends is written to ``tc_action_outbox``. The Telegram Bot API cannot read
  back what a bot said, so until this existed, "what did the agent tell the operator?"
  could only be pieced together from model session logs.
- The ``terracompute-console`` command lets someone with root on the controller ask the
  agent a question and read the conversation back. The question enters the same inbox a
  Telegram message does, and the answer still goes to the group, so the operator sees
  everything the console does.

What the console cannot do is the point of it. It files questions and nothing else:
never an approval, never an instruction such as ``/now``, and it cannot resume a pause
or release a hold -- the service refuses those steers when they come from it. Every plan
the agent proposes still waits for a person's tap in Telegram. Only root on the
controller can use it, and root there can already do anything, so it adds a faster
doorway and no new authority.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .secrets_scrub import scrub

# Who a console question is from. Telegram user ids are positive; this can never be one.
CONSOLE_SENDER = -1
# The steers a console question may not cause. Each lifts a restraint a person put on
# the agent, and lifting one is a person's decision, made in Telegram.
CONSOLE_FORBIDDEN_STEERS = frozenset({"resume", "release"})
NAMESPACE = "terracompute-actions-telegram-v1"
MAX_QUESTION_CHARS = 4000
MAX_OUTBOX_TEXT = 8000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OutboxRecorder:
    """The Telegram client, with every sent message written down as it goes.

    Recording never stands in the way of sending: a failure to write the record is
    swallowed, because a lost log line costs less than a lost message to the operator.
    """

    def __init__(self, client: Any, db: sqlite3.Connection, clock: Callable[[], datetime]):
        self._client = client
        self._db = db
        self._clock = clock
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_outbox (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 sent_utc TEXT NOT NULL,
                 chat_id INTEGER,
                 message_id INTEGER,
                 buttons TEXT NOT NULL DEFAULT '',
                 outcome TEXT NOT NULL,
                 text TEXT NOT NULL
               )"""
        )
        self._db.commit()

    def send_message(self, chat_id: Any, message: str, *args: Any, **kwargs: Any) -> Any:
        # Button labels only. The callback data carries the approval nonce, and the
        # record has no need of it.
        labels = [
            button[0] for button in (kwargs.get("approve_callback"), kwargs.get("deny_callback"))
            if isinstance(button, tuple) and button
        ]
        try:
            receipt = self._client.send_message(chat_id, message, *args, **kwargs)
        except Exception as error:
            self._record(chat_id, None, labels, f"failed:{type(error).__name__}", message)
            raise
        self._record(chat_id, getattr(receipt, "message_id", None), labels, "sent", message)
        return receipt

    def _record(
        self, chat_id: Any, message_id: Any, labels: list[str], outcome: str, text: str
    ) -> None:
        try:
            self._db.execute(
                """INSERT INTO tc_action_outbox(sent_utc, chat_id, message_id, buttons,
                     outcome, text) VALUES(?,?,?,?,?,?)""",
                (
                    self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    chat_id if isinstance(chat_id, int) else None,
                    message_id if isinstance(message_id, int) else None,
                    " | ".join(labels), outcome[:64], scrub(str(text))[:MAX_OUTBOX_TEXT],
                ),
            )
            self._db.commit()
        except sqlite3.Error:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


# -- the command --------------------------------------------------------------------


def _owner_only(path: Path) -> None:
    """Refuse to run as anyone but the database's owner.

    A write as root would leave root-owned WAL files behind, and the service could then
    no longer open its own database. The wrapper runs this as the service user.
    """
    try:
        owner = path.stat().st_uid
    except OSError as error:
        raise SystemExit(f"cannot read {path}: {error.strerror}") from error
    if os.geteuid() != owner:
        raise SystemExit(
            f"run this as the owner of {path} (uid {owner}), e.g. through the "
            f"terracompute-console wrapper; you are uid {os.geteuid()}"
        )


def ask(config: dict[str, Any], question: str) -> int:
    question = " ".join(question.split())
    if not question:
        raise SystemExit("nothing to ask")
    if len(question) > MAX_QUESTION_CHARS:
        raise SystemExit(f"a question may be at most {MAX_QUESTION_CHARS} characters")
    inbox = Path(config["inbox_path"])
    _owner_only(inbox)
    # Negative, so it can never collide with a Telegram update id, and unique per ask.
    update_id = -time.time_ns() // 1000
    db = sqlite3.connect(inbox, timeout=10)
    try:
        db.execute("PRAGMA busy_timeout=10000")
        db.execute(
            """INSERT INTO telegram_operator_inbox(namespace, update_id, group_id,
                 sender_id, message_id, callback_id, kind, subject_id, nonce, text)
               VALUES(?,?,?,?,NULL,NULL,'question',NULL,?,?)""",
            (NAMESPACE, update_id, int(config["telegram_group_id"]), CONSOLE_SENDER,
             question, f"console:{question[:200]}"),
        )
        db.commit()
    finally:
        db.close()
    print(f"asked at {_now()} (update {update_id}); the answer goes to the group and to `log`")
    return 0


def log(config: dict[str, Any], since: str, limit: int) -> int:
    """Questions in, messages out, in the order they happened."""
    rows: list[tuple[str, str, str]] = []
    actions = sqlite3.connect(f"file:{config['actions_database']}?mode=ro", uri=True)
    try:
        try:
            for sent, buttons, outcome, text in actions.execute(
                """SELECT sent_utc, buttons, outcome, text FROM tc_action_outbox
                   WHERE sent_utc >= ? ORDER BY id DESC LIMIT ?""", (since, limit),
            ):
                tag = "bot" + (f" [{buttons}]" if buttons else "") + (
                    "" if outcome == "sent" else f" ({outcome})")
                rows.append((sent, tag, text))
        except sqlite3.OperationalError:
            pass  # No outbox yet: the service has not run with the recorder.
    finally:
        actions.close()
    inbox = sqlite3.connect(f"file:{config['inbox_path']}?mode=ro", uri=True)
    try:
        for stored, sender, kind, nonce, text in inbox.execute(
            """SELECT stored_utc, sender_id, kind, nonce, text FROM telegram_operator_inbox
               WHERE replace(stored_utc,' ','T') >= ? ORDER BY stored_utc DESC LIMIT ?""",
            (since.rstrip("Z"), limit),
        ):
            who = "console" if int(sender) == CONSOLE_SENDER else "operator"
            body = nonce if kind == "question" and nonce else text
            rows.append((str(stored).replace(" ", "T") + "Z", f"{who} {kind}", str(body)))
    finally:
        inbox.close()
    for when, tag, text in sorted(rows)[-limit:]:
        print(f"--- {when} {tag}\n{text}\n")
    return 0


def status(config: dict[str, Any]) -> int:
    db = sqlite3.connect(f"file:{config['actions_database']}?mode=ro", uri=True)
    try:
        report = {
            "controls": db.execute("SELECT name, value FROM tc_action_controls").fetchall(),
            "waiting_requests": db.execute(
                "SELECT proposal_id, stage, substr(command,1,120) FROM tc_action_cycles "
                "WHERE stage != 'done'").fetchall(),
            "conversations": db.execute(
                "SELECT root, round, updated_utc FROM tc_action_conversations").fetchall(),
            "reads_waiting": db.execute(
                "SELECT count(*) FROM tc_action_conversation_reads WHERE ran_utc IS NULL"
            ).fetchone()[0],
            "live_loops": db.execute(
                "SELECT incident_key, code, state, rounds FROM tc_action_observe_loops "
                "WHERE state IN ('open','final')").fetchall(),
        }
    except sqlite3.OperationalError as error:
        raise SystemExit(f"the actions database is not ready: {error}") from error
    finally:
        db.close()
    print(json.dumps(report, indent=1, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terracompute-console")
    parser.add_argument("--config", type=Path, required=True,
                        help="the actions service's JSON configuration")
    sub = parser.add_subparsers(dest="command", required=True)
    asking = sub.add_parser("ask", help="put a question to the agent, as the operator would")
    asking.add_argument("question", nargs="+")
    reading = sub.add_parser("log", help="read the conversation back")
    reading.add_argument("--since", default="1970-01-01T00:00:00Z")
    reading.add_argument("--limit", type=int, default=40)
    sub.add_parser("status", help="what the service is doing and waiting on")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"cannot read the configuration: {error}", file=sys.stderr)
        return 2
    if args.command == "ask":
        return ask(config, " ".join(args.question))
    if args.command == "log":
        return log(config, args.since, max(1, min(args.limit, 500)))
    return status(config)


if __name__ == "__main__":
    sys.exit(main())
