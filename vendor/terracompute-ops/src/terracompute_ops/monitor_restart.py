"""Approval-gated restart of one GPU monitoring container on the target.

This is rung 1 of the blocked-GPU-handover remediation ladder (see
docs/GPU-VFIO-HANDOVER.md and docs/MONITOR-RESTART-ACTION.md). The module has no
generic command surface: the target helper accepts three fixed operations for one
fixed container, and every value sent to it is a constant or a validated UUID.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .actions import (
    DispatchResult,
    DispatchStatus,
    MembershipDecision,
    PostActionEvidence,
)
from .incidents import stable_signature
from .policy import (
    MACHINE_ID,
    ActionClass,
    ActionProposal,
    PreActionEvidence,
    SourceBinding,
    SourceState,
)

COMPONENT = "dcgm-exporter"
TARGET_HOSTNAME = "terracompute"
TARGET_BOARD = "ROME2D32GM-2T"
MAX_ACTOR_BYTES = 64 * 1024
STATUS_TIMEOUT_SECONDS = 60.0
RESTART_TIMEOUT_SECONDS = 120.0
CLEANUP_GRACE_SECONDS = 2.0
# A session's command rides stdin, never the SSH command line. Bounded here as well as
# on the target, because the target refusing an oversized payload is a wasted round
# trip and a bad shape should not leave this process at all.
MAX_SESSION_SCRIPT_BYTES = 64 * 1024
# The target caps a session at 300s of wall clock plus its own grace; allow a little
# more than that here so the target's own timeout, which reports cleanly, wins the race
# against this blunt one, which does not.
SESSION_TIMEOUT_SECONDS = 330.0
AFFECTED_DOMAIN = "monitoring"
CONTAINER_RESOURCE = f"container:{COMPONENT}"
PROPOSAL_ID_PREFIX = "mr-"
_UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")
_BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
_TENANT = re.compile(r"^C\.[0-9]{1,20}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID = _SHA256
MAX_TENANTS = 256
_SSH_TARGET = re.compile(r"^[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.:-]*$")
_PROPOSAL_ID = re.compile(r"^mr-[0-9a-f]{12}$")
_READ_TOPIC = re.compile(r"^[a-z][a-z-]{1,30}$")
# The restart command ran, but its effect is not in the record: docker may still have
# restarted the container after the CLI timed out or failed, or the after-status was
# unreadable. Reconciliation settles these from a fresh status.
UNCERTAIN_RESTART_REASONS = frozenset({
    "restart_timeout",
    "restart_output_limit",
    "restart_failed",
    "status_after_unavailable",
})
# Envelope failures that can follow a restart; they prove nothing either way.
UNCERTAIN_ENVELOPE_REASONS = frozenset({"ledger_write_failed", "internal_error"})
# Refusals made before any ledger claim, so no restart can have run.
UNCLAIMED_REFUSAL_REASONS = frozenset({"actor_busy", "ledger_unavailable"})
# Docker gets this long after the helper finished to complete a restart it accepted.
RESTART_SETTLE = timedelta(minutes=2)
HANDOVER_CLEARED = "handover cleared"
STOP_CONDITION = (
    "Stop if dcgm-exporter is not running with a new start time, or if any tenant "
    "container other than a VM rental present before the restart disappears or "
    "changes. Never retry automatically; a persisting handover needs its own proposal."
)


class ActorError(RuntimeError):
    """A bounded, secret-free failure of the target helper channel."""


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise ActorError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ActorError(f"{field}_invalid") from error
    return parsed.astimezone(timezone.utc)


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ActorError(f"{field}_invalid")
    return value


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 64:
        raise ActorError(f"{field}_invalid")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


@dataclass(frozen=True)
class ContainerState:
    present: bool
    running: bool
    started_at: str | None


@dataclass(frozen=True)
class TenantMember:
    name: str
    container_id: str
    started_at: str


@dataclass(frozen=True)
class Tenants:
    count: int
    digest: str
    names: tuple[str, ...]
    members: tuple[TenantMember, ...] = ()


@dataclass(frozen=True)
class ActorStatus:
    request_id: str
    observed_at: datetime
    hostname: str
    board: str
    boot_id: str
    container: ContainerState
    handover_blocked: tuple[str, ...]
    nvidia_visible_count: int
    pci_gpu_count: int
    tenants: Tenants
    vm_containers: tuple[str, ...]

    @property
    def identity_verified(self) -> bool:
        return self.hostname == TARGET_HOSTNAME and self.board == TARGET_BOARD


def _base(
    document: object, operation: str, request_id: str, component: str = COMPONENT
) -> Mapping[str, Any]:
    if not isinstance(document, dict):
        raise ActorError("document_invalid")
    if (
        document.get("schema_version") != 1
        or document.get("operation") != operation
        or document.get("id") != request_id
        or document.get("component") != component
        or document.get("machine_id") != MACHINE_ID
    ):
        raise ActorError("document_identity_invalid")
    return document


def _container(value: object) -> ContainerState:
    if not isinstance(value, dict):
        raise ActorError("container_invalid")
    started = value.get("started_at")
    if started is not None:
        _utc(started, "container_started_at")
    return ContainerState(
        _bool(value.get("present"), "container_present"),
        _bool(value.get("running"), "container_running"),
        started,
    )


def _tenants(value: object) -> Tenants:
    if not isinstance(value, dict):
        raise ActorError("tenants_invalid")
    names = value.get("names")
    digest = value.get("digest")
    raw_members = value.get("members")
    if (
        not isinstance(names, list)
        or len(names) > MAX_TENANTS
        or not all(isinstance(name, str) and _TENANT.fullmatch(name) for name in names)
        or names != sorted(names)
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
        or not isinstance(raw_members, list)
        or len(raw_members) != len(names)
    ):
        raise ActorError("tenants_invalid")
    members = []
    for member in raw_members:
        if (
            not isinstance(member, dict)
            or set(member) != {"name", "id", "started_at"}
            or not isinstance(member["name"], str)
            or not _TENANT.fullmatch(member["name"])
            or not isinstance(member["id"], str)
            or not _CONTAINER_ID.fullmatch(member["id"])
        ):
            raise ActorError("tenants_invalid")
        _utc(member["started_at"], "tenant_started_at")
        members.append(TenantMember(member["name"], member["id"], member["started_at"]))
    if [member.name for member in members] != names:
        raise ActorError("tenants_invalid")
    count = value.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(names):
        raise ActorError("tenants_invalid")
    return Tenants(count, digest, tuple(names), tuple(members))


def parse_status(document: object, request_id: str) -> ActorStatus:
    """Strictly parse the helper's ``status`` document for ``request_id``."""
    doc = _base(document, "status", request_id)
    if doc.get("ok") is not True:
        raise ActorError("status_not_ok")
    boot_id = doc.get("boot_id")
    if not isinstance(boot_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", boot_id):
        raise ActorError("boot_id_invalid")
    hostname = doc.get("hostname")
    board = doc.get("board")
    if not isinstance(hostname, str) or not isinstance(board, str):
        raise ActorError("identity_invalid")
    return ActorStatus(
        request_id=request_id,
        observed_at=_utc(doc.get("observed_at"), "observed_at"),
        hostname=hostname[:253],
        board=board[:128],
        boot_id=boot_id,
        container=_container(doc.get("container")),
        handover_blocked=_names(doc.get("handover_blocked"), _BDF, "handover_blocked"),
        nvidia_visible_count=_count(doc.get("nvidia_visible_count"), "nvidia_visible_count"),
        pci_gpu_count=_count(doc.get("pci_gpu_count"), "pci_gpu_count"),
        tenants=_tenants(doc.get("tenants")),
        vm_containers=_names(doc.get("vm_containers"), _TENANT, "vm_containers"),
    )


def _names(value: object, pattern: re.Pattern[str], field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_TENANTS
        or not all(isinstance(item, str) and pattern.fullmatch(item) for item in value)
        or value != sorted(value)
    ):
        raise ActorError(f"{field}_invalid")
    return tuple(value)


def host_revision(status: ActorStatus) -> str:
    return hashlib.sha256(
        _canonical({"boot_id": status.boot_id, "hostname": status.hostname, "board": status.board})
    ).hexdigest()


def fault_revision(status: ActorStatus, bdf: str) -> str:
    """What could change the ANSWER, as against what the approver saw.

    These are two different jobs that want opposite things, and they shared one hash.
    :func:`evidence_revision` binds everything a person was shown, so that a proposal
    stops matching the moment anything moves -- deliberately strict, and load-bearing.
    Question identity wants the opposite: it should move only when the answer might.

    Sharing the strict one dragged the question along with it. Every rental starting or
    stopping moves ``tenants.digest``, which moved the revision, which made it a new
    question, so the investigator ran again from the top and reached the same
    conclusion -- because somebody renting a GPU elsewhere on the box has nothing to do
    with whether this one can be handed to its VM. On a marketplace host that is
    constant, and it is most of what the token budget was being spent on.

    So this one holds the fault: the GPU, whether its handover is blocked, what the
    driver can see, whether our own component is up, and the boot id -- because a
    reboot changes every answer there is. It leaves out who happens to be renting,
    and it leaves out ``started_at``, which moves every few seconds while a container
    is in a crash loop and says nothing the present/running pair does not.
    """
    return hashlib.sha256(
        _canonical(
            {
                "bdf": bdf,
                "blocked": bdf in status.handover_blocked,
                "boot_id": status.boot_id,
                "component": [status.container.present, status.container.running],
                "gpus": [status.nvidia_visible_count, status.pci_gpu_count],
                "vm_containers": list(status.vm_containers),
            }
        )
    ).hexdigest()


def evidence_revision(status: ActorStatus, bdf: str) -> str:
    """Bind every fact the approver saw; any change requires a new proposal."""
    return hashlib.sha256(
        _canonical(
            {
                "action": ActionClass.MONITOR_COMPONENT_RESTART.value,
                "bdf": bdf,
                "blocked": bdf in status.handover_blocked,
                "boot_id": status.boot_id,
                "board": status.board,
                "container": [
                    status.container.present,
                    status.container.running,
                    status.container.started_at,
                ],
                "gpus": [status.nvidia_visible_count, status.pci_gpu_count],
                "hostname": status.hostname,
                "tenants": status.tenants.digest,
                "vm_containers": list(status.vm_containers),
            }
        )
    ).hexdigest()


def handover_incident_signature(bdf: str) -> str:
    """The stable signature of the probe's blocked-handover incident for ``bdf``."""
    return stable_signature(
        {
            "fault_family": "gpu",
            "code": "gpu_vfio_handover_blocked",
            "evidence": {"pci_bdf": bdf},
        }
    )


def new_proposal_id() -> str:
    # Short enough that "approve:<id>:<nonce>" fits Telegram's 64-byte callback data.
    return PROPOSAL_ID_PREFIX + secrets.token_hex(6)


def build_proposal(
    status: ActorStatus,
    bdf: str,
    *,
    policy_revision: str,
    clock: Callable[[], datetime],
    proposal_id: str | None = None,
) -> ActionProposal:
    """Propose the restart only for a verified target whose handover is blocked."""
    if not _BDF.fullmatch(bdf) or bdf not in status.handover_blocked:
        raise ActorError("handover_not_blocked")
    if not status.identity_verified:
        raise ActorError("target_identity_mismatch")
    if not status.container.present or not status.container.running:
        raise ActorError("component_not_running")
    return ActionProposal.create(
        proposal_id=proposal_id or new_proposal_id(),
        action_class=ActionClass.MONITOR_COMPONENT_RESTART,
        parameters={"component": COMPONENT},
        resource_ids=(CONTAINER_RESOURCE, f"gpu:{bdf}"),
        rental_impacts=(),
        affected_domains=(AFFECTED_DOMAIN,),
        source_bindings=(
            SourceBinding("host", host_revision(status)),
            SourceBinding("rentals", status.tenants.digest),
        ),
        evidence_revision=evidence_revision(status, bdf),
        policy_revision=policy_revision,
        stop_condition=STOP_CONDITION,
        clock=clock,
    )


# When a proposal was made is not part of what it asserts.
_TIMESTAMP_FIELDS = ("created_at", "expires_at")


def proposal_shape(proposal: ActionProposal) -> str:
    """Digest everything a proposal asserts except its timestamps.

    A request can wait for a human indefinitely, while the proposal the broker executes
    is built fresh when the answer arrives. Equal shapes mean the later proposal says
    exactly what the person was shown: same action, target, evidence and stop condition.
    """
    document = {
        key: value
        for key, value in proposal.exact_document().items()
        if key not in _TIMESTAMP_FIELDS
    }
    return hashlib.sha256(_canonical(document)).hexdigest()


def proposal_bdf(proposal: ActionProposal) -> str:
    gpus = [item for item in proposal.resource_ids if item.startswith("gpu:")]
    if len(gpus) != 1 or not _BDF.fullmatch(gpus[0][4:]):
        raise ActorError("proposal_gpu_invalid")
    return gpus[0][4:]


class ActorClient(Protocol):
    def run(self, operation: str, request_id: str) -> dict[str, Any]: ...


class SSHActorClient:
    """Reach the forced-command helper with a pinned host key and bounded I/O."""

    def __init__(
        self,
        *,
        ssh_binary: str,
        target: str,
        identity_file: Path,
        known_hosts_file: Path,
    ):
        if not Path(ssh_binary).is_absolute():
            raise ValueError("ssh binary must be an absolute path")
        if not _SSH_TARGET.fullmatch(target):
            raise ValueError("actor target must be a plain user@host value")
        for path in (identity_file, known_hosts_file):
            if not Path(path).is_absolute():
                raise ValueError("actor credential paths must be absolute")
        self.ssh_binary = ssh_binary
        self.target = target
        self.identity_file = Path(identity_file)
        self.known_hosts_file = Path(known_hosts_file)

    def run(self, operation: str, request_id: str) -> dict[str, Any]:
        # ``inspect <topic>`` names a read topic; the others name this component.
        verb, _, topic = operation.partition(" ")
        if not _UUID.fullmatch(request_id):
            raise ValueError("unsupported actor operation")
        if verb == "inspect":
            if not _READ_TOPIC.fullmatch(topic):
                raise ValueError("unsupported actor operation")
            component = topic
        elif verb in {"status", "restart", "result"} and not topic:
            component = COMPONENT
        else:
            raise ValueError("unsupported actor operation")
        timeout = RESTART_TIMEOUT_SECONDS if verb == "restart" else STATUS_TIMEOUT_SECONDS
        argv = [
            self.ssh_binary, "-F", "/dev/null",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_file}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"IdentityFile={self.identity_file}",
            "-o", "ConnectTimeout=15", "-o", "ClearAllForwardings=yes",
            "-o", "ForwardAgent=no", "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no",
            self.target,
            # The forced command reads only SSH_ORIGINAL_COMMAND; both parts are fixed
            # or validated above, so nothing here can be interpreted by a shell.
            f"{verb} {component} {request_id}",
        ]
        return _run_bounded_json(argv, timeout)

    def session(
        self, script: str, request_id: str, *, writable: bool = False,
        timeout: float = SESSION_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Run one agent-authored command on the target, piping it on stdin.

        The SSH command line stays three fixed tokens -- ``observe host <uuid>`` or
        ``session host <uuid>``, none of them derived from the script -- so nothing the
        agent wrote is ever parsed by a shell here or interpreted by sshd. The script is
        bytes on stdin, which the target reads whole before it runs anything.

        ``writable`` picks the profile the target runs it under: an observation cannot
        write, a management session can. A write is the caller's decision to make, and
        the caller makes it having already gone through approval; this method does not
        police that, but it does make the two visibly different requests.
        """
        if not _UUID.fullmatch(request_id):
            raise ValueError("unsupported actor operation")
        if not isinstance(script, str) or not script.strip():
            raise ValueError("empty session script")
        payload = script.encode("utf-8")
        if len(payload) > MAX_SESSION_SCRIPT_BYTES or b"\x00" in payload:
            raise ValueError("session script exceeds bound or is not text")
        verb = "session" if writable else "observe"
        argv = [
            self.ssh_binary, "-F", "/dev/null",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts_file}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"IdentityFile={self.identity_file}",
            "-o", "ConnectTimeout=15", "-o", "ClearAllForwardings=yes",
            "-o", "ForwardAgent=no", "-o", "PermitLocalCommand=no", "-o", "RequestTTY=no",
            self.target,
            # Fixed and validated; the command the agent wrote is on stdin, not here.
            f"{verb} host {request_id}",
        ]
        # A caller may wait less than the target will run. It stops waiting; the host
        # stops on its own at its own cap. A diagnostic read that has not finished in a
        # minute is not worth a service that answers nobody for five.
        return _run_bounded_json(
            argv, min(float(timeout), SESSION_TIMEOUT_SECONDS), stdin_bytes=payload
        )


def _run_bounded_json(
    argv: list[str], timeout: float, stdin_bytes: bytes | None = None
) -> dict[str, Any]:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    # Feed stdin inside the same loop as draining stdout, so a target that starts
    # replying before it has read the whole script cannot deadlock against us.
    pending = memoryview(stdin_bytes) if stdin_bytes is not None else None
    if pending is not None and process.stdin is not None:
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
    stdout_open = True
    try:
        while stdout_open:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ActorError("actor_timeout")
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is None:
                continue
            for key, _mask in events:
                if key.fileobj is process.stdout:
                    chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    if not chunk:
                        stdout_open = False  # EOF: the target has finished replying.
                        continue
                    output.extend(chunk)
                    if len(output) > MAX_ACTOR_BYTES:
                        raise ActorError("actor_output_limit")
                elif pending is not None and key.fileobj is process.stdin:
                    try:
                        written = os.write(process.stdin.fileno(), pending[:65536])
                        pending = pending[written:]
                    except BrokenPipeError:
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(process.stdin)
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise ActorError("actor_timeout") from error
    finally:
        selector.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=CLEANUP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        process.stdout.close()
    try:
        document = json.loads(bytes(output).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ActorError("actor_output_invalid") from error
    if not isinstance(document, dict):
        raise ActorError("actor_output_invalid")
    return document


class EvidenceStore:
    """Preserve action evidence in the backed-up controller state database."""

    def __init__(self, connection: sqlite3.Connection, clock: Callable[[], datetime]):
        self.db = connection
        self.clock = clock
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS tc_action_evidence (
                 ref TEXT PRIMARY KEY,
                 kind TEXT NOT NULL,
                 subject TEXT NOT NULL,
                 recorded_utc TEXT NOT NULL,
                 document_json BLOB NOT NULL
               )"""
        )
        self.db.commit()

    def record(self, kind: str, subject: str, document: Mapping[str, Any]) -> str:
        if kind not in {"proposal-status", "preflight-status", "restart-result",
                        "postflight-status", "target-read", "target-observe", "diagnosis",
                        "action-result"}:
            raise ValueError("unsupported evidence kind")
        body = _canonical(dict(document))
        if len(body) > MAX_ACTOR_BYTES * 4:
            raise ValueError("evidence document is too large")
        ref = self._ref(kind, subject, body)
        recorded = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            self.db.execute(
                """INSERT OR IGNORE INTO tc_action_evidence(ref,kind,subject,recorded_utc,document_json)
                   VALUES(?,?,?,?,?)""",
                (ref, kind, subject[:128], recorded, body),
            )
            self.db.commit()
        except BaseException:
            # A busy shared database must not leave this connection on a stale snapshot.
            self.db.rollback()
            raise
        return ref

    @staticmethod
    def _ref(kind: str, subject: str, body: bytes) -> str:
        return "evidence:" + hashlib.sha256(kind.encode() + b"\0" + subject.encode() + b"\0" + body).hexdigest()

    def verified(self, ref: str, kind: str, subject: str) -> dict[str, Any] | None:
        """Return the document stored under ``ref`` only if its content still hashes to it.

        Other controller roles can write the shared database, so a reference kept in the
        private action database is the only trusted pointer to evidence here.
        """
        row = self.db.execute(
            "SELECT kind, subject, document_json FROM tc_action_evidence WHERE ref=?", (ref,)
        ).fetchone()
        if row is None or row[0] != kind or row[1] != subject[:128]:
            return None
        body = bytes(row[2]) if isinstance(row[2], (bytes, memoryview)) else str(row[2]).encode()
        if self._ref(kind, subject, body) != ref:
            return None
        document = json.loads(body.decode("utf-8"))
        return document if isinstance(document, dict) else None

    def has(self, ref: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM tc_action_evidence WHERE ref=?", (ref,)
        ).fetchone() is not None


