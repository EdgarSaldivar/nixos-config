"""Phase 2 capability-broker foundation: typed, bounded, fail-closed, disabled by default.

This module is the local, testable core of the rearchitecture's capability broker
(docs/OPERATOR-AGENT-REARCHITECTURE.md, Phase 2). It gives a durable task four
request families -- arbitrary read-only target observation, sanitized controller
state, a task-scoped development workspace, and injected web/repository research
-- without giving the model a credential, a socket, or a second command
whitelist. Nothing constructs it in the deployed runtime yet: it is inert until
the feature flag and its adapters are wired in a later commissioning step.

The design keeps capability open and authority conditional:

- Operations are free text. There is no catalogue of allowed commands at this
  layer; a previously unseen read, build, package, or repository operation is
  expressible without a source change. What is enforced instead is identity and
  effect: the target machine binding, the task-to-worktree binding, workspace
  path containment, and a typed effect declaration whose tenant / production /
  external / network / host classes fail closed to the (future) approval path.
  Effect enforcement is structural -- a declared ``effects`` list against a
  closed vocabulary -- never a keyword scan of evidence text, so ordinary
  argument text stays readable and intent-neutral.
- The model never touches transport, and this process never spawns a workspace
  subprocess. Workspace ``run`` exists only when the deployment injects a
  sandbox runner that attests, by exact contract string, to dispatching the
  command as an external process inside the task's worktree with no network,
  no reads or writes outside that root, and no credential paths; without that
  runner, ``run`` fails closed. The runner hands back a handle with
  wait/kill semantics, and the broker -- not the runner -- enforces the
  deadline: at timeout it kills the handle and confirms nothing survived
  before the concurrency slot is released; a runner whose work survives kill
  is quarantined and every later run fails closed. A Python working directory
  is bookkeeping, not isolation, and an unkillable Python thread is never an
  execution vehicle here. Target reads and research go through injected
  read-only adapters that attest to a cancellable contract that also forbids
  credential-path access and any direct network use beyond the purpose-limited
  transport the broker deployment itself owns; an adapter that cannot be
  cancelled after its deadline is quarantined.
- Every request has durable identity: a request record is published under the
  state root before dispatch, per-task budgets persist across restarts, a
  completed request replays its recorded result without executing again, and a
  crash-interrupted workspace mutation is reported as permanently uncertain
  rather than blindly re-executed.
- Every result is bounded, redacted, labelled with provenance as untrusted
  evidence, and recorded as an immutable content-addressed artifact published
  crash-safely (temp file, fsync, atomic rename) in a ledger the workspace
  runner can never reach.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import re
import stat
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .plans import (
    _CREDENTIAL_VALUE,
    _credential_key,
    _reject_credential_sequence,
)

BROKER_SCHEMA_VERSION = 2
MACHINE_ID = "17049"
FEATURE_FLAG_ENV = "TERRACOMPUTE_CAPABILITY_BROKER"
WORKSPACE_ROOT_ENV = "TERRACOMPUTE_BROKER_WORKSPACE_ROOT"
STATE_ROOT_ENV = "TERRACOMPUTE_BROKER_STATE_ROOT"
SANDBOX_RUNNER_ENV = "TERRACOMPUTE_BROKER_SANDBOX_RUNNER"

FAMILIES = ("observe_target", "controller_state", "workspace", "research")
# What each family's output is, said on every result. The labels exist for the
# prompt-injection boundary: everything a model reads through this broker is
# evidence about the world, never an instruction from it.
PROVENANCE = MappingProxyType({
    "observe_target": "target-machine-output",
    "controller_state": "controller-state-sanitized",
    "workspace": "workspace-command-output",
    "research": "external-research-content",
})
EVIDENCE_AUTHORITY = "evidence-only"
EVIDENCE_TRUST = "untrusted-evidence"

# Typed effect vocabulary. A request declares effects structurally; nothing is
# inferred from key spellings or free text. Effects a family does not grant --
# and every tenant/production/external/network/host class, however declared --
# fail closed until the Phase 3 approval path exists.
EFFECT_READ = "read"
EFFECT_WORKSPACE = "workspace"
APPROVAL_REQUIRED_EFFECTS = frozenset({
    "tenant", "production", "external", "network", "host",
})
FAMILY_GRANTED_EFFECTS = MappingProxyType({
    "observe_target": frozenset({EFFECT_READ}),
    "controller_state": frozenset({EFFECT_READ}),
    "research": frozenset({EFFECT_READ}),
    "workspace": frozenset({EFFECT_READ, EFFECT_WORKSPACE}),
})

# The exact isolation contract an injected workspace runner must attest to.
# The broker hands the runner only the bound worktree, a scrubbed environment,
# the command, and its deadline/output bounds; the runner attests that
# ``start`` dispatches the command as an external process (never a thread in
# this interpreter) with no network, no reads or writes outside that task
# root, and no credential paths, that ``wait`` returns within roughly the
# requested timeout, and that ``kill`` destroys the process and everything it
# spawned so no runner work survives it. The v1 synchronous run() contract is
# gone: an unkillable in-process runner could outlive its deadline while
# holding broker capacity, so it is no longer accepted at all.
SANDBOX_RUNNER_CONTRACT = (
    "sandbox-runner-v2:external-process,no-network,no-read-outside-task-root,"
    "no-write-outside-task-root,no-credential-paths,wait-bounded,"
    "kill-terminates-all-work"
)

# The contract every observe/controller/research adapter must attest to: it is
# a read-only capability (it mutates neither the target nor any workspace),
# it never opens credential paths (auth files, key material, systemd
# credential directories), it opens no network transport of its own -- the
# only transport it may use is the purpose-limited channel the broker
# deployment owns and wires into it -- and cancel() promptly aborts an
# in-flight invoke() so no thread is abandoned.
READ_ONLY_ADAPTER_CONTRACT = (
    "read-only-cancellable-adapter-v2:no-mutation,no-workspace-access,"
    "no-credential-paths,no-direct-network,broker-owned-transport-only,"
    "cancel-terminates-promptly"
)

MAX_OPERATION_CHARS = 4096
MAX_REQUEST_BYTES = 128 * 1024
MAX_OUTPUT_BYTES_CEILING = 256 * 1024
MAX_TIMEOUT_CEILING_SECONDS = 330.0
MAX_ARTIFACT_BYTES = 512 * 1024
MAX_WORKSPACE_FILE_BYTES = 256 * 1024
MAX_EFFECTS = 16

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EFFECT_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_REQUEST_KEYS = frozenset({
    "schema_version", "request_id", "task_id", "machine_id", "family",
    "operation", "arguments", "effects", "timeout_seconds", "max_output_bytes",
})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Output redaction. A line naming credential material goes whole; an opaque
# high-entropy token goes alone. Same posture as the investigator runtime's
# report sanitizer: better to lose a citation than to publish a secret.
_SENSITIVE_LINE = re.compile(
    r"(?i)(authorization|bearer|api[-_ ]?key|password|passwd|credential|auth\.json|"
    r"access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|private[-_ ]?key|"
    r"secret[-_ ]?key|passphrase)"
)
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9_=-]{40,}\b")

# The only names a sandboxed workspace command inherits. Everything else --
# tokens, cloud credentials, agent configuration -- stays with the broker.
SAFE_ENVIRONMENT_NAMES = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")

# Workspace operations that cannot mutate the worktree. Anything else in the
# workspace family -- including an unknown future primitive -- is treated as a
# mutation for crash-recovery purposes, so uncertainty always fails closed.
_READ_ONLY_WORKSPACE_OPERATIONS = frozenset({"read_file", "list_dir"})

# errno values that mean a path component turned out to be a symlink or
# otherwise escaped the expected directory shape mid-walk.
_ESCAPE_ERRNOS = frozenset(
    value for value in (
        getattr(errno, "ELOOP", None),
        getattr(errno, "EMLINK", None),
        getattr(errno, "ENOTDIR", None),
    ) if value is not None
)

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)

# Component-safe traversal needs openat(2)-style dir_fd support and O_NOFOLLOW.
# Without them the direct file primitives fail closed rather than fall back to
# a resolve-then-open sequence that a symlink race could subvert.
_FD_OPS_SUPPORTED = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and _O_NOFOLLOW != 0
    and _O_DIRECTORY != 0
)

ADAPTER_CANCEL_GRACE_SECONDS = 5.0
RUNNER_KILL_GRACE_SECONDS = 5.0


class BrokerError(ValueError):
    """A fixed-category broker failure safe to expose to the model."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _canonical(document: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            document, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise BrokerError("document-encoding-failed") from error


