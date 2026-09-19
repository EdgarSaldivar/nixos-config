"""Carrying out what a finding asked for, through the management session.

The service already restarts one container -- dcgm-exporter -- through a dedicated
adapter, with an evidence backup, preflight and postflight checks and a human's button.
That machinery exists because restarting *that* container perturbs the very GPU state
being diagnosed, and because it is the action a blocked handover incident proposes.

The other monitoring on this machine needs none of that ceremony and had no path at
all: a finding asking to restart the node exporter was validated, found to be something
the adapter could not do, and handed to a person. The charter says managing the
monitoring we installed is the agent's own work, so this is the path for the rest of it.

What makes it safe is not the shell. The container name is validated against the
catalogue before it gets here -- a docker name, no spaces and no shell metacharacters,
and not a tenant's rental -- so the script this module builds is fixed text plus one
checked word; the model never writes a command. The session it runs in walls off tenant
data whatever the command says, and the helper writes the command down before it runs
it. A name docker does not know comes back as "No such container", which is the right
place for that answer: docker knows what exists on the machine and we do not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .diagnosis import ours
from .monitor_restart import ActorError, EvidenceStore, _base, _reason

# Long enough for a container to stop and come back on a busy host, short enough that a
# person is not waiting on it. The session's own cap is five minutes.
RESTART_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class Carried:
    """What happened when the service tried to do the thing itself."""

    action: str
    container: str
    ok: bool
    detail: str

    def document(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "container": self.container,
            "ok": self.ok,
            "detail": self.detail,
        }


class MonitoringActor:
    """Restarts monitoring we installed, through a management session.

    Deliberately not a general executor. It knows one verb, and the only thing that
    varies in the command it sends is a container name the catalogue has already
    checked -- which is what keeps a model's text out of a shell on a machine with
    other people's work on it.
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

    def restart(self, container: str, subject: str | None = None) -> Carried:
        """Restart one monitoring container. A failure is reported, never raised."""
        if not ours(container):
            # Unreachable through a parsed finding, which is the point: this is the
            # same boundary stated twice, so a future caller that skips the catalogue
            # still cannot name a tenant's container here.
            return Carried("restart-monitoring-container", container, False, "not ours to restart")
        script = f"docker restart {container}"
        request_id = self.request_id_factory()
        try:
            document = self.client.session(
                script, request_id, writable=True, timeout=self.timeout
            )
            result = self._parse(document, request_id, container)
        except (ActorError, OSError, ValueError) as error:
            result = Carried(
                "restart-monitoring-container", container, False,
                f"the session did not run: {type(error).__name__}",
            )
        self.evidence.record("action-result", subject or self.subject, dict(
            result.document(), recorded_at=self.clock().isoformat().replace("+00:00", "Z"),
        ))
        return result

    @staticmethod
    def _parse(document: object, request_id: str, container: str) -> Carried:
        doc = _base(document, "session", request_id, component="host")
        if doc.get("ok") is not True:
            return Carried(
                "restart-monitoring-container", container, False,
                f"the session refused it: {_reason(doc)}",
            )
        code = doc.get("exit_code")
        lines = doc.get("lines")
        said = " ".join(str(line) for line in lines[-3:]) if isinstance(lines, list) else ""
        if code != 0:
            # docker prints the reason on the same stream, and it is the useful half:
            # "No such container" and "permission denied" want different answers.
            return Carried(
                "restart-monitoring-container", container, False,
                f"docker exited {code}: {said[:200]}" if said else f"docker exited {code}",
            )
        return Carried("restart-monitoring-container", container, True, "restarted")
