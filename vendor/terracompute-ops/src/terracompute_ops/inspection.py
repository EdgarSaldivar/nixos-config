"""Read-only target reads for diagnosis.

The target helper answers a fixed set of read topics (``target/terracompute-act.py``).
Nothing here can change the machine: the topic names a catalogued command, no caller
value reaches a command line, and the helper redacts tenant process paths before the
answer leaves the host.

These reads are what an investigator looks at when it is working out what is wrong, so
they are recorded as evidence with the rest of the incident material.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .monitor_restart import (
    MAX_ACTOR_BYTES,
    ActorClient,
    ActorError,
    EvidenceStore,
    _base,
    _reason,
    _utc,
)

# Mirrors READ_TOPICS in the target helper; a topic the helper does not know is refused
# there, and one this side does not know is never sent.
READ_TOPICS = (
    "containers",
    "exporter-logs",
    "gpu-handles",
    "gpu-inventory",
    "gpu-processes",
    "kernel-gpu-log",
    "pci-errors",
)
MAX_LINES = 200
MAX_LINE_CHARS = 300
_PRINTABLE = re.compile(r"^[\x20-\x7e]*$")


@dataclass(frozen=True)
class TargetRead:
    topic: str
    observed_at: datetime
    hostname: str
    board: str
    boot_id: str
    lines: tuple[str, ...]
    truncated: bool
    failure: str | None

    @property
    def ok(self) -> bool:
        return self.failure is None

    def document(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "hostname": self.hostname,
            "board": self.board,
            "boot_id": self.boot_id,
            "lines": list(self.lines),
            "truncated": self.truncated,
            "failure": self.failure,
        }


def parse_read(document: object, topic: str, request_id: str) -> TargetRead:
    """Strictly parse one ``inspect`` answer. Bounded and ASCII, like every helper reply."""
    doc = _base(document, "inspect", request_id, component=topic)
    if doc.get("topic") != topic:
        raise ActorError("topic_mismatch")
    raw = doc.get("lines")
    if not isinstance(raw, list) or len(raw) > MAX_LINES:
        raise ActorError("lines_invalid")
    lines = []
    for line in raw:
        if not isinstance(line, str) or len(line) > MAX_LINE_CHARS or not _PRINTABLE.fullmatch(line):
            raise ActorError("lines_invalid")
        lines.append(line)
    truncated = doc.get("truncated")
    if not isinstance(truncated, bool):
        raise ActorError("truncated_invalid")
    hostname, board, boot_id = doc.get("hostname"), doc.get("board"), doc.get("boot_id")
    if not all(isinstance(value, str) for value in (hostname, board, boot_id)):
        raise ActorError("identity_invalid")
    return TargetRead(
        topic=topic,
        observed_at=_utc(doc.get("observed_at"), "observed_at"),
        hostname=hostname[:253],
        board=board[:128],
        boot_id=boot_id,
        lines=tuple(lines),
        truncated=truncated,
        failure=None if doc.get("ok") is True else _reason(doc),
    )


class TargetReader:
    """Runs catalogued reads against the target and keeps them as evidence."""

    def __init__(
        self,
        client: ActorClient,
        evidence: EvidenceStore,
        *,
        subject: str = "diagnosis",
        request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.client = client
        self.evidence = evidence
        self.subject = subject
        self.request_id_factory = request_id_factory
        self.clock = clock

    def read(self, topic: str, subject: str | None = None) -> TargetRead:
        if topic not in READ_TOPICS:
            raise ActorError("unknown_topic")
        request_id = self.request_id_factory()
        result = parse_read(self.client.run(f"inspect {topic}", request_id), topic, request_id)
        self.evidence.record("target-read", subject or self.subject, result.document())
        return result

    def read_all(self, subject: str | None = None) -> dict[str, TargetRead | str]:
        """Every topic, with a failed read reported rather than raised."""
        answers: dict[str, TargetRead | str] = {}
        for topic in READ_TOPICS:
            try:
                answers[topic] = self.read(topic, subject)
            except (ActorError, OSError, ValueError) as error:
                answers[topic] = f"unavailable: {type(error).__name__}"
        return answers


def summarize(answers: Mapping[str, TargetRead | str], limit: int = 40) -> str:
    """A compact text block for a model prompt or a group message."""
    blocks = []
    for topic in READ_TOPICS:
        answer = answers.get(topic)
        if isinstance(answer, str):
            blocks.append(f"## {topic}\n{answer}")
            continue
        if answer is None:
            continue
        if not answer.ok:
            blocks.append(f"## {topic}\nread failed: {answer.failure}")
            continue
        body = "\n".join(answer.lines[-limit:]) or "(no output)"
        more = " (earlier lines dropped)" if answer.truncated or len(answer.lines) > limit else ""
        blocks.append(f"## {topic}{more}\n{body}")
    return "\n\n".join(blocks)[: MAX_ACTOR_BYTES]
