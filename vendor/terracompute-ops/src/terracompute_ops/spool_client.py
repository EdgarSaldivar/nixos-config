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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .investigator_runtime import (
    REQUEST_SCHEMA_VERSION as _RUNTIME_SCHEMA_VERSION,
)
from .work_owner import WorkOwner, expiry_text, parse_expiry

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
# "review" asks the escalation model for an independent critique of a plan before a
# person is asked to approve it.
_KINDS = frozenset({"diagnose", "converse", "review"})


class SpoolUnavailable(RuntimeError):
    """The spool could not be used. Never a reason to stop the loop."""


@dataclass(frozen=True)
class Answer:
    status: str
    text: str
    reason: str | None


@dataclass(frozen=True)
class Progress:
    """Runtime-observed phase. A deadline alone never asserts a live call."""
    phase: str
    live: bool
    lease_until: str | None = None


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
            status = os.stat(self.pending / name, follow_symlinks=False)
            return stat.S_ISREG(status.st_mode) and status.st_nlink == 1
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
        owner: WorkOwner | None = None,
        expires_at: datetime | None = None,
    ) -> bool:
        """Publish one request. False when it was already waiting."""
        name = _checked_id(request_id)
        document = _request_document(
            request_id=name, incident_id=incident_id, evidence_hash=evidence_hash,
            severity=severity, prompt=prompt, kind=kind,
            investigation_id=investigation_id, effort=effort, requested=requested,
        )
        if owner is not None or expires_at is not None:
            if (owner is None or expires_at is None or owner.request_id != name
                or owner.evidence_generation != evidence_hash
                or owner.incident_id is not None and owner.incident_id != incident_id):
                raise ValueError("owned request needs matching owner and expiry")
            document = dict(document, schema_version=REQUEST_SCHEMA_VERSION,
                            owner=owner.document(), expires_at=expiry_text(
                                expires_at, now=datetime.now(timezone.utc)))
        else:
            # Existing producers keep publishing the mixed-version read-only protocol.
            document = dict(document, schema_version=5)
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

    def peek(self, request_id: str, *, owner: WorkOwner | None = None,
             expires_at: datetime | None = None) -> Answer | None:
        """Read without deleting; persist acceptance before ``acknowledge``.

        Owned results require the caller's complete owner and matching request,
        machine, incident and evidence. Legacy callers can still read legacy results.
        """
        name = _checked_id(request_id)
        path = self.completed / f"{name}.json"
        try:
            handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SpoolUnavailable(type(error).__name__) from error
        try:
            status = os.fstat(handle)
            if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_size > MAX_RESULT_BYTES):
                raise SpoolUnavailable("result-file-invalid")
            encoded = os.read(handle, MAX_RESULT_BYTES)
        except OSError as error:
            raise SpoolUnavailable(type(error).__name__) from error
        finally:
            os.close(handle)
        try:
            document = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return Answer("unavailable", "", "result-unreadable")
        if not isinstance(document, dict) or document.get("request_id") != name:
            return Answer("unavailable", "", "result-mismatched")
        if document.get("machine_id") != MACHINE_ID:
            return Answer("unavailable", "", "result-target-mismatch")
        if owner is not None:
            if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
                raise ValueError("owned result needs aware expiry")
            if owner.request_id != name or document.get("schema_version") != 2:
                return Answer("unavailable", "", "result-owner-mismatch")
            try:
                actual = WorkOwner.parse(document.get("owner"))
            except (TypeError, ValueError):
                return Answer("unavailable", "", "result-owner-mismatch")
            if (actual != owner or document.get("incident_id") != owner.incident_id
                and owner.incident_id is not None
                or document.get("evidence_hash") != owner.evidence_generation):
                return Answer("unavailable", "", "result-owner-mismatch")
            cancellation = self._read_metadata(f"cancel-{name}.json")
            if cancellation is not None:
                if (set(cancellation) != {"schema_version", "owner"}
                    or cancellation.get("schema_version") != 1
                    or cancellation.get("owner") != owner.document()):
                    return Answer("unavailable", "", "cancellation-invalid")
                return Answer("rejected", "", "owner-cancelled")
            if datetime.now(timezone.utc) >= expires_at:
                return Answer("rejected", "", "owner-expired")
        elif document.get("schema_version") == 2:
            return Answer("unavailable", "", "result-owner-required")
        report = document.get("report")
        reason = document.get("reason")
        return Answer(
            status=str(document.get("status") or "unavailable"),
            text=report if isinstance(report, str) else "",
            reason=str(reason) if isinstance(reason, str) else None,
        )

    def acknowledge(self, request_id: str, *, owner: WorkOwner | None = None,
                    expires_at: datetime | None = None) -> bool:
        """Delete only a result already read and durably handled by the caller."""
        answer = self.peek(request_id, owner=owner, expires_at=expires_at)
        if answer is None:
            return False
        if answer.reason in {"result-owner-mismatch", "result-owner-required",
                             "result-target-mismatch", "result-mismatched",
                             "cancellation-invalid"}:
            raise SpoolUnavailable(answer.reason)
        self._safe_unlink(self.completed / f"{_checked_id(request_id)}.json")
        if owner is not None:
            self._safe_unlink(self.completed / f"progress-{request_id}.json", missing_ok=True)
            self._safe_unlink(self.completed / f"cancel-{request_id}.json", missing_ok=True)
        return True

    def collect(self, request_id: str) -> Answer | None:
        """Legacy consume-on-read API; owned results require peek/acknowledge."""
        answer = self.peek(request_id)
        if answer is not None and answer.reason != "result-owner-required":
            if answer.reason in {"result-owner-mismatch", "result-target-mismatch",
                                 "result-mismatched", "result-unreadable"}:
                self.discard(request_id)
            else:
                self.acknowledge(request_id)
        return answer

    def discard(self, request_id: str, *, owner: WorkOwner | None = None) -> None:
        """After durably recording a rejected result, clear its named spool slot."""
        self._safe_unlink(self.completed / f"{_checked_id(request_id)}.json")
        if owner is not None:
            if owner.request_id != request_id:
                raise ValueError("owner/request mismatch")
            self._safe_unlink(self.completed / f"progress-{request_id}.json", missing_ok=True)
            # An untrusted output is not proof the runtime consumed cancellation.
            # Keep producer intent until a matching terminal result is acknowledged.

    @staticmethod
    def _safe_unlink(path: Path, *, missing_ok: bool = False) -> None:
        try:
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise SpoolUnavailable("spool-file-invalid")
            path.unlink()
        except FileNotFoundError:
            if not missing_ok:
                raise SpoolUnavailable("spool-file-missing")
        except OSError as error:
            raise SpoolUnavailable(type(error).__name__) from error

    def request_cancellation(self, owner: WorkOwner) -> None:
        """Publish producer intent in completed; no claimed/private access needed."""
        document = {"schema_version": 1, "owner": owner.document()}
        self._write_metadata(f"cancel-{owner.request_id}.json", document)

    def progress(self, owner: WorkOwner) -> Progress:
        """Queued, claimed, dispatched, obsolete or finished; lease is runtime written."""
        document = self._read_metadata(f"progress-{owner.request_id}.json", runtime_owned=True)
        if document is None:
            return Progress("queued" if self.waiting(owner.request_id) else "unknown", False)
        if document.get("owner") != owner.document() or document.get("schema_version") != 1:
            raise SpoolUnavailable("progress-owner-mismatch")
        phase = document.get("phase")
        if phase not in {"claimed", "dispatching", "dispatched", "obsolete", "finished"}:
            raise SpoolUnavailable("progress-invalid")
        lease = document.get("lease_until")
        try:
            live = (phase == "dispatched" and isinstance(lease, str)
                    and parse_expiry(lease) > datetime.now(timezone.utc))
        except ValueError as error:
            raise SpoolUnavailable("progress-invalid") from error
        return Progress(phase, live, lease if isinstance(lease, str) else None)

    def _read_metadata(self, name: str, *, runtime_owned: bool = False) -> dict[str, Any] | None:
        try:
            fd = os.open(self.completed / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as error:
            raise SpoolUnavailable(type(error).__name__) from error
        try:
            status = os.fstat(fd)
            if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_size > 2048 or stat.S_IMODE(status.st_mode) != 0o640
                or runtime_owned and status.st_uid == os.geteuid()):
                raise SpoolUnavailable("metadata-file-invalid")
            raw = os.read(fd, 2049)
        finally:
            os.close(fd)
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SpoolUnavailable("metadata-invalid") from None
        if not isinstance(document, dict):
            raise SpoolUnavailable("metadata-invalid")
        return document

    def _write_metadata(self, name: str, document: Mapping[str, Any]) -> None:
        raw = json.dumps(document, sort_keys=True).encode("ascii")
        if len(raw) > 2048:
            raise ValueError("metadata exceeds bound")
        temp = self.completed / f".{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o640)
            try:
                os.fchmod(fd, 0o640)
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temp, self.completed / name)
        except OSError as error:
            temp.unlink(missing_ok=True)
            raise SpoolUnavailable(type(error).__name__) from error


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
        "schema_version": 5,
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
