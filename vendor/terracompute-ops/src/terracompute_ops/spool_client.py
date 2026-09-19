"""Ask the investigator across its spool, without waiting for the answer.

The action loop must keep answering the group, finishing executions and delivering
outcomes while a model thinks, and a model may think for ten minutes. So asking is a
write that returns at once and collecting is a read on a later pass; nothing here
blocks on the investigator.

This is the producer side of the bridge described in docs/INVESTIGATOR-RUNTIME.md. It
may create a request in the pending directory and read and consume its own answer in
the completed directory. It cannot list pending work, reach claimed work, the
quarantine, the investigator's database or its home, and it never tries.

Nothing the investigator returns is trusted here. The answer is data: it is bounded,
parsed against the action contract elsewhere, and can only ever become a finding.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .investigator_runtime import (
    REQUEST_SCHEMA_VERSION as _RUNTIME_SCHEMA_VERSION,
)

# The one the runtime accepts, imported rather than copied. Two constants
# meaning the same thing is two constants that can disagree, and this pair
# did: the sender kept writing 4 after the receiver moved to 5, so a field
# the receiver was waiting for never arrived under a version that had it.
REQUEST_SCHEMA_VERSION = _RUNTIME_SCHEMA_VERSION
MACHINE_ID = "17049"
MAX_PROMPT_BYTES = 64 * 1024
# The runtime's own bound on a result document, with room for its envelope.
MAX_RESULT_BYTES = 128 * 1024
# Owner read/write for us, group read for the runtime that must open it. The staging
# and pending directories are setgid, so the group is the bridge both services share.
REQUEST_MODE = 0o640
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INVESTIGATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:#-]{0,159}$")
_EFFORTS = frozenset({"medium", "high"})
_SEVERITIES = frozenset({"info", "warning", "error", "critical"})
_KINDS = frozenset({"diagnose", "converse"})


class SpoolUnavailable(RuntimeError):
    """The spool could not be used. Never a reason to stop the loop."""


@dataclass(frozen=True)
class Answer:
    status: str
    text: str
    reason: str | None


class SpoolInvestigator:
    """One producer's view of the investigator: ask, and later collect."""

    def __init__(self, request_spool: Path, result_spool: Path, staging: Path):
        for path in (request_spool, result_spool, staging):
            if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                raise ValueError("spool paths must be absolute and free of traversal")
        self.pending = Path(request_spool) / "pending"
        self.completed = Path(result_spool) / "completed"
        # Requests are built here and moved into pending whole, so the runtime never
        # sees a half-written file. This must sit under the same mount as pending:
        # systemd gives each ReadWritePaths entry its own bind mount, and rename(2)
        # is EXDEV across mount points even on one filesystem.
        self.staging = Path(staging)

    # -- asking -------------------------------------------------------------------

    def waiting(self, request_id: str) -> bool:
        """True while our request is still there to be claimed.

        Pending cannot be listed, only asked about by name, which is all we need.
        """
        try:
            name = f"{_checked_id(request_id)}.json"
            return stat.S_ISREG(os.stat(self.pending / name).st_mode)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise SpoolUnavailable(type(error).__name__) from error

    def ask(
        self,
        request_id: str,
        *,
        incident_id: str,
        evidence_hash: str,
        severity: str,
        prompt: str,
        kind: str = "diagnose",
        investigation_id: str = "",
        effort: str = "high",
        requested: bool = False,
    ) -> bool:
        """Publish one request. False when it was already waiting."""
        name = _checked_id(request_id)
        document = _request_document(
            request_id=name, incident_id=incident_id, evidence_hash=evidence_hash,
            severity=severity, prompt=prompt, kind=kind,
            investigation_id=investigation_id, effort=effort, requested=requested,
        )
        if self.waiting(name):
            return False
        temporary = self.staging / f".request.{uuid.uuid4().hex}.json"
        try:
            try:
                self.staging.mkdir(mode=0o700, parents=True)
            except FileExistsError:
                pass  # Provisioned by the deployment, and not ours to re-mode.
            else:
                # mkdir's mode is masked by the umask, and a staging directory we
                # cannot write to would stop every question.
                os.chmod(self.staging, 0o700)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            handle = os.open(temporary, flags, REQUEST_MODE)
            try:
                # The runtime has to read what we send it, and it is not the owner of
                # this file: the bridge group is how it gets in. A umask cannot be
                # trusted to leave that bit alone.
                os.fchmod(handle, REQUEST_MODE)
                os.write(handle, json.dumps(document, sort_keys=True).encode("ascii"))
                os.fsync(handle)
            finally:
                os.close(handle)
            os.rename(temporary, self.pending / f"{name}.json")
        except OSError as error:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise SpoolUnavailable(type(error).__name__) from error
        return True

    # -- collecting ---------------------------------------------------------------

    def collect(self, request_id: str) -> Answer | None:
        """Our answer, consumed as it is read. None while there is none."""
        name = _checked_id(request_id)
        path = self.completed / f"{name}.json"
        try:
            handle = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SpoolUnavailable(type(error).__name__) from error
        try:
            status = os.fstat(handle)
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_RESULT_BYTES:
                raise SpoolUnavailable("result-file-invalid")
            encoded = os.read(handle, MAX_RESULT_BYTES)
        except OSError as error:
            raise SpoolUnavailable(type(error).__name__) from error
        finally:
            os.close(handle)
        try:
            # Consuming it here means one answer is read once, and an answer we cannot
            # parse does not come back every pass.
            os.unlink(path)
        except OSError:
            pass
        try:
            document = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return Answer("unavailable", "", "result-unreadable")
        if not isinstance(document, dict) or document.get("request_id") != name:
            return Answer("unavailable", "", "result-mismatched")
        if document.get("machine_id") != MACHINE_ID:
            return Answer("unavailable", "", "result-target-mismatch")
        report = document.get("report")
        reason = document.get("reason")
        return Answer(
            status=str(document.get("status") or "unavailable"),
            text=report if isinstance(report, str) else "",
            reason=str(reason) if isinstance(reason, str) else None,
        )