class TelegramMembershipVerifier:
    """Independently re-query group membership at approval and execution time."""

    def __init__(self, client: Any, clock: Callable[[], datetime]):
        self.client = client
        self.clock = clock

    def verify(self, group_id: int, user_id: int) -> MembershipDecision:
        from .telegram import current_human_member

        member = self.client.get_chat_member(group_id, user_id)
        current = bool(current_human_member(member, user_id))
        user = member.get("user") if isinstance(member, dict) else None
        human = isinstance(user, dict) and user.get("is_bot") is False
        return MembershipDecision(
            group_id=group_id,
            user_id=user_id,
            current_member=current,
            human=human,
            independently_verified=True,
            verified_at=self.clock().astimezone(timezone.utc),
        )


class MonitorRestartAdapter:
    """The fixed ActionAdapter for ``monitor-component-restart`` of dcgm-exporter.

    ``backup_ref`` returns the reference of a successful backup that already
    preserves the proposal-time evidence for this proposal, or None.
    """

    def __init__(
        self,
        client: ActorClient,
        evidence: EvidenceStore,
        *,
        backup_ref: Callable[[ActionProposal], str | None],
        preflight_ref: Callable[[ActionProposal], str | None] = lambda proposal: None,
        request_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.client = client
        self.evidence = evidence
        self.backup_ref = backup_ref
        # The broker's private record of the evidence reference taken before dispatch.
        self.preflight_ref = preflight_ref
        self.request_id_factory = request_id_factory
        # Source age is measured on the controller's clock at receipt, so skew between
        # the target and Imladris cannot make fresh evidence look stale or future.
        self.clock = clock
        self._before: dict[str, ActorStatus] = {}

    def supports(self, action_class: ActionClass) -> bool:
        return action_class is ActionClass.MONITOR_COMPONENT_RESTART

    def validate(self, proposal: ActionProposal) -> None:
        if (
            proposal.action_class is not ActionClass.MONITOR_COMPONENT_RESTART
            or dict(proposal.parameters) != {"component": COMPONENT}
            or proposal.rental_impacts
            or tuple(proposal.affected_domains) != (AFFECTED_DOMAIN,)
            or not _PROPOSAL_ID.fullmatch(proposal.proposal_id)
        ):
            raise ActorError("proposal_shape_invalid")
        bdf = proposal_bdf(proposal)
        if set(proposal.resource_ids) != {CONTAINER_RESOURCE, f"gpu:{bdf}"}:
            raise ActorError("proposal_resources_invalid")

    def status(self) -> ActorStatus:
        request_id = self.request_id_factory()
        return parse_status(self.client.run("status", request_id), request_id)

    def preflight(self, proposal: ActionProposal) -> PreActionEvidence:
        self.validate(proposal)
        bdf = proposal_bdf(proposal)
        status = self.status()
        received_at = self.clock()
        self._before[proposal.proposal_id] = status
        while len(self._before) > 16:
            self._before.pop(next(iter(self._before)))
        evidence_ref = self.evidence.record(
            "preflight-status", proposal.proposal_id, _status_document(status)
        )
        backup_ref = self.backup_ref(proposal)
        resources = (CONTAINER_RESOURCE, f"gpu:{bdf}") if bdf in status.handover_blocked else (CONTAINER_RESOURCE,)
        return PreActionEvidence(
            machine_id=MACHINE_ID,
            target_identity_verified=status.identity_verified,
            evidence_revision=evidence_revision(status, bdf),
            sources=(
                SourceState("host", host_revision(status), received_at),
                SourceState("rentals", status.tenants.digest, received_at),
            ),
            resource_ids=resources,
            rental_impacts=(),
            affected_domains=(AFFECTED_DOMAIN,),
            mappings_known=False,
            power_domain_proven=False,
            evidence_ref=evidence_ref,
            backup_ref=backup_ref,
            backup_succeeded=backup_ref is not None,
        )

    def dispatch(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        try:
            document = self.client.run("restart", execution_id)
        except ActorError as error:
            # The helper may have acted before the channel failed; reconcile reads the ledger.
            return DispatchResult(execution_id, DispatchStatus.UNKNOWN, str(error))
        return _restart_result(document, execution_id)

    def postflight(
        self, proposal: ActionProposal, execution_id: str, result: DispatchResult
    ) -> PostActionEvidence:
        before = self._preflight(proposal)
        after = self.status()
        ref = self.evidence.record(
            "postflight-status",
            execution_id,
            {"execution_id": execution_id, "status": _status_document(after)},
        )
        ok, detail = postcondition(before, after, proposal_bdf(proposal))
        return PostActionEvidence(execution_id, ref, ok and result.status is DispatchStatus.SUCCEEDED, detail)

    def _preflight(self, proposal: ActionProposal) -> ActorStatus | None:
        """The pre-dispatch status: kept in memory, or after a service restart the
        preserved evidence whose reference the broker holds privately."""
        before = self._before.get(proposal.proposal_id)
        if before is not None:
            return before
        ref = self.preflight_ref(proposal)
        document = None if ref is None else self.evidence.verified(ref, "preflight-status", proposal.proposal_id)
        if document is None:
            return None
        try:
            return status_from_evidence(document)
        except (ActorError, KeyError, TypeError, ValueError):
            return None

    def reconcile(self, proposal: ActionProposal, execution_id: str) -> DispatchResult:
        document = self.client.run("result", execution_id)
        try:
            _base(document, "result", execution_id)
        except ActorError as error:
            return DispatchResult(execution_id, DispatchStatus.UNKNOWN, str(error))
        reason = _reason(document)
        state = document.get("state")
        if state is None:
            # An envelope-only answer (ledger unavailable, or a run holding the lock that
            # may be this execution) proves nothing either way.
            return DispatchResult(execution_id, DispatchStatus.UNKNOWN, reason)
        result = _restart_result(document, execution_id, operation="result")
        if state not in ("executed", "interrupted") or result.status is not DispatchStatus.UNKNOWN:
            return result
        try:
            # Both times come from the target's clock, so skew with Imladris cannot matter.
            observed = _utc(document.get("observed_at"), "observed_at")
            settled = observed - _utc(document.get("completed_at"), "completed_at") >= RESTART_SETTLE
        except ActorError:
            settled = False
        if not settled:
            return result
        started_before = document.get("started_at_before")
        if started_before is None:
            # Normally the helper armed the claim with its baseline; otherwise use the
            # verified preflight.
            before = self._preflight(proposal)
            started_before = None if before is None else before.container.started_at
        if not isinstance(started_before, str):
            return DispatchResult(execution_id, DispatchStatus.UNKNOWN, f"{reason}; no start-time baseline")
        return self._settle_from_status(
            started_before, document.get("execution_boot_id"), execution_id, reason
        )

    def _settle_from_status(
        self, started_before: str, execution_boot_id: object, execution_id: str, reason: str
    ) -> DispatchResult:
        try:
            status = self.status()
        except (ActorError, OSError, ValueError):
            return DispatchResult(execution_id, DispatchStatus.UNKNOWN, f"{reason}; status unavailable")
        if not status.identity_verified:
            return DispatchResult(execution_id, DispatchStatus.UNKNOWN, f"{reason}; target identity unverified")
        if isinstance(execution_boot_id, str) and _UUID.fullmatch(execution_boot_id) and (
            execution_boot_id != status.boot_id
        ):
            # A reboot restarts the exporter too, so its start time proves nothing.
            return DispatchResult(
                execution_id, DispatchStatus.FAILED,
                f"{reason}; target rebooted before the restart could be confirmed",
            )
        if status.container.running and status.container.started_at not in (None, started_before):
            return DispatchResult(
                execution_id, DispatchStatus.SUCCEEDED, f"restart took effect after {reason}"
            )
        return DispatchResult(
            execution_id, DispatchStatus.FAILED, f"{reason}; restart did not take effect"
        )


def _restart_result(
    document: Mapping[str, Any], execution_id: str, *, operation: str = "restart"
) -> DispatchResult:
    try:
        _base(document, operation, execution_id)
    except ActorError as error:
        return DispatchResult(execution_id, DispatchStatus.UNKNOWN, str(error))
    detail = _reason(document)
    state = document.get("state")
    if state == "unknown":
        return DispatchResult(execution_id, DispatchStatus.UNKNOWN, "helper ledger unknown")
    if document.get("ok") is True and state == "executed":
        return DispatchResult(execution_id, DispatchStatus.SUCCEEDED, "restart completed")
    if state is None and detail in UNCLAIMED_REFUSAL_REASONS:
        return DispatchResult(execution_id, DispatchStatus.REFUSED, detail)
    if state == "refused" or (state == "executed" and detail == "restart_launch_failed"):
        # The helper stopped before docker ran, or docker could not be started.
        return DispatchResult(execution_id, DispatchStatus.REFUSED, detail)
    if state != "executed" or detail in UNCERTAIN_RESTART_REASONS | UNCERTAIN_ENVELOPE_REASONS:
        return DispatchResult(execution_id, DispatchStatus.UNKNOWN, detail)
    return DispatchResult(execution_id, DispatchStatus.FAILED, detail)


def _reason(document: Mapping[str, Any]) -> str:
    reason = document.get("reason")
    if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,64}", reason):
        return reason
    return "restart failed"


def postcondition(
    before: ActorStatus | None, after: ActorStatus, bdf: str | None = None
) -> tuple[bool, str]:
    """Judge the stop conditions from before and after status documents.

    With ``bdf``, "handover cleared" means that GPU is no longer blocked; other GPUs
    may still be blocked and get their own proposals.
    """
    if before is None:
        return False, "preflight status unavailable"
    if not after.identity_verified or after.boot_id != before.boot_id:
        return False, "target identity or boot changed"
    if not after.container.running or after.container.started_at in {None, before.container.started_at}:
        return False, "dcgm-exporter is not running with a new start time"
    removed = set(before.tenants.names) - set(after.tenants.names)
    if removed - set(before.vm_containers):
        return False, "a non-VM tenant container disappeared"
    # The expected success removes the stuck VM rental. Every other tenant must survive
    # as the same container with the same start time; a new rental is only reported.
    after_members = {member.name: member for member in after.tenants.members}
    for member in before.tenants.members:
        if member.name in removed:
            continue
        current = after_members.get(member.name)
        if current is None or (current.container_id, current.started_at) != (
            member.container_id, member.started_at
        ):
            return False, "a tenant container was recreated or restarted"
    prefix = "new tenant containers appeared; " if set(after.tenants.names) - set(before.tenants.names) else ""
    blocked = bdf in after.handover_blocked if bdf is not None else bool(after.handover_blocked)
    return True, prefix + ("handover still blocked" if blocked else HANDOVER_CLEARED)


def status_from_evidence(document: Mapping[str, Any]) -> ActorStatus:
    """Rebuild a status from its preserved evidence document (see ``_status_document``)."""
    hostname, board, boot_id = document["hostname"], document["board"], document["boot_id"]
    if not all(isinstance(value, str) for value in (hostname, board, boot_id)):
        raise ActorError("evidence_identity_invalid")
    return ActorStatus(
        request_id=str(document["request_id"]),
        observed_at=_utc(document["observed_at"], "observed_at"),
        hostname=hostname,
        board=board,
        boot_id=boot_id,
        container=_container(document["container"]),
        handover_blocked=_names(document["handover_blocked"], _BDF, "handover_blocked"),
        nvidia_visible_count=_count(document["nvidia_visible_count"], "nvidia_visible_count"),
        pci_gpu_count=_count(document["pci_gpu_count"], "pci_gpu_count"),
        tenants=_tenants(document["tenants"]),
        vm_containers=_names(document["vm_containers"], _TENANT, "vm_containers"),
    )


def _status_document(status: ActorStatus) -> dict[str, Any]:
    return {
        "request_id": status.request_id,
        "observed_at": status.observed_at.isoformat().replace("+00:00", "Z"),
        "hostname": status.hostname,
        "board": status.board,
        "boot_id": status.boot_id,
        "container": {
            "present": status.container.present,
            "running": status.container.running,
            "started_at": status.container.started_at,
        },
        "handover_blocked": list(status.handover_blocked),
        "nvidia_visible_count": status.nvidia_visible_count,
        "pci_gpu_count": status.pci_gpu_count,
        "tenants": {
            "count": status.tenants.count,
            "digest": status.tenants.digest,
            "names": list(status.tenants.names),
            "members": [
                {"name": member.name, "id": member.container_id, "started_at": member.started_at}
                for member in status.tenants.members
            ],
        },
        "vm_containers": list(status.vm_containers),
    }
