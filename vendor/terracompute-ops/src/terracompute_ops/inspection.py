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
from dataclasses import dataclass, replace
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
from .secrets_scrub import scrub

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
    # The catalogued reads include exporter logs, which print what the exporter was
    # started with. Scrubbed here for the same reason the model's own reads are.
    lines = scrub("\n".join(lines)).split("\n") if lines else lines
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


def answered(answers: Mapping[str, TargetRead | str]) -> tuple[str, ...]:
    """Which topics came back with something, in a fixed order.

    Availability is evidence in its own right and changes rarely, unlike the content
    of a log, so it can tell a question apart without making every reading a new one.
    """
    return tuple(
        topic for topic in READ_TOPICS
        if isinstance(answers.get(topic), TargetRead) and answers[topic].ok
    )


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


# What one model-authored read may take up on its way back. The helper already bounds
# its own output; this is the controller refusing to be surprised by it.
MAX_OBSERVE_LINES = 200
OBSERVE_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class Observed:
    """One command the model asked for, and what the host said back."""

    command: str
    lines: tuple[str, ...]
    truncated: bool
    exit_code: int | None
    failure: str | None

    @property
    def ok(self) -> bool:
        return self.failure is None

    def text(self) -> str:
        """What the model is shown. A failure is a result, never an empty success."""
        if self.failure is not None:
            return f"(this read did not run: {self.failure})"
        body = "\n".join(self.lines)
        if self.truncated:
            body = "(earlier lines dropped by the host)\n" + body
        if not body:
            body = "(no output)"
        # First, not last. The host reports ok for any command that ran, whatever it
        # exited with, so this line is the only thing distinguishing a failed command
        # from a clean one -- and everything downstream keeps the tail of a long read,
        # which is exactly where a line appended at the end goes missing.
        if self.exit_code:
            body = f"(exit status {self.exit_code})\n{body}"
        return body

    def document(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "lines": list(self.lines),
            "truncated": self.truncated,
            "exit_code": self.exit_code,
            "failure": self.failure,
        }


def parse_session(document: object, request_id: str) -> Observed:
    """Strictly parse one ``observe`` answer, exactly as a catalogued read is parsed.

    The MCP server's runner checked only ``ok``; anything malformed, or an answer from
    another host, would have gone into a transcript that drives a real diagnosis.
    """
    doc = _base(document, "observe", request_id, component="host")
    raw = doc.get("lines")
    if not isinstance(raw, list) or len(raw) > MAX_OBSERVE_LINES:
        raise ActorError("lines_invalid")
    lines = []
    for line in raw:
        if not isinstance(line, str) or len(line) > MAX_LINE_CHARS or not _PRINTABLE.fullmatch(line):
            raise ActorError("lines_invalid")
        lines.append(line)
    # Scrubbed here, where the host's words first become ours, so no store, prompt or
    # message downstream ever holds a secret a read happened to print.
    lines = scrub("\n".join(lines)).split("\n") if lines else lines
    truncated = doc.get("truncated")
    if not isinstance(truncated, bool):
        raise ActorError("truncated_invalid")
    exit_code = doc.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise ActorError("exit_code_invalid")
    return Observed(
        command="",
        lines=tuple(lines),
        truncated=truncated,
        exit_code=exit_code,
        failure=None if doc.get("ok") is True else _reason(doc),
    )


class TargetObserver:
    """Runs the reads a model asked for, under the profile that cannot write.

    Deliberately separate from :class:`TargetReader`. That one runs a fixed vocabulary
    of topics the helper knows by name; this one carries text the model wrote, and the
    only thing standing between that text and the machine is the kernel-enforced
    read-only profile it runs under. Keeping them apart keeps that difference visible.
    """

    def __init__(
        self,
        client: Any,
        evidence: EvidenceStore,
        *,
        subject: str = "diagnosis",
        request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        timeout: float = OBSERVE_TIMEOUT_SECONDS,
    ):
        self.client = client
        self.evidence = evidence
        self.subject = subject
        self.request_id_factory = request_id_factory
        self.timeout = timeout

    def observe(self, command: str, subject: str | None = None) -> Observed:
        """Run one read and keep it as evidence. A failure comes back, never raised."""
        request_id = self.request_id_factory()
        try:
            document = self.client.session(
                command, request_id, writable=False, timeout=self.timeout
            )
            result = replace(parse_session(document, request_id), command=command)
        except (ActorError, OSError, ValueError) as error:
            # Name the reason, not just the class. These are fixed tokens -- a timeout,
            # a malformed reply, a refused session -- and which one it was is evidence:
            # a sysfs read that HANGS on the device under suspicion says something
            # about that device, and "observe ActorError" said nothing at all.
            detail = str(error)[:60] if str(error) else type(error).__name__
            result = Observed(command, (), False, None, f"{type(error).__name__}: {detail}")
        # Recorded before the caller is given it, so nothing the model saw is missing
        # from the record. A read that runs twice is recorded twice, which is honest.
        #
        # A failure here must not escape: the caller only persists the output once this
        # returns, so an error would leave the read queued and the same command would
        # run on the host again on the next pass, and the one after that. An oversized
        # document is deterministic, so that is not a retry -- it is the same command
        # every tick until the deadline.
        try:
            self.evidence.record("target-observe", subject or self.subject, result.document())
        except Exception as error:
            try:
                self.evidence.record("target-observe", subject or self.subject, {
                    "command": command[:512],
                    "lines": [],
                    "truncated": True,
                    "exit_code": result.exit_code,
                    "failure": f"output not kept: {type(error).__name__}",
                })
            except Exception:
                pass  # The read still happened; losing its copy must not repeat it.
        return result