def _checked_id(request_id: Any) -> str:
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise ValueError("request id must be short and alphanumeric")
    return request_id


def _request_document(
    *, request_id: str, incident_id: str, evidence_hash: str, severity: str, prompt: str,
    kind: str = "diagnose", investigation_id: str = "", effort: str = "high",
    requested: bool = False,
) -> Mapping[str, Any]:
    """Exactly the fields the runtime accepts, checked before anything is written."""
    if not isinstance(incident_id, str) or not _IDENTIFIER.fullmatch(incident_id):
        raise ValueError("incident id is not an identifier the investigator accepts")
    if not isinstance(evidence_hash, str) or not _SHA256.fullmatch(evidence_hash):
        raise ValueError("evidence hash must be a sha256 digest")
    if severity not in _SEVERITIES:
        raise ValueError("severity is not one the investigator accepts")
    if kind not in _KINDS:
        raise ValueError("kind is not one the investigator accepts")
    # What this turn spends against. Empty means "this question is its own
    # investigation", which is what a caller with nothing to group by wants.
    if not isinstance(investigation_id, str) or (
        investigation_id and not _INVESTIGATION.fullmatch(investigation_id)
    ):
        raise ValueError("investigation id is not an identifier the investigator accepts")
    # How hard to think. A turn that decides which reads to run does not need what a
    # turn that concludes from all of them needs, and the difference is most of the bill.
    if effort not in _EFFORTS:
        raise ValueError("effort is not one the investigator accepts")
    if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt:
        raise ValueError("prompt must be non-empty text")
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError("prompt is larger than the investigator accepts")
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "machine_id": MACHINE_ID,
        "incident_id": incident_id,
        "evidence_hash": evidence_hash,
        "severity": severity,
        "prompt": prompt,
        "kind": kind,
        "investigation_id": investigation_id,
        "effort": effort,
        # A person asked for this one by name, so the daily backstop -- which exists to
        # stop the machine looping at three in the morning -- does not apply to it.
        "requested": bool(requested),
    }