def _digest(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _fsync_directory(directory: Path) -> None:
    handle = os.open(str(directory), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _atomic_publish(
    directory: Path, path: Path, encoded: bytes, *,
    exclusive: bool, final_mode: int = 0o600,
) -> None:
    """Crash-safe publish: temp file, write, fsync, atomic link/rename, dir fsync.

    With ``exclusive`` the publish uses link(2) so exactly one writer can claim
    the name; a lost race raises FileExistsError with no partial file at the
    destination. Either way the destination only ever holds complete content.
    """
    temp = directory / f".tmp-{uuid.uuid4().hex}"
    handle = os.open(
        str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600
    )
    try:
        try:
            os.write(handle, encoded)
            os.fsync(handle)
            os.fchmod(handle, final_mode)
        finally:
            os.close(handle)
        if exclusive:
            os.link(str(temp), str(path), follow_symlinks=False)
            os.unlink(str(temp))
        else:
            os.replace(str(temp), str(path))
        _fsync_directory(directory)
    except FileExistsError:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise
    except OSError as error:
        try:
            os.unlink(str(temp))
        except OSError:
            pass
        raise BrokerError("state-write-failed") from error


def redact_secrets(text: str) -> str:
    """Remove credential-shaped lines and opaque tokens from model-visible text."""
    safe_lines: list[str] = []
    for line in text.splitlines():
        line = "".join(c for c in line if c >= " " or c == "\t")
        if _SENSITIVE_LINE.search(line):
            safe_lines.append("[sensitive-content-redacted]")
            continue
        safe_lines.append(_HIGH_ENTROPY.sub("[opaque-value-redacted]", line))
    return "\n".join(safe_lines)


def safe_subprocess_environment(
    home: Path, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A scrubbed environment for sandboxed workspace commands: no inherited secret."""
    source = base if base is not None else os.environ
    environment = {name: source[name] for name in SAFE_ENVIRONMENT_NAMES if name in source}
    environment["HOME"] = str(home)
    return environment


# -- typed requests ---------------------------------------------------------------


@dataclass(frozen=True)
class BrokerRequest:
    """One versioned, typed capability request. Operations are free text."""

    request_id: str
    task_id: str
    machine_id: str
    family: str
    operation: str
    arguments: Mapping[str, Any]
    effects: tuple[str, ...]
    timeout_seconds: float
    max_output_bytes: int
    schema_version: int = BROKER_SCHEMA_VERSION

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "machine_id": self.machine_id,
            "family": self.family,
            "operation": self.operation,
            "arguments": _thaw_json(self.arguments),
            "effects": list(self.effects),
            "timeout_seconds": self.timeout_seconds,
            "max_output_bytes": self.max_output_bytes,
        }

    def digest(self) -> str:
        return _digest(_canonical(self.document()))

    def mutates_workspace(self) -> bool:
        return (
            self.family == "workspace"
            and self.operation not in _READ_ONLY_WORKSPACE_OPERATIONS
        )


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple) or isinstance(value, list):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


_MAX_ARGUMENT_STRING = 128 * 1024
_MAX_ARGUMENT_KEYS = 256
_MAX_ARGUMENT_ITEMS = 4096


def _safe_arguments(value: Any, path: str = "arguments", depth: int = 0) -> Any:
    """Detached, bounded, credential-free request arguments.

    The point is what may not cross: credential-shaped keys, credential-bearing
    values and argv forms, unbounded nesting. Text keeps its newlines and tabs;
    everything below space otherwise is rejected. Nothing here infers intent
    from wording: argument text is data, and only credential material is walled.
    """
    if depth > 16:
        raise BrokerError("request-arguments-unsafe")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise BrokerError("request-arguments-unsafe")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_ARGUMENT_STRING:
            raise BrokerError("request-arguments-unsafe")
        if any(c < " " and c not in "\t\n\r" for c in value) or "\x7f" in value:
            raise BrokerError("request-arguments-unsafe")
        if _CREDENTIAL_VALUE.search(value):
            raise BrokerError("request-arguments-unsafe")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_ARGUMENT_KEYS:
            raise BrokerError("request-arguments-unsafe")
        copied: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or not raw_key or len(raw_key) > 128:
                raise BrokerError("request-arguments-unsafe")
            if _credential_key(raw_key):
                raise BrokerError("request-arguments-unsafe")
            copied[raw_key] = _safe_arguments(item, f"{path}.{raw_key}", depth + 1)
        return copied
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_ARGUMENT_ITEMS:
            raise BrokerError("request-arguments-unsafe")
        items = [_safe_arguments(item, f"{path}[]", depth + 1) for item in value]
        try:
            _reject_credential_sequence(list(value), path)
        except ValueError as error:
            raise BrokerError("request-arguments-unsafe") from error
        return items
    raise BrokerError("request-arguments-unsafe")


def _pairs_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError("request-duplicate-field")
        result[key] = value
    return result


def parse_broker_request(document: Any) -> BrokerRequest:
    """Validate one request document; malformed input raises, it never executes."""
    if not isinstance(document, Mapping) or set(document) != _REQUEST_KEYS:
        raise BrokerError("request-schema-invalid")
    version = document["schema_version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != BROKER_SCHEMA_VERSION:
        raise BrokerError("request-schema-version-unknown")
    request_id = document["request_id"]
    task_id = document["task_id"]
    machine_id = document["machine_id"]
    for name, value in (("request_id", request_id), ("task_id", task_id)):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise BrokerError(f"request-{name.replace('_', '-')}-invalid")
    if not isinstance(machine_id, str) or not machine_id or len(machine_id) > 32:
        raise BrokerError("request-machine-id-invalid")
    family = document["family"]
    if family not in FAMILIES:
        raise BrokerError("request-family-unknown")
    operation = document["operation"]
    if (
        not isinstance(operation, str)
        or not operation.strip()
        or len(operation) > MAX_OPERATION_CHARS
        or any(c < " " and c not in "\t\n" for c in operation)
    ):
        raise BrokerError("request-operation-invalid")
    arguments = document["arguments"]
    if not isinstance(arguments, Mapping):
        raise BrokerError("request-arguments-invalid")
    # Bounded JSON with no credential-shaped keys or values: a request cannot
    # carry a secret in. Unlike plan fields, argument strings may hold ordinary
    # text whitespace -- file content and shell scripts need their newlines.
    checked = _safe_arguments(dict(arguments))
    effects = document["effects"]
    if not isinstance(effects, (list, tuple)) or len(effects) > MAX_EFFECTS:
        raise BrokerError("request-effects-invalid")
    for effect in effects:
        if not isinstance(effect, str) or not _EFFECT_TOKEN.fullmatch(effect):
            raise BrokerError("request-effects-invalid")
    timeout = document["timeout_seconds"]
    if (
        not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
        or not 0 < float(timeout) <= MAX_TIMEOUT_CEILING_SECONDS
    ):
        raise BrokerError("request-timeout-invalid")
    max_output = document["max_output_bytes"]
    if (
        not isinstance(max_output, int) or isinstance(max_output, bool)
        or not 1 <= max_output <= MAX_OUTPUT_BYTES_CEILING
    ):
        raise BrokerError("request-output-bound-invalid")
    return BrokerRequest(
        request_id=request_id, task_id=task_id, machine_id=machine_id,
        family=family, operation=operation, arguments=_freeze_json(checked),
        effects=tuple(effects), timeout_seconds=float(timeout),
        max_output_bytes=max_output,
    )


def parse_broker_request_json(text: str | bytes) -> BrokerRequest:
    encoded = text.encode("utf-8") if isinstance(text, str) else text
    if len(encoded) > MAX_REQUEST_BYTES:
        raise BrokerError("request-size-limit")
    try:
        document = json.loads(encoded, object_pairs_hook=_pairs_no_duplicates)
    except BrokerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise BrokerError("request-json-invalid") from error
    return parse_broker_request(document)


# -- results ----------------------------------------------------------------------


@dataclass(frozen=True)
class BrokerResult:
    request_id: str
    request_digest: str
    task_id: str
    machine_id: str
    family: str
    operation: str
    provenance: str
    status: str  # ok | denied | error | timeout | uncertain
    reason: str | None
    output: str
    truncated: bool
    artifact_digest: str | None
    schema_version: int = BROKER_SCHEMA_VERSION
    authority: str = EVIDENCE_AUTHORITY
    trust: str = EVIDENCE_TRUST

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": "broker-result",
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "task_id": self.task_id,
            "machine_id": self.machine_id,
            "family": self.family,
            "operation": self.operation,
            "provenance": self.provenance,
            "authority": self.authority,
            "trust": self.trust,
            "status": self.status,
            "reason": self.reason,
            "output": self.output,
            "truncated": self.truncated,
        }


# -- sandbox runner contract --------------------------------------------------------


@dataclass(frozen=True)
class SandboxCommand:
    """Everything a sandbox runner receives. Nothing else crosses this boundary:
    no ledger paths, no state root, no credential names, no broker internals."""

    task_id: str
    task_root: Path
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    timeout_seconds: float
    max_output_bytes: int


@dataclass(frozen=True)
class SandboxOutcome:
    status: str  # ok | timeout | error
    exit_code: int | None = None
    output: str = ""


class SandboxHandle:
    """One dispatched sandbox execution the broker can bound and destroy.

    ``wait`` blocks for at most roughly ``timeout_seconds`` and returns the
    ``SandboxOutcome`` once the external process has fully finished, or None
    while work is still running. ``kill`` destroys the external process and
    everything it spawned; after ``kill`` returns, a subsequent ``wait`` must
    promptly report an outcome, which is the broker's proof that no runner
    work survived the request. ``wait`` after completion is idempotent.
    """

    def wait(self, timeout_seconds: float) -> SandboxOutcome | None:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError


class SandboxRunner:
    """Base class documenting the injected workspace runner contract.

    ``start`` dispatches ``command.argv`` with ``command.environment`` inside
    ``command.task_root`` as an external process under real isolation -- no
    network, no reads or writes outside that root, no credential paths --
    bounding captured output by ``command.max_output_bytes``, and returns a
    ``SandboxHandle``. The broker, not the runner, enforces the deadline: it
    waits at most the request's timeout, then kills the handle and requires
    confirmation that nothing survived before the request finishes. A runner
    attests to that contract by carrying the exact ``SANDBOX_RUNNER_CONTRACT``
    string; the broker refuses anything else, including the retired v1
    synchronous ``run`` contract, whose in-process execution could neither be
    cancelled nor killed.
    """

    contract = ""

    def start(self, command: SandboxCommand) -> SandboxHandle:
        raise NotImplementedError


class ReadOnlyAdapter:
    """Base class documenting the read-only cancellable adapter contract.

    ``invoke`` performs one read-only operation; it must not mutate the target
    machine, the controller, or any workspace. It must never open a
    credential path -- auth files, key material, systemd credential
    directories -- and it must not open direct network transport of its own:
    the only transport it may use is the purpose-limited, broker-owned
    channel the deployment wires into it (the read-only forced-command target
    channel, the deployment's research fetcher). ``cancel`` must promptly
    abort an in-flight ``invoke`` so its thread terminates; an adapter that
    keeps running after cancel is quarantined and receives no further work.
    """

    contract = READ_ONLY_ADAPTER_CONTRACT

    def invoke(self, operation: str, arguments: Mapping[str, Any]) -> Any:
        raise NotImplementedError

    def cancel(self) -> None:
        raise NotImplementedError


def _verified_sandbox_runner(runner: Any) -> Any:
    if runner is None:
        return None
    if (
        getattr(runner, "contract", None) != SANDBOX_RUNNER_CONTRACT
        or not callable(getattr(runner, "start", None))
    ):
        raise BrokerError("sandbox-runner-contract-mismatch")
    return runner


def _verified_adapter(adapter: Any) -> Any:
    if adapter is None:
        return None
    if (
        getattr(adapter, "contract", None) != READ_ONLY_ADAPTER_CONTRACT
        or not callable(getattr(adapter, "invoke", None))
        or not callable(getattr(adapter, "cancel", None))
    ):
        raise BrokerError("adapter-contract-mismatch")
    return adapter


# -- budgets and configuration -----------------------------------------------------


@dataclass(frozen=True)
class BrokerBudgets:
    max_timeout_seconds: float = 120.0
    max_output_bytes: int = 64 * 1024
    max_concurrent: int = 2
    max_requests_per_task: int = 512

    def __post_init__(self) -> None:
        if not 0 < self.max_timeout_seconds <= MAX_TIMEOUT_CEILING_SECONDS:
            raise BrokerError("budget-timeout-invalid")
        if not 1 <= self.max_output_bytes <= MAX_OUTPUT_BYTES_CEILING:
            raise BrokerError("budget-output-invalid")
        if not 1 <= self.max_concurrent <= 16:
            raise BrokerError("budget-concurrency-invalid")
        if not 1 <= self.max_requests_per_task <= 100_000:
            raise BrokerError("budget-request-count-invalid")


@dataclass(frozen=True)
class BrokerConfig:
    """Disabled by default: an unconfigured deployment builds no broker."""

    state_root: Path
    allowed_workspace_roots: tuple[Path, ...]
    enabled: bool = False
    machine_id: str = MACHINE_ID
    budgets: BrokerBudgets = field(default_factory=BrokerBudgets)
    sandbox_runner_command: str | None = None

    def __post_init__(self) -> None:
        paths = (self.state_root, *self.allowed_workspace_roots)
        if not self.allowed_workspace_roots:
            raise BrokerError("workspace-roots-missing")
        for path in paths:
            if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                raise BrokerError("configuration-path-invalid")
        for root in self.allowed_workspace_roots:
            if self.state_root == root or self.state_root in root.parents or root in self.state_root.parents:
                raise BrokerError("state-root-not-dedicated")
        if not isinstance(self.machine_id, str) or not self.machine_id or len(self.machine_id) > 32:
            raise BrokerError("configuration-machine-id-invalid")
        if self.sandbox_runner_command is not None and (
            not isinstance(self.sandbox_runner_command, str)
            or not self.sandbox_runner_command.strip()
        ):
            raise BrokerError("configuration-sandbox-runner-invalid")


def broker_enabled(environment: Mapping[str, str] | None = None) -> bool:
    source = environment if environment is not None else os.environ
    return source.get(FEATURE_FLAG_ENV) == "1"


def config_from_environment(
    environment: Mapping[str, str] | None = None,
) -> BrokerConfig | None:
    """The deployment's broker configuration, or None while the flag is off."""
    source = environment if environment is not None else os.environ
    if not broker_enabled(source):
        return None
    workspace_root = source.get(WORKSPACE_ROOT_ENV)
    state_root = source.get(STATE_ROOT_ENV)
    if not workspace_root or not state_root:
        raise BrokerError("broker-environment-incomplete")
    return BrokerConfig(
        state_root=Path(state_root),
        allowed_workspace_roots=(Path(workspace_root),),
        enabled=True,
        sandbox_runner_command=source.get(SANDBOX_RUNNER_ENV) or None,
    )


# -- workspace binding --------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceGrant:
    task_id: str
    root: Path


class WorkspaceRegistry:
    """Task-to-worktree identity binding, durable across restarts.

    Containment here is identity, not an operation list: a task may do anything
    inside its own worktree, and nothing outside it. Direct file primitives
    prove containment at open time with component-safe dirfd traversal, and the
    sandbox runner receives only the bound root.
    """

    def __init__(self, allowed_roots: Sequence[Path], state_dir: Path, machine_id: str):
        if not allowed_roots:
            raise BrokerError("workspace-roots-missing")
        if not isinstance(machine_id, str) or not machine_id:
            raise BrokerError("configuration-machine-id-invalid")
        self.machine_id = machine_id
        resolved: list[Path] = []
        for root in allowed_roots:
            if not root.is_absolute() or ".." in root.parts:
                raise BrokerError("workspace-root-invalid")
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            resolved.append(root.resolve(strict=True))
        self.allowed_roots = tuple(resolved)
        self.grants_dir = state_dir / "grants"
        self.grants_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._grants: dict[str, WorkspaceGrant] = {}
        self._lock = threading.Lock()

    def _grant_path(self, task_id: str) -> Path:
        return self.grants_dir / f"{task_id}.json"

    def bind(self, task_id: str) -> WorkspaceGrant:
        if not _IDENTIFIER.fullmatch(task_id) or "/" in task_id:
            raise BrokerError("task-id-invalid")
        with self._lock:
            existing = self._load(task_id)
            if existing is not None:
                return existing
            root = self.allowed_roots[0] / task_id
            root.mkdir(mode=0o700, parents=False, exist_ok=True)
            if root.is_symlink():
                raise BrokerError("workspace-root-symlink")
            grant = WorkspaceGrant(task_id, root.resolve(strict=True))
            self._contained_in_allowed(grant.root)
            body = {
                "schema_version": BROKER_SCHEMA_VERSION,
                "task_id": task_id,
                "machine_id": self.machine_id,
                "root": str(grant.root),
            }
            body["digest"] = _digest(_canonical(body))
            try:
                _atomic_publish(
                    self.grants_dir, self._grant_path(task_id), _canonical(body),
                    exclusive=True,
                )
            except FileExistsError as error:
                raise BrokerError("grant-write-conflict") from error
            self._grants[task_id] = grant
            return grant

    def grant(self, task_id: str) -> WorkspaceGrant | None:
        with self._lock:
            return self._load(task_id)

    def _load(self, task_id: str) -> WorkspaceGrant | None:
        if task_id in self._grants:
            return self._grants[task_id]
        path = self._grant_path(task_id)
        try:
            encoded = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise BrokerError("grant-unreadable") from error
        try:
            body = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BrokerError("grant-invalid") from error
        if not isinstance(body, dict):
            raise BrokerError("grant-invalid")
        recorded = body.pop("digest", None)
        if recorded != _digest(_canonical(body)):
            raise BrokerError("grant-tampered")
        if body.get("task_id") != task_id or body.get("machine_id") != self.machine_id:
            raise BrokerError("grant-identity-mismatch")
        root = Path(str(body.get("root", "")))
        if not root.is_absolute():
            raise BrokerError("grant-invalid")
        root = root.resolve(strict=True)
        self._contained_in_allowed(root)
        grant = WorkspaceGrant(task_id, root)
        self._grants[task_id] = grant
        return grant

    def _contained_in_allowed(self, root: Path) -> None:
        if not any(
            root == allowed or root.is_relative_to(allowed)
            for allowed in self.allowed_roots
        ):
            raise BrokerError("workspace-containment")


# -- component-safe direct file primitives -------------------------------------------


def _contained_parts(root: Path, candidate: Any, *, allow_root: bool) -> tuple[str, ...]:
    """Split one request path into components proven lexically inside the root.

    Only the lexical layer happens here; the physical layer -- symlinks placed
    at any component, including a parent directory swapped for a symlink after
    this check -- is defeated by the dirfd/O_NOFOLLOW walk that consumes these
    parts, because every component is opened relative to the previous verified
    directory handle and never through a re-resolved absolute path.
    """
    if not isinstance(candidate, str) or "\x00" in candidate:
        raise BrokerError("workspace-path-invalid")
    if candidate == "":
        if allow_root:
            return ()
        raise BrokerError("workspace-path-invalid")
    path = Path(candidate)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as error:
            raise BrokerError("workspace-containment") from error
    parts = path.parts
    for part in parts:
        if part == ".." or "/" in part or "\x00" in part:
            raise BrokerError("workspace-containment")
    if not parts and not allow_root:
        raise BrokerError("workspace-path-invalid")
    return parts


def _open_task_root(root: Path) -> int:
    if not _FD_OPS_SUPPORTED:
        raise BrokerError("workspace-fd-unsupported")
    try:
        return os.open(str(root), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError as error:
        if error.errno in _ESCAPE_ERRNOS:
            raise BrokerError("workspace-containment") from error
        raise BrokerError("workspace-file-invalid") from error


def _walk_directories(root_fd: int, parts: Sequence[str], *, create: bool) -> int:
    """Descend one verified component at a time; a symlink anywhere fails closed."""
    fd = root_fd
    try:
        for part in parts:
            try:
                next_fd = os.open(
                    part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                # The new entry must be durable in its parent before anything
                # is reported written beneath it.
                os.fsync(fd)
                next_fd = os.open(
                    part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as error:
                if error.errno in _ESCAPE_ERRNOS:
                    raise BrokerError("workspace-containment") from error
                raise
            if fd != root_fd:
                os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        if fd != root_fd:
            os.close(fd)
        raise


# -- immutable evidence ledger -------------------------------------------------------


class EvidenceLedger:
    """Content-addressed, append-only broker evidence that survives restarts.

    Artifacts are published crash-safely -- temp file, fsync, atomic rename,
    directory fsync -- and any bytes already present at an address are verified
    against that address before being trusted; a partial or corrupted file is
    replaced by a verified publish. The ledger lives under the state root,
    which configuration keeps disjoint from every workspace root, so nothing a
    workspace runner executes can reach it.
    """

    def __init__(self, root: Path, *, isolated_from: Sequence[Path] = ()):
        if not root.is_absolute() or ".." in root.parts:
            raise BrokerError("ledger-root-invalid")
        for boundary in isolated_from:
            if root == boundary or boundary in root.parents or root in boundary.parents:
                raise BrokerError("ledger-not-isolated")
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _task_dir(self, task_id: str) -> Path:
        if not _IDENTIFIER.fullmatch(task_id):
            raise BrokerError("task-id-invalid")
        directory = self.root / task_id
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        return directory

    @staticmethod
    def _read_verified(path: Path, digest: str) -> bytes | None:
        try:
            handle = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise BrokerError("artifact-invalid") from error
        try:
            status = os.fstat(handle)
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_ARTIFACT_BYTES:
                raise BrokerError("artifact-invalid")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(handle, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(handle)
        encoded = b"".join(chunks)
        if _digest(encoded) != digest:
            raise BrokerError("artifact-tampered")
        return encoded

    def record(self, task_id: str, document: Mapping[str, Any]) -> str:
        encoded = _canonical(document)
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise BrokerError("artifact-size-limit")
        digest = _digest(encoded)
        with self._lock:
            directory = self._task_dir(task_id)
            path = directory / f"{digest}.json"
            try:
                if self._read_verified(path, digest) is not None:
                    return digest  # Same content, same address: idempotent.
            except BrokerError:
                # A partial or corrupted file sits at this address. The correct
                # bytes are known by construction; republish them atomically.
                pass
            _atomic_publish(directory, path, encoded, exclusive=False, final_mode=0o400)
        return digest

    def load(self, task_id: str, digest: str) -> dict[str, Any]:
        if not _SHA256.fullmatch(digest):
            raise BrokerError("artifact-digest-invalid")
        path = self._task_dir(task_id) / f"{digest}.json"
        encoded = self._read_verified(path, digest)
        if encoded is None:
            raise BrokerError("artifact-missing")
        return json.loads(encoded)

    def list(self, task_id: str) -> tuple[str, ...]:
        directory = self._task_dir(task_id)
        names = sorted(
            entry.name[:-5]
            for entry in os.scandir(directory)
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".json")
        )
        return tuple(name for name in names if _SHA256.fullmatch(name))


# -- durable request ledger ----------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    kind: str  # execute | replay | denied | uncertain | error
    reason: str | None = None
    artifact_digest: str | None = None


class RequestLedger:
    """Durable, transactional request identity and budgets under the state root.

    Every admitted request publishes a pending record -- request_id plus request
    digest -- before anything executes, and transitions it atomically to
    completed with the result's artifact address. The same request_id with a
    different digest fails closed; a completed request replays its recorded
    result without executing; a pending workspace mutation found after a crash
    is permanently uncertain and is never blindly re-executed. Budgets are the
    count of durable records, so a restart forgets nothing.
    """

    def __init__(self, root: Path, machine_id: str, max_requests_per_task: int):
        if not root.is_absolute() or ".." in root.parts:
            raise BrokerError("request-ledger-root-invalid")
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.machine_id = machine_id
        self.max_requests_per_task = max_requests_per_task
        self._lock = threading.Lock()
        self._inflight: set[tuple[str, str]] = set()
        self._counts: dict[str, int] = {}

    def _task_dir(self, task_id: str) -> Path:
        if not _IDENTIFIER.fullmatch(task_id):
            raise BrokerError("task-id-invalid")
        directory = self.root / task_id
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        return directory

    def _record_path(self, directory: Path, request_id: str) -> Path:
        return directory / f"{request_id}.json"

    def _spent(self, task_id: str, directory: Path) -> int:
        if task_id not in self._counts:
            self._counts[task_id] = sum(
                1 for entry in os.scandir(directory)
                if entry.is_file(follow_symlinks=False)
                and entry.name.endswith(".json")
                and not entry.name.startswith(".")
            )
        return self._counts[task_id]

    def _record_body(
        self, request: BrokerRequest, state: str, artifact_digest: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": BROKER_SCHEMA_VERSION,
            "kind": "broker-request-record",
            "request_id": request.request_id,
            "task_id": request.task_id,
            "machine_id": self.machine_id,
            "request_digest": request.digest(),
            "state": state,
        }
        if artifact_digest is not None:
            body["result_artifact_digest"] = artifact_digest
        body["digest"] = _digest(_canonical(body))
        return body

    @staticmethod
    def _read_record(path: Path) -> dict[str, Any] | None:
        try:
            handle = os.open(str(path), os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise BrokerError("request-record-invalid") from error
        try:
            status = os.fstat(handle)
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_ARTIFACT_BYTES:
                raise BrokerError("request-record-invalid")
            encoded = os.read(handle, status.st_size + 1)
        finally:
            os.close(handle)
        try:
            body = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BrokerError("request-record-invalid") from error
        if not isinstance(body, dict):
            raise BrokerError("request-record-invalid")
        recorded = body.pop("digest", None)
        if recorded != _digest(_canonical(body)):
            raise BrokerError("request-record-invalid")
        return body

    def admit(self, request: BrokerRequest) -> Admission:
        with self._lock:
            directory = self._task_dir(request.task_id)
            path = self._record_path(directory, request.request_id)
            key = (request.task_id, request.request_id)
            try:
                record = self._read_record(path)
            except BrokerError as error:
                return Admission("error", error.reason)
            if record is not None:
                if (
                    record.get("task_id") != request.task_id
                    or record.get("machine_id") != self.machine_id
                    or record.get("kind") != "broker-request-record"
                ):
                    return Admission("error", "request-record-invalid")
                if record.get("request_digest") != request.digest():
                    # Same identity, different content: replay-with-substitution.
                    return Admission("denied", "request-identity-conflict")
                state = record.get("state")
                if state == "completed":
                    artifact = record.get("result_artifact_digest")
                    if not isinstance(artifact, str) or not _SHA256.fullmatch(artifact):
                        return Admission("error", "request-record-invalid")
                    return Admission("replay", artifact_digest=artifact)
                if state != "pending":
                    return Admission("error", "request-record-invalid")
                if key in self._inflight:
                    return Admission("denied", "request-in-flight")
                if request.mutates_workspace():
                    # Recorded before a dispatch that never completed: the
                    # mutation may or may not have happened. Never re-run it.
                    return Admission("uncertain", "workspace-outcome-uncertain")
                self._inflight.add(key)
                return Admission("execute")
            if self._spent(request.task_id, directory) >= self.max_requests_per_task:
                return Admission("denied", "task-request-budget")
            body = self._record_body(request, "pending", None)
            try:
                _atomic_publish(directory, path, _canonical(body), exclusive=True)
            except FileExistsError:
                return Admission("denied", "request-in-flight")
            self._counts[request.task_id] = self._spent(request.task_id, directory) + 1
            self._inflight.add(key)
            return Admission("execute")

    def complete(self, request: BrokerRequest, artifact_digest: str) -> None:
        with self._lock:
            directory = self._task_dir(request.task_id)
            path = self._record_path(directory, request.request_id)
            body = self._record_body(request, "completed", artifact_digest)
            _atomic_publish(directory, path, _canonical(body), exclusive=False)
            self._inflight.discard((request.task_id, request.request_id))

    def release(self, request: BrokerRequest) -> None:
        with self._lock:
            self._inflight.discard((request.task_id, request.request_id))


# -- the broker ----------------------------------------------------------------------


class CapabilityBroker:
    """Perform typed capability requests on behalf of the model.

    Adapters are injected and contract-checked; the broker owns limits,
    identity checks, request durability, redaction, provenance, and the
    evidence ledger. It holds no credential of its own and passes none through:
    read-only adapters take only (operation, arguments), and the sandbox
    runner receives only the bound worktree, a scrubbed environment, the
    command, and its bounds.
    """

    def __init__(
        self,
        config: BrokerConfig,
        *,
        observe_adapter: Any | None = None,
        controller_state_adapter: Any | None = None,
        research_adapters: Mapping[str, Any] | None = None,
        sandbox_runner: Any | None = None,
        base_environment: Mapping[str, str] | None = None,
        adapter_cancel_grace_seconds: float = ADAPTER_CANCEL_GRACE_SECONDS,
        runner_kill_grace_seconds: float = RUNNER_KILL_GRACE_SECONDS,
    ):
        if not config.enabled:
            raise BrokerError("capability-broker-disabled")
        self.config = config
        self.observe_adapter = _verified_adapter(observe_adapter)
        self.controller_state_adapter = _verified_adapter(controller_state_adapter)
        self.research_adapters = {
            name: _verified_adapter(adapter)
            for name, adapter in dict(research_adapters or {}).items()
        }
        self.sandbox_runner = _verified_sandbox_runner(sandbox_runner)
        self.base_environment = base_environment
        if not 0 < adapter_cancel_grace_seconds <= 60:
            raise BrokerError("adapter-grace-invalid")
        self._cancel_grace_seconds = adapter_cancel_grace_seconds
        if not 0 < runner_kill_grace_seconds <= 60:
            raise BrokerError("runner-grace-invalid")
        self._runner_kill_grace_seconds = runner_kill_grace_seconds
        self.workspaces = WorkspaceRegistry(
            config.allowed_workspace_roots, config.state_root, config.machine_id
        )
        self.ledger = EvidenceLedger(
            config.state_root / "evidence",
            isolated_from=config.allowed_workspace_roots,
        )
        self.requests = RequestLedger(
            config.state_root / "requests",
            config.machine_id,
            config.budgets.max_requests_per_task,
        )
        self._slots = threading.BoundedSemaphore(config.budgets.max_concurrent)
        self._revoked_adapters: set[int] = set()
        self._quarantined_runners: set[int] = set()
        self._file_lock = threading.Lock()

    # -- public entry points ---------------------------------------------------

    def bind_workspace(self, task_id: str) -> WorkspaceGrant:
        return self.workspaces.bind(task_id)

    def handle_json(self, text: str | bytes) -> BrokerResult:
        return self.handle(parse_broker_request_json(text))

    def handle(self, request: BrokerRequest) -> BrokerResult:
        if not isinstance(request, BrokerRequest):
            request = parse_broker_request(request)
        if request.machine_id != self.config.machine_id:
            return self._reject(request, "denied", "machine-identity-mismatch")
        denial = self._screen_effects(request)
        if denial is not None:
            return self._reject(request, "denied", denial)
        if not self._slots.acquire(blocking=False):
            return self._reject(request, "denied", "concurrency-limit")
        try:
            admission = self.requests.admit(request)
            if admission.kind == "replay":
                return self._replay(request, admission.artifact_digest)
            if admission.kind == "denied":
                return self._reject(request, "denied", admission.reason)
            if admission.kind == "error":
                return self._reject(request, "error", admission.reason)
            if admission.kind == "uncertain":
                result = self._finish(request, "uncertain", admission.reason, "")
                self.requests.complete(request, result.artifact_digest)
                return result
            # admission.kind == "execute": the pending record is durable now.
            try:
                timeout = min(request.timeout_seconds, self.config.budgets.max_timeout_seconds)
                limit = min(request.max_output_bytes, self.config.budgets.max_output_bytes)
                handler = {
                    "observe_target": self._observe_target,
                    "controller_state": self._controller_state,
                    "workspace": self._workspace,
                    "research": self._research,
                }[request.family]
                try:
                    status, reason, output = handler(request, timeout)
                except BrokerError as error:
                    status, reason, output = "error", error.reason, ""
                except Exception:
                    status, reason, output = "error", "execution-failed", ""
                result = self._finish(request, status, reason, output, limit=limit)
                self.requests.complete(request, result.artifact_digest)
                return result
            finally:
                self.requests.release(request)
        finally:
            self._slots.release()

    # -- shared mechanics -------------------------------------------------------

    def _screen_effects(self, request: BrokerRequest) -> str | None:
        """Typed effect enforcement against a closed vocabulary.

        Read-only families carry the read capability, workspace additionally
        the workspace-mutation capability. Every tenant class, every
        production/external/network/host class, and every unknown effect fails
        closed to the (future) approval path. Nothing here reads evidence
        text: only the declared, typed ``effects`` list is consulted.
        """
        granted = FAMILY_GRANTED_EFFECTS[request.family]
        for effect in request.effects:
            folded = effect.casefold()
            if folded == "tenant":
                return "tenant-approval-required"
            if folded in APPROVAL_REQUIRED_EFFECTS:
                return "effect-approval-required"
            if folded not in granted:
                return "effect-approval-required"
        return None

    def _reject(self, request: BrokerRequest, status: str, reason: str) -> BrokerResult:
        """A bounded rejection that persists nothing and carries no output.

        Only a request holding a durable admitted record may write evidence.
        A rejection decided before (or instead of) admission -- wrong machine,
        screened effects, concurrency pressure, an exhausted or conflicting
        request budget, an unreadable record -- must not be able to grow the
        evidence ledger, or a denial flood becomes an unbounded artifact
        stream that bypasses durable budget admission entirely. The decision
        is deterministic from durable state, so replaying the same rejected
        request recomputes the same answer without needing a stored artifact.
        """
        return BrokerResult(
            request_id=request.request_id,
            request_digest=request.digest(),
            task_id=request.task_id,
            machine_id=self.config.machine_id,
            family=request.family,
            operation=request.operation[:256],
            provenance=PROVENANCE[request.family],
            status=status,
            reason=reason,
            output="",
            truncated=False,
            artifact_digest=None,
        )

    def _finish(
        self,
        request: BrokerRequest,
        status: str,
        reason: str | None,
        output: str,
        *,
        limit: int | None = None,
    ) -> BrokerResult:
        bound = limit if limit is not None else self.config.budgets.max_output_bytes
        redacted = redact_secrets(output)
        encoded = redacted.encode("utf-8")
        truncated = len(encoded) > bound
        if truncated:
            encoded = encoded[:bound]
            while encoded:
                try:
                    redacted = encoded.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
            else:
                redacted = ""
        result = BrokerResult(
            request_id=request.request_id,
            request_digest=request.digest(),
            task_id=request.task_id,
            machine_id=self.config.machine_id,
            family=request.family,
            operation=request.operation[:256],
            provenance=PROVENANCE[request.family],
            status=status,
            reason=reason,
            output=redacted,
            truncated=truncated,
            artifact_digest=None,
        )
        digest = self.ledger.record(request.task_id, result.document())
        return BrokerResult(**{**result.__dict__, "artifact_digest": digest})

    def _replay(self, request: BrokerRequest, artifact_digest: str) -> BrokerResult:
        """Return the recorded result for a completed request without executing."""
        try:
            document = self.ledger.load(request.task_id, artifact_digest)
        except BrokerError as error:
            # The request is admitted, but its recorded artifact is missing or
            # corrupt. Reporting that failure must not mint new artifacts on
            # every retry, so the error result is bounded and non-persisted.
            return self._reject(request, "error", error.reason)
        try:
            return BrokerResult(
                request_id=str(document["request_id"]),
                request_digest=str(document["request_digest"]),
                task_id=str(document["task_id"]),
                machine_id=str(document["machine_id"]),
                family=str(document["family"]),
                operation=str(document["operation"]),
                provenance=str(document["provenance"]),
                status=str(document["status"]),
                reason=document["reason"] if document["reason"] is None else str(document["reason"]),
                output=str(document["output"]),
                truncated=bool(document["truncated"]),
                artifact_digest=artifact_digest,
                schema_version=int(document["schema_version"]),
                authority=str(document["authority"]),
                trust=str(document["trust"]),
            )
        except (KeyError, TypeError, ValueError):
            return self._reject(request, "error", "replay-artifact-invalid")

    _ADAPTER_FAILURE_REASONS = frozenset({"adapter-cancel-violation", "adapter-revoked"})

    def _call_adapter(
        self, adapter: Any, work: Callable[[], Any], timeout: float
    ) -> tuple[str, Any]:
        """Run one contract-checked adapter with a hard bound and real cancellation.

        On timeout the adapter's cancel() is demanded and the worker thread is
        joined within the grace period; an adapter that keeps running anyway
        broke its attested contract, so it is quarantined and every later call
        fails closed instead of piling up abandoned threads.
        """
        if id(adapter) in self._revoked_adapters:
            return ("error", "adapter-revoked")
        box: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                box.put(("ok", work()))
            except BrokerError as error:
                box.put(("error", error.reason))
            except Exception as error:  # Adapter internals stay opaque to the model.
                box.put(("error", type(error).__name__))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            try:
                adapter.cancel()
            except Exception:
                pass
            thread.join(self._cancel_grace_seconds)
            if thread.is_alive():
                self._revoked_adapters.add(id(adapter))
                return ("error", "adapter-cancel-violation")
            return ("timeout", None)
        try:
            return box.get_nowait()
        except queue.Empty:
            return ("error", "adapter-result-missing")

    # -- family handlers ---------------------------------------------------------

    def _observe_target(
        self, request: BrokerRequest, timeout: float
    ) -> tuple[str, str | None, str]:
        """Arbitrary bounded read-only observation through the injected adapter.

        The adapter is the same read-only, forced-command, tenant-walled
        channel ``observe_mcp`` uses. It takes only command text: there is no
        writable variant and no topic list reachable from here, so a new read
        needs no ``READ_TOPICS`` entry and cannot become a write.
        """
        adapter = self.observe_adapter
        if adapter is None:
            return ("error", "observe-unavailable", "")
        command = request.arguments.get("command")
        if command is None:
            command = request.operation
        if not isinstance(command, str) or not command.strip() or len(command) > MAX_OPERATION_CHARS:
            return ("error", "observe-command-invalid", "")
        arguments = request.arguments
        kind, value = self._call_adapter(
            adapter, lambda: adapter.invoke(command, arguments), timeout
        )
        if kind == "timeout":
            return ("timeout", "time-budget-exceeded", "")
        if kind == "error":
            reason = value if value in self._ADAPTER_FAILURE_REASONS else "observe-failed"
            return ("error", reason, "")
        return ("ok", None, value if isinstance(value, str) else str(value))

    def _controller_state(
        self, request: BrokerRequest, timeout: float
    ) -> tuple[str, str | None, str]:
        adapter = self.controller_state_adapter
        if adapter is None:
            return ("error", "controller-state-unavailable", "")
        arguments = request.arguments
        operation = request.operation
        kind, value = self._call_adapter(
            adapter, lambda: adapter.invoke(operation, arguments), timeout
        )
        if kind == "timeout":
            return ("timeout", "time-budget-exceeded", "")
        if kind == "error":
            reason = value if value in self._ADAPTER_FAILURE_REASONS else "controller-state-failed"
            return ("error", reason, "")
        try:
            sanitized = sanitize_controller_state(value)
        except BrokerError as error:
            return ("error", error.reason, "")
        return ("ok", None, json.dumps(sanitized, sort_keys=True, ensure_ascii=True))

    def _research(
        self, request: BrokerRequest, timeout: float
    ) -> tuple[str, str | None, str]:
        name = request.arguments.get("adapter")
        if not isinstance(name, str) or not name:
            name = request.operation
        adapter = self.research_adapters.get(name)
        if adapter is None:
            # The request itself was well-formed and expressible; the refusal is
            # environmental (no such adapter is deployed), not a schema wall.
            return ("error", "adapter-unavailable", "")
        arguments = request.arguments
        operation = request.operation
        kind, value = self._call_adapter(
            adapter, lambda: adapter.invoke(operation, arguments), timeout
        )
        if kind == "timeout":
            return ("timeout", "time-budget-exceeded", "")
        if kind == "error":
            reason = value if value in self._ADAPTER_FAILURE_REASONS else "adapter-failed"
            return ("error", reason, "")
        return ("ok", None, value if isinstance(value, str) else str(value))

    def _workspace(
        self, request: BrokerRequest, timeout: float
    ) -> tuple[str, str | None, str]:
        grant = self.workspaces.grant(request.task_id)
        if grant is None:
            return ("denied", "task-not-bound", "")
        operation = request.operation
        try:
            if operation == "run":
                return self._workspace_run(grant, request, timeout)
            if operation == "read_file":
                return self._workspace_read(grant, request)
            if operation == "write_file":
                return self._workspace_write(grant, request)
            if operation == "list_dir":
                return self._workspace_list(grant, request)
            if operation == "store_artifact":
                return self._workspace_artifact(grant, request)
        except BrokerError as error:
            reason = error.reason
            status = "denied" if reason == "workspace-containment" else "error"
            return (status, reason, "")
        # Any shell, git, dependency, test, build, or Nix command is already
        # expressible through "run"; this names a primitive, not a permission.
        return ("error", "workspace-operation-unsupported", "")

    def _workspace_run(
        self, grant: WorkspaceGrant, request: BrokerRequest, timeout: float
    ) -> tuple[str, str | None, str]:
        """Execute one workspace command through the attested sandbox runner.

        This process never spawns the command itself: without an injected
        runner attesting the exact isolation contract, run fails closed. The
        runner receives only the bound worktree, a scrubbed environment, the
        command, and its bounds, and returns an external-process handle. The
        broker enforces the deadline against that handle: at timeout it kills
        the work and requires confirmation that nothing survived before this
        request finishes and its concurrency slot is released. A runner whose
        work survives kill -- or whose handle cannot be controlled at all --
        is quarantined, so a misbehaving runner can fail at most once per
        broker instead of accumulating orphaned work or pinned capacity.
        """
        runner = self.sandbox_runner
        if runner is None:
            return ("denied", "sandbox-runner-unavailable", "")
        if id(runner) in self._quarantined_runners:
            return ("error", "sandbox-runner-quarantined", "")
        argv = request.arguments.get("argv")
        shell = request.arguments.get("shell")
        if argv is not None:
            if (
                not isinstance(argv, (list, tuple)) or not argv
                or any(not isinstance(item, str) or not item for item in argv)
                or len(argv) > 256
            ):
                return ("error", "workspace-argv-invalid", "")
            command = tuple(str(item) for item in argv)
        elif isinstance(shell, str) and shell.strip():
            command = ("/bin/sh", "-c", shell)
        else:
            return ("error", "workspace-command-missing", "")
        limit = min(request.max_output_bytes, self.config.budgets.max_output_bytes)
        sandbox_command = SandboxCommand(
            task_id=grant.task_id,
            task_root=grant.root,
            argv=command,
            environment=MappingProxyType(
                safe_subprocess_environment(grant.root, self.base_environment)
            ),
            timeout_seconds=timeout,
            max_output_bytes=limit,
        )
        try:
            handle = runner.start(sandbox_command)
        except BrokerError as error:
            return ("error", error.reason, "")
        except Exception:
            return ("error", "sandbox-start-failed", "")
        if not callable(getattr(handle, "wait", None)) or not callable(
            getattr(handle, "kill", None)
        ):
            # Work may already be running with no way to bound or destroy it:
            # the runner broke its attested contract and gets no further work.
            self._quarantined_runners.add(id(runner))
            return ("error", "sandbox-handle-invalid", "")
        try:
            outcome = handle.wait(timeout)
        except Exception:
            outcome = None
        if outcome is None:
            # Broker-side deadline: destroy the work and demand proof of death
            # before this slot is released. A confirmed kill is a timeout; an
            # unconfirmed one is a contract violation and quarantines the
            # runner so surviving work can never be joined by more.
            final = self._destroy_sandbox_work(runner, handle)
            if final is None:
                return ("error", "sandbox-kill-violation", "")
            return ("timeout", "time-budget-exceeded", final.output)
        if (
            not isinstance(outcome, SandboxOutcome)
            or outcome.status not in ("ok", "timeout", "error")
            or not isinstance(outcome.output, str)
        ):
            # Fail closed on a malformed claim, but only after making sure no
            # runner work survives the request.
            if self._destroy_sandbox_work(runner, handle) is None:
                return ("error", "sandbox-kill-violation", "")
            return ("error", "sandbox-outcome-invalid", "")
        if outcome.status == "timeout":
            return ("timeout", "time-budget-exceeded", outcome.output)
        if outcome.status == "error":
            return ("error", "sandbox-run-failed", outcome.output)
        return ("ok", None, f"exit={outcome.exit_code}\n{outcome.output}")

    def _destroy_sandbox_work(self, runner: Any, handle: Any) -> SandboxOutcome | None:
        """Kill dispatched sandbox work and confirm nothing survived.

        Returns the final outcome when the handle confirms the work is gone
        within the kill grace. Anything else -- kill or wait raising, no
        outcome inside the grace, an outcome that is not a well-formed
        ``SandboxOutcome`` -- means survival cannot be ruled out: the runner
        is quarantined and None is returned so the caller fails closed.
        """
        try:
            handle.kill()
            final = handle.wait(self._runner_kill_grace_seconds)
        except Exception:
            final = None
        if not isinstance(final, SandboxOutcome) or not isinstance(final.output, str):
            self._quarantined_runners.add(id(runner))
            return None
        return final

    def _workspace_read(
        self, grant: WorkspaceGrant, request: BrokerRequest
    ) -> tuple[str, str | None, str]:
        parts = _contained_parts(
            grant.root, request.arguments.get("path", ""), allow_root=False
        )
        with self._file_lock:
            root_fd = _open_task_root(grant.root)
            try:
                dir_fd = _walk_directories(root_fd, parts[:-1], create=False)
                try:
                    try:
                        handle = os.open(
                            parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC,
                            dir_fd=dir_fd,
                        )
                    except FileNotFoundError:
                        return ("error", "workspace-file-missing", "")
                    except OSError as error:
                        if error.errno in _ESCAPE_ERRNOS:
                            raise BrokerError("workspace-containment") from error
                        return ("error", "workspace-file-invalid", "")
                    try:
                        status = os.fstat(handle)
                        if not stat.S_ISREG(status.st_mode):
                            return ("error", "workspace-file-invalid", "")
                        if status.st_size > MAX_WORKSPACE_FILE_BYTES:
                            return ("error", "workspace-file-too-large", "")
                        chunks: list[bytes] = []
                        while True:
                            chunk = os.read(handle, 64 * 1024)
                            if not chunk:
                                break
                            chunks.append(chunk)
                    finally:
                        os.close(handle)
                finally:
                    if dir_fd != root_fd:
                        os.close(dir_fd)
            except FileNotFoundError:
                return ("error", "workspace-file-missing", "")
            finally:
                os.close(root_fd)
        return ("ok", None, b"".join(chunks).decode("utf-8", "replace"))

    def _workspace_write(
        self, grant: WorkspaceGrant, request: BrokerRequest
    ) -> tuple[str, str | None, str]:
        parts = _contained_parts(
            grant.root, request.arguments.get("path", ""), allow_root=False
        )
        content = request.arguments.get("content")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WORKSPACE_FILE_BYTES:
            return ("error", "workspace-content-invalid", "")
        with self._file_lock:
            root_fd = _open_task_root(grant.root)
            try:
                dir_fd = _walk_directories(root_fd, parts[:-1], create=True)
                try:
                    try:
                        handle = os.open(
                            parts[-1],
                            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW | _O_CLOEXEC,
                            0o600, dir_fd=dir_fd,
                        )
                    except OSError as error:
                        if error.errno in _ESCAPE_ERRNOS:
                            raise BrokerError("workspace-containment") from error
                        return ("error", "workspace-write-failed", "")
                    try:
                        os.write(handle, content.encode("utf-8"))
                        os.fsync(handle)
                    finally:
                        os.close(handle)
                    # File durability is not enough: until the containing
                    # directory is synced, the name itself can vanish in a
                    # crash. Success is only reported past this point.
                    os.fsync(dir_fd)
                finally:
                    if dir_fd != root_fd:
                        os.close(dir_fd)
            except FileNotFoundError:
                return ("error", "workspace-write-failed", "")
            finally:
                os.close(root_fd)
        return ("ok", None, f"wrote {'/'.join(parts)}")

    def _workspace_list(
        self, grant: WorkspaceGrant, request: BrokerRequest
    ) -> tuple[str, str | None, str]:
        candidate = request.arguments.get("path", ".")
        if candidate == ".":
            candidate = ""
        parts = _contained_parts(grant.root, candidate, allow_root=True)
        with self._file_lock:
            root_fd = _open_task_root(grant.root)
            try:
                try:
                    dir_fd = _walk_directories(root_fd, parts, create=False)
                except FileNotFoundError:
                    return ("error", "workspace-file-missing", "")
                except OSError as error:
                    if error.errno in _ESCAPE_ERRNOS:
                        raise BrokerError("workspace-containment") from error
                    return ("error", "workspace-file-invalid", "")
                try:
                    names = sorted(os.listdir(dir_fd))
                finally:
                    if dir_fd != root_fd:
                        os.close(dir_fd)
            finally:
                os.close(root_fd)
        return ("ok", None, "\n".join(names[:2048]))

    def _workspace_artifact(
        self, grant: WorkspaceGrant, request: BrokerRequest
    ) -> tuple[str, str | None, str]:
        content = request.arguments.get("content")
        if not isinstance(content, str):
            return ("error", "workspace-content-invalid", "")
        label = request.arguments.get("label")
        document = {
            "schema_version": BROKER_SCHEMA_VERSION,
            "kind": "task-artifact",
            "task_id": grant.task_id,
            "machine_id": self.config.machine_id,
            "label": label if isinstance(label, str) else "",
            "content": content,
        }
        digest = self.ledger.record(grant.task_id, document)
        return ("ok", None, f"artifact sha256:{digest}")


def sanitize_controller_state(value: Any, depth: int = 0) -> Any:
    """Controller state a model may read: credential-shaped fields never cross."""
    if depth > 16:
        raise BrokerError("controller-state-too-deep")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        if len(value) > 1024:
            raise BrokerError("controller-state-too-large")
        result: dict[str, Any] = {}
        dropped = 0
        for key, item in value.items():
            name = str(key)
            # A credential-shaped field is dropped whole, name included: keeping
            # the name would trip the line redactor and blank the whole document.
            if _SENSITIVE_LINE.search(name) or _credential_key(name):
                dropped += 1
                continue
            result[name] = sanitize_controller_state(item, depth + 1)
        if dropped:
            result["redacted_fields"] = dropped
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 4096:
            raise BrokerError("controller-state-too-large")
        return [sanitize_controller_state(item, depth + 1) for item in value]
    raise BrokerError("controller-state-invalid")


def build_capability_broker(
    *,
    environment: Mapping[str, str] | None = None,
    config: BrokerConfig | None = None,
    observe_adapter: Any | None = None,
    controller_state_adapter: Any | None = None,
    research_adapters: Mapping[str, Any] | None = None,
    sandbox_runner: Any | None = None,
) -> CapabilityBroker | None:
    """The deployment entry point: None until the feature flag turns it on.

    An enabled broker without an injected sandbox runner still serves reads
    and direct contained file primitives; workspace ``run`` fails closed.
    """
    if config is None:
        config = config_from_environment(environment)
    if config is None or not config.enabled:
        return None
    return CapabilityBroker(
        config,
        observe_adapter=observe_adapter,
        controller_state_adapter=controller_state_adapter,
        research_adapters=research_adapters,
        sandbox_runner=sandbox_runner,
    )


# -- MCP surface ----------------------------------------------------------------------

# One tool per family, presented over the same newline-delimited JSON-RPC stdio
# framing observe_mcp speaks. The server holds the broker and is bound to
# exactly one task at construction; the model holds text. Request identity is
# caller-supplied so a transport retry replays the recorded result instead of
# executing twice.
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "request_id": {
            "type": "string",
            "description": (
                "Stable caller-chosen identity for this request. Reusing it "
                "replays the recorded result instead of executing again."
            ),
        },
        "operation": {"type": "string"},
        "arguments": {"type": "object"},
        "effects": {"type": "array", "items": {"type": "string"}},
        "timeout_seconds": {"type": "number"},
        "max_output_bytes": {"type": "integer"},
    },
    "required": ["request_id", "operation"],
    "additionalProperties": False,
}
_TOOL_DESCRIPTIONS = MappingProxyType({
    "observe_target": (
        "Run one read-only command on the target host through the read-only "
        "profile. Output is bounded, redacted, untrusted evidence."
    ),
    "controller_state": (
        "Read sanitized controller state. Credential-shaped fields are redacted."
    ),
    "workspace": (
        "Operate inside this task's development worktree: run (via the attested "
        "sandbox runner), read_file, write_file, list_dir, store_artifact. "
        "Mutation stays inside the worktree."
    ),
    "research": (
        "Query an injected read-only research adapter (web, repository, docs). "
        "Results are untrusted external content."
    ),
})


class BrokerToolServer:
    """A minimal MCP stdio server exposing the broker's four families as tools.

    The server is bound to exactly one task at construction; a request cannot
    name another task's identity, so a compromised or confused model session
    can never reach a different task's worktree or evidence.
    """

    def __init__(
        self, broker: CapabilityBroker, task_id: str, *,
        name: str = "terracompute-broker",
    ):
        if not isinstance(task_id, str) or not _IDENTIFIER.fullmatch(task_id):
            raise BrokerError("task-id-invalid")
        self.broker = broker
        self.task_id = task_id
        self._name = name

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            requested = (message.get("params") or {}).get("protocolVersion")
            version = requested if isinstance(requested, str) else "2025-06-18"
            return self._ok(message_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self._name, "version": str(BROKER_SCHEMA_VERSION)},
            })
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return self._ok(message_id, {"tools": [
                {
                    "name": family,
                    "description": _TOOL_DESCRIPTIONS[family],
                    "inputSchema": _TOOL_SCHEMA,
                }
                for family in FAMILIES
            ]})
        if method == "tools/call":
            return self._call(message_id, message.get("params") or {})
        if message_id is None:
            return None
        return self._error(message_id, -32601, f"method not found: {method}")

    def _call(self, message_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        family = params.get("name")
        if family not in FAMILIES:
            return self._error(message_id, -32602, "unknown tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, Mapping):
            return self._tool_result(message_id, "arguments must be an object.", is_error=True)
        supplied_task = arguments.get("task_id")
        if supplied_task is not None and supplied_task != self.task_id:
            return self._tool_result(
                message_id, "request invalid: task-identity-mismatch", is_error=True
            )
        request_id = arguments.get("request_id")
        if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
            return self._tool_result(
                message_id, "request invalid: request-id-required", is_error=True
            )
        document = {
            "schema_version": BROKER_SCHEMA_VERSION,
            "request_id": request_id,
            "task_id": self.task_id,
            "machine_id": self.broker.config.machine_id,
            "family": family,
            "operation": arguments.get("operation"),
            "arguments": arguments.get("arguments") or {},
            "effects": arguments.get("effects") or [],
            "timeout_seconds": arguments.get(
                "timeout_seconds", self.broker.config.budgets.max_timeout_seconds
            ),
            "max_output_bytes": arguments.get(
                "max_output_bytes", self.broker.config.budgets.max_output_bytes
            ),
        }
        try:
            result = self.broker.handle(parse_broker_request(document))
        except BrokerError as error:
            return self._tool_result(message_id, f"request invalid: {error.reason}", is_error=True)
        return self._tool_result(
            message_id,
            json.dumps(result.document(), sort_keys=True, ensure_ascii=True),
            is_error=result.status not in {"ok"},
        )

    def _tool_result(self, message_id: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
        return self._ok(message_id, {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        })

    @staticmethod
    def _ok(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @staticmethod
    def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
