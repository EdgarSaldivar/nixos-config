"""Carrying out what a finding asked for, through the management session.

dcgm-exporter keeps a dedicated adapter with an evidence backup, preflight and
postflight checks and a human's button, because restarting *that* container perturbs
the very GPU state being diagnosed. This is the path for everything else.

What makes it safe is no longer the shape of the command. It used to be: the module
built `docker restart <name>` from a catalogue-checked parameter, so the model never
wrote a command -- and the same property meant the agent could name five actions and
perform one, with the rest failing at execution for want of an adapter.

Now the command is what the finding said, and safety sits above it.
:func:`~terracompute_ops.authorization.classify` decides who may authorise a command,
and only a short, boring, reversible set arrives here without a person having read it.
Everything else is shown to somebody exactly as written. The session it runs in walls
off tenant data whatever the command says, and the helper writes the command down
before running it, so a command that panics the box is still attributable.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .authorization import Risk, classify
from .monitor_restart import ActorError, EvidenceStore, _base, _reason

# Long enough for a container to stop and come back on a busy host, short enough that a
# person is not waiting on it. The session's own cap is five minutes.
RESTART_TIMEOUT_SECONDS = 90.0

# These commands can tear down the SSH transport before the target helper writes its
# result. Silence after dispatch is therefore not evidence of failure: the only honest
# answer is unknown until fresh target status proves what happened.
DISCONNECTING_ACTION = re.compile(
    r"^(?:systemctl\s+(?:reboot|poweroff)|shutdown\s+-(?:r|h)\b|reboot\b|poweroff\b)"
)


@dataclass(frozen=True)
class Carried:
    """What happened when the service tried to do the thing itself."""

    action: str
    container: str
    ok: bool
    detail: str
    uncertain: bool = False

    def document(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "container": self.container,
            "ok": self.ok,
            "detail": self.detail,
            "uncertain": self.uncertain,
        }


class MonitoringActor:
    """Carries out, through a management session, what the agent may do alone.

    It IS a general executor now, deliberately. It used to know one verb with one
    catalogue-checked parameter, which kept model text out of a shell -- and also
    meant the agent could name five actions and perform one. What keeps this safe is
    no longer the shape of the command but who is allowed to authorise it:
    :func:`classify` admits only a short, boring, reversible set here, and everything
    else goes to a person who reads the literal command first.
    """

    def __init__(
        self,
        client: Any,
        evidence: EvidenceStore,
        *,
        subject: str = "acting",
        request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        timeout: float = RESTART_TIMEOUT_SECONDS,
    ):
        self.client = client
        self.evidence = evidence
        self.subject = subject
        self.request_id_factory = request_id_factory
        self.clock = clock
        self.timeout = timeout

    def run(self, command: str, subject: str | None = None, *, approved: bool = False) -> Carried:
        """Carry out one command.

        Was `restart(container)`, which built `docker restart {container}` from a
        catalogue parameter. There is no catalogue now: the command IS the action, and
        who may authorise it is :func:`classify`'s answer, not this method's.

        `approved` says a person has seen this exact command and tapped Approve. It
        widens what may run from the short unattended set to anything the classifier
        does not refuse outright -- which is the point of an approval. It never widens
        past REFUSED: no tap makes somebody else's rental ours to touch.
        """
        risk, reason = classify(command)
        allowed = {Risk.SELF, Risk.APPROVAL} if approved else {Risk.SELF}
        if risk not in allowed:
            # Unreachable through a parsed finding, which is the point: the same
            # boundary stated twice, so a caller that skips the classifier still
            # cannot get a tenant's container or an unattended reboot through here.
            return Carried(
                command, command, False,
                f"{'not something I will do at all' if risk is Risk.REFUSED else 'not mine to do alone'}: {reason}",
            )
        script = command
        request_id = self.request_id_factory()
        try:
            document = self.client.session(
                script, request_id, writable=True, timeout=self.timeout
            )
            result = self._parse(document, request_id, command)
        except ActorError as error:
            uncertain = approved and DISCONNECTING_ACTION.match(command) is not None
            result = Carried(
                command, command, False,
                (f"the target stopped answering before it reported the result "
                 f"({type(error).__name__}: {error}); the command may have run"
                 if uncertain else f"the session did not run: {type(error).__name__}: {error}"),
                uncertain=uncertain,
            )
        except (OSError, ValueError) as error:
            result = Carried(
                command, command, False,
                f"the session did not run: {type(error).__name__}: {error}",
            )
        self.evidence.record("action-result", subject or self.subject, dict(
            result.document(), recorded_at=self.clock().isoformat().replace("+00:00", "Z"),
        ))
        return result

    @staticmethod
    def _parse(document: object, request_id: str, command: str) -> Carried:
        doc = _base(document, "session", request_id, component="host")
        if doc.get("ok") is not True:
            return Carried(
                command, command, False,
                f"the session refused it: {_reason(doc)}",
            )
        code = doc.get("exit_code")
        lines = doc.get("lines")
        said = " ".join(str(line) for line in lines[-3:]) if isinstance(lines, list) else ""
        if code != 0:
            # docker prints the reason on the same stream, and it is the useful half:
            # "No such container" and "permission denied" want different answers.
            return Carried(
                command, command, False,
                f"it exited {code}: {said[:200]}" if said else f"it exited {code}",
            )
        return Carried(command, command, True, "done")
