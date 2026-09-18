"""Standalone, bounded filesystem runtime for the Terracompute investigator.

The request spool is a deliberately small trust boundary.  Producers publish
sanitized JSON files into ``pending`` and this service publishes fixed-schema
JSON into the result spool.  This module has no network or action API of its
own; the only process boundary is the injected, pinned Codex App Server argv.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .investigator import (
    AppServerClient,
    InvestigationStore,
    Investigator,
    InvestigatorError,
    SubprocessJsonRpcTransport,
    private_codex_environment,
)


MACHINE_ID = "17049"
REQUEST_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 72 * 1024
MAX_REPORT_BYTES = 32 * 1024
MAX_SPOOL_ENTRIES = 128
MAX_ARGV_ENTRIES = 16
MAX_ARG_BYTES = 4096
DEFAULT_POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 60.0
MAX_RUN_ITERATIONS = 100_000

_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "machine_id",
        "incident_id",
        "evidence_hash",
        "severity",
        "prompt",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REASON = frozenset(
    {
        "unchanged-evidence",
        "astra-escalation-not-justified",
        "auth-or-quota-unavailable",
        "model-unavailable",
        "runtime-unavailable",
        "episode-closed",
        "lead-concurrency-cap",
        "episode-token-accounting-unavailable",
        "rolling-token-accounting-unavailable",
        "episode-turn-cap",
        "episode-token-cap",
        "rolling-turn-cap",
        "critical-reserve",
        "rolling-token-cap",
        "astra-cap",
        "turn-failed",
        "investigation-timeout",
        "investigation-timeout-execution-unknown",
        "runtime-failure-execution-unknown",
        "unknown-in-flight",
    }
)
_RESULT_STATUS = frozenset(
    {"completed", "unchanged", "unavailable", "rejected", "timeout", "interrupted", "failed"}
)
_SENSITIVE_LINE = re.compile(
    r"(?i)(authorization|bearer|api[-_ ]?key|password|passwd|credential|auth\.json|"
    r"access[-_ ]?token|refresh[-_ ]?token|client[-_ ]?secret|private[-_ ]?key)"
)
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_.-])(?:/[A-Za-z0-9_.~+@%:,=-]+)+")
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\s]+")
_TRAVERSAL = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9_=-]{32,}\b")
_SECRET_ARG = re.compile(
    r"(?i)(api[-_]?key|access[-_]?token|refresh[-_]?token|password|credential|bearer)"
)


class InvestigatorRuntimeError(ValueError):
    """A fixed-category local runtime failure safe to expose as metadata."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class InvestigatorRuntimeConfig:
    request_spool: Path
    result_spool: Path
    database_path: Path
    service_home: Path
    app_server_argv: tuple[str, ...]
    poll_seconds: float = DEFAULT_POLL_SECONDS
    turn_timeout_seconds: float = 600.0
    max_spool_entries: int = MAX_SPOOL_ENTRIES
    # The one other user whose requests are accepted, or None for none. A producer is
    # the only way work reaches this runtime from another service; without one the
    # runtime answers nobody but itself.
    producer_uid: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.request_spool,
            self.result_spool,
            self.database_path,
            self.service_home,
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise InvestigatorRuntimeError("configuration-path-not-absolute")
            if ".." in value.parts:
                raise InvestigatorRuntimeError("configuration-path-traversal")
        if (
            self.request_spool == self.result_spool
            or self.request_spool in self.result_spool.parents
            or self.result_spool in self.request_spool.parents
        ):
            raise InvestigatorRuntimeError("spool-roots-not-distinct")
        protected = (self.request_spool, self.result_spool, self.database_path)
        if any(
            self.service_home == path
            or self.service_home in path.parents
            or path in self.service_home.parents
            for path in protected
        ):
            raise InvestigatorRuntimeError("service-home-not-dedicated")
        argv = self.app_server_argv
        try:
            argv_too_large = any(len(arg.encode("utf-8")) > MAX_ARG_BYTES for arg in argv)
        except (AttributeError, TypeError, UnicodeEncodeError):
            argv_too_large = True
        if (
            not isinstance(argv, tuple)
            or not argv
            or len(argv) > MAX_ARGV_ENTRIES
            or any(not isinstance(arg, str) or not arg for arg in argv)
            or argv_too_large
            or any(_SECRET_ARG.search(arg) for arg in argv)
            or not Path(argv[0]).is_absolute()
            or len(argv) < 2
            or argv[1] != "app-server"
            or argv.count("app-server") != 1
        ):
            raise InvestigatorRuntimeError("app-server-argv-invalid")
        if (
            not isinstance(self.poll_seconds, (int, float))
            or isinstance(self.poll_seconds, bool)
            or not 0 < self.poll_seconds <= MAX_POLL_SECONDS
        ):
            raise InvestigatorRuntimeError("poll-bound-invalid")
        if (
            not isinstance(self.turn_timeout_seconds, (int, float))
            or isinstance(self.turn_timeout_seconds, bool)
            or not 0 < self.turn_timeout_seconds <= 600
        ):
            raise InvestigatorRuntimeError("turn-timeout-invalid")
        if (
            not isinstance(self.max_spool_entries, int)
            or isinstance(self.max_spool_entries, bool)
            or not 1 <= self.max_spool_entries <= MAX_SPOOL_ENTRIES
        ):
            raise InvestigatorRuntimeError("spool-entry-bound-invalid")
        if self.producer_uid is not None and (
            not isinstance(self.producer_uid, int)
            or isinstance(self.producer_uid, bool)
            # Never root (it needs no grant), never this runtime's own user (already
            # accepted, and naming it twice hides which grant is in force).
            or not 0 < self.producer_uid < 2**31
            or self.producer_uid == os.geteuid()
        ):
            raise InvestigatorRuntimeError("producer-uid-invalid")


@dataclass(frozen=True)
class IterationResult:
    state: str
    request_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _Request:
    request_id: str
    incident_id: str
    evidence_hash: str
    severity: str
    prompt: str


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvestigatorRuntimeError("clock-invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise InvestigatorRuntimeError("filesystem-symlink-rejected")


def _private_directory(path: Path, *, create: bool, group: int = 0) -> None:
    """This runtime's directory, with at most the named group bits granted.

    ``group`` is the only widening allowed, and only for the directories a producer
    must reach. Other users are never granted anything, the directory must still be
    owned by this runtime, and a directory carrying group bits it was not granted is
    rejected rather than narrowed.
    """
    _assert_no_symlink_components(path)
    if create:
        path.mkdir(mode=0o700 | group, parents=True, exist_ok=True)
    try:
        status = path.lstat()
    except OSError as error:
        raise InvestigatorRuntimeError("filesystem-unavailable") from error
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise InvestigatorRuntimeError("filesystem-directory-invalid")
    if (
        status.st_mode & 0o007
        or status.st_mode & 0o070 & ~group
        or status.st_uid != os.geteuid()
    ):
        raise InvestigatorRuntimeError("filesystem-permissions-invalid")


def _reachable_by_others(path: Path, boundary: Path) -> None:
    """Every directory from `boundary` down to `path` must let a non-owner through.

    A grant on a spool directory is worthless if something above it shuts the producer
    out, and the failure is silent: requests never arrive, and diagnosis falls back for
    a reason nobody can see. This turns that into a refusal to start.

    The walk stops at `boundary` -- the root this runtime's deployment lays out. What
    is above that belongs to the operating system, and demanding anything of it would
    be this runtime overreaching.
    """
    current = path
    while True:
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            raise InvestigatorRuntimeError("filesystem-unavailable") from error
        if not stat.S_ISDIR(mode) or not mode & 0o011:
            raise InvestigatorRuntimeError("producer-path-unreachable")
        if current == boundary or current.parent == current:
            return
        current = current.parent


def _canonical_json(document: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii") + b"\n"
    except (TypeError, ValueError, RecursionError) as error:
        raise InvestigatorRuntimeError("result-encoding-failed") from error


def _atomic_write(
    directory: Path, name: str, document: Mapping[str, object], mode: int = 0o600
) -> None:
    encoded = _canonical_json(document)
    if len(encoded) > MAX_REPORT_BYTES + 4096:
        raise InvestigatorRuntimeError("result-size-limit")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    file_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_fd = os.open(temporary, flags, mode, dir_fd=directory_fd)
        # O_CREAT's mode is masked by the umask, and a result a producer cannot read
        # is a result it will wait for forever.
        os.fchmod(file_fd, mode)
        view = memoryview(encoded)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as error:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise InvestigatorRuntimeError("result-publication-failed") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _pairs_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvestigatorRuntimeError("request-schema-invalid")
        result[key] = value
    return result


def _parse_request(claims: Path, name: str, owners: frozenset[int]) -> _Request:
    directory_fd = os.open(claims, os.O_RDONLY | os.O_DIRECTORY)
    file_fd: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_fd = os.open(name, flags, dir_fd=directory_fd)
        status = os.fstat(file_fd)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid not in owners
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            raise InvestigatorRuntimeError("request-file-invalid")
        if status.st_size > MAX_REQUEST_BYTES:
            raise InvestigatorRuntimeError("request-size-limit")
        pieces: list[bytes] = []
        remaining = MAX_REQUEST_BYTES + 1
        while remaining:
            piece = os.read(file_fd, remaining)
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        encoded = b"".join(pieces)
        if len(encoded) > MAX_REQUEST_BYTES:
            raise InvestigatorRuntimeError("request-size-limit")
    except InvestigatorRuntimeError:
        raise
    except OSError as error:
        raise InvestigatorRuntimeError("request-file-invalid") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)
    try:
        document = json.loads(encoded, object_pairs_hook=_pairs_no_duplicates)
    except InvestigatorRuntimeError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise InvestigatorRuntimeError("request-json-invalid") from error
    if not isinstance(document, dict) or set(document) != _REQUEST_KEYS:
        raise InvestigatorRuntimeError("request-schema-invalid")
    if (
        not isinstance(document["schema_version"], int)
        or isinstance(document["schema_version"], bool)
        or document["schema_version"] != REQUEST_SCHEMA_VERSION
    ):
        raise InvestigatorRuntimeError("request-schema-invalid")
    request_id = document["request_id"]
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise InvestigatorRuntimeError("request-schema-invalid")
    if name != f"{request_id}.json":
        raise InvestigatorRuntimeError("request-name-mismatch")
    if document["machine_id"] != MACHINE_ID:
        raise InvestigatorRuntimeError("request-target-mismatch")
    incident_id = document["incident_id"]
    evidence_hash = document["evidence_hash"]
    severity = document["severity"]
    prompt = document["prompt"]
    if not isinstance(incident_id, str) or not _IDENTIFIER.fullmatch(incident_id):
        raise InvestigatorRuntimeError("request-schema-invalid")
    if not isinstance(evidence_hash, str) or not _SHA256.fullmatch(evidence_hash):
        raise InvestigatorRuntimeError("request-schema-invalid")
    if not isinstance(severity, str) or severity not in {"info", "warning", "error", "critical"}:
        raise InvestigatorRuntimeError("request-schema-invalid")
    try:
        prompt_size = len(prompt.encode("utf-8")) if isinstance(prompt, str) else -1
    except UnicodeEncodeError:
        prompt_size = -1
    if not isinstance(prompt, str) or not prompt.strip() or not 0 <= prompt_size <= 64 * 1024 or "\x00" in prompt:
        raise InvestigatorRuntimeError("request-schema-invalid")
    return _Request(request_id, incident_id, evidence_hash, severity, prompt)


def _sanitize_report(text: object, prompt: str) -> str:
    if not isinstance(text, str):
        return ""
    cleaned = text.replace(prompt, "[request-redacted]") if prompt else text
    safe_lines: list[str] = []
    for line in cleaned.splitlines():
        line = "".join(character for character in line if character >= " " or character == "\t")
        if _SENSITIVE_LINE.search(line):
            safe_lines.append("[sensitive-content-redacted]")
            continue
        line = _WINDOWS_PATH.sub("[path-redacted]", line)
        line = _ABSOLUTE_PATH.sub("[path-redacted]", line)
        line = _HIGH_ENTROPY.sub("[opaque-value-redacted]", line)
        if _TRAVERSAL.search(line):
            line = "[path-redacted]"
        safe_lines.append(line)
    result = "\n".join(safe_lines).strip()
    encoded = result.encode("utf-8")[:MAX_REPORT_BYTES]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


class InvestigatorRuntime:
    """Own one lead at a time and bridge bounded request/result spools."""

    def __init__(
        self,
        config: InvestigatorRuntimeConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        transport_factory: Callable[..., Any] = SubprocessJsonRpcTransport,
    ):
        self.config = config
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.transport_factory = transport_factory
        self.pending = config.request_spool / "pending"
        self.claims = config.request_spool / "claimed"
        self.completed = config.result_spool / "completed"
        self.quarantine = config.result_spool / "quarantine"
        # A request may be owned by this runtime, or by the one configured producer.
        self.owners = frozenset(
            {os.geteuid()}
            | ({config.producer_uid} if config.producer_uid is not None else set())
        )
        producer = config.producer_uid is not None
        # The grant a producer needs and nothing more: traverse the two roots, create
        # a request in pending, read and consume its own answer in completed. It never
        # reaches claimed work, the quarantine, the database or the service home.
        for directory, group, boundary in (
            (config.request_spool, 0o010 if producer else 0, config.request_spool.parent),
            (self.pending, 0o030 if producer else 0, config.request_spool.parent),
            (self.claims, 0, None),
            (config.result_spool, 0o010 if producer else 0, config.result_spool.parent),
            (self.completed, 0o070 if producer else 0, config.result_spool.parent),
            (self.quarantine, 0, None),
            (config.database_path.parent, 0, None),
        ):
            _private_directory(directory, create=True, group=group)
            if producer and group and boundary is not None:
                # The grant has to be reachable, not merely present.
                _reachable_by_others(directory, boundary)
        _private_directory(config.service_home, create=False)
        self.result_mode = 0o640 if producer else 0o600
        try:
            database_status = config.database_path.lstat()
        except FileNotFoundError:
            database_status = None
        except OSError as error:
            raise InvestigatorRuntimeError("filesystem-unavailable") from error
        if database_status is not None and (
            not stat.S_ISREG(database_status.st_mode)
            or stat.S_ISLNK(database_status.st_mode)
            or database_status.st_nlink != 1
        ):
            raise InvestigatorRuntimeError("database-path-invalid")
        self._lock_path = config.request_spool / ".investigator.lock"

    def _acquire_lock(self) -> int | None:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(self._lock_path, flags, 0o600)
            status = os.fstat(file_fd)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                os.close(file_fd)
                raise InvestigatorRuntimeError("claim-lock-invalid")
            fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return file_fd
        except BlockingIOError:
            try:
                os.close(file_fd)
            except UnboundLocalError:
                pass
            return None
        except OSError as error:
            raise InvestigatorRuntimeError("claim-lock-unavailable") from error

    def _entries(self, directory: Path) -> list[os.DirEntry[str]]:
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as error:
            raise InvestigatorRuntimeError("spool-unavailable") from error
        if len(entries) > self.config.max_spool_entries:
            raise InvestigatorRuntimeError("spool-entry-limit")
        return sorted(entries, key=lambda item: item.name)

    def _quarantine_document(self, name: str, reason: str) -> None:
        key = hashlib.sha256(name.encode("utf-8", "surrogateescape")).hexdigest()
        document = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "machine_id": MACHINE_ID,
            "status": "quarantined",
            "reason": reason,
            "request_key": key,
            "completed_utc": _utc_text(self.clock()),
        }
        _atomic_write(self.quarantine, f"{key}.json", document)

    @staticmethod
    def _remove(directory: Path, name: str) -> None:
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise InvestigatorRuntimeError("spool-consume-failed") from error
        finally:
            os.close(directory_fd)

    def _next_claim(self) -> str | IterationResult | None:
        if self.config.producer_uid is not None:
            # Answers are the producer's to consume. If it stops, stop taking work
            # rather than filling the spool: the bound is backpressure, not deletion.
            self._entries(self.completed)
        claimed = self._entries(self.claims)
        if claimed:
            entry = claimed[0]
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                self._quarantine_document(entry.name, "request-file-invalid")
                if entry.is_symlink():
                    self._remove(self.claims, entry.name)
                return IterationResult("quarantined", reason="request-file-invalid")
            return entry.name
        pending = self._entries(self.pending)
        if not pending:
            return None
        entry = pending[0]
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            self._quarantine_document(entry.name, "request-file-invalid")
            if entry.is_symlink():
                self._remove(self.pending, entry.name)
            return IterationResult("quarantined", reason="request-file-invalid")
        name = entry.name
        if not name.endswith(".json") or not _REQUEST_ID.fullmatch(name[:-5]):
            self._quarantine_document(name, "request-name-invalid")
            self._remove(self.pending, name)
            return IterationResult("quarantined", reason="request-name-invalid")
        pending_fd = os.open(self.pending, os.O_RDONLY | os.O_DIRECTORY)
        claims_fd = os.open(self.claims, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.rename(name, name, src_dir_fd=pending_fd, dst_dir_fd=claims_fd)
            os.fsync(pending_fd)
            os.fsync(claims_fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise InvestigatorRuntimeError("request-claim-failed") from error
        finally:
            os.close(pending_fd)
            os.close(claims_fd)
        return name

    @staticmethod
    def _unknown_in_flight(store: InvestigationStore) -> bool:
        row = store.db.execute(
            "SELECT 1 FROM terracompute_investigation_turns WHERE status='in_flight' LIMIT 1"
        ).fetchone()
        return row is not None

    def _result_document(
        self,
        request: _Request,
        *,
        status: str,
        reason: str | None,
        report: object = "",
        episode_id: int | None = None,
        reported_tokens: int | None = None,
        overshoot_tokens: int = 0,
    ) -> dict[str, object]:
        safe_status = status if status in _RESULT_STATUS else "unavailable"
        safe_reason = reason if reason in _SAFE_REASON else ("runtime-unavailable" if reason else None)
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_id": request.request_id,
            "machine_id": MACHINE_ID,
            "incident_id": request.incident_id,
            "evidence_hash": request.evidence_hash,
            "severity": request.severity,
            "status": safe_status,
            "reason": safe_reason,
            "episode_id": episode_id if isinstance(episode_id, int) and episode_id > 0 else None,
            "reported_tokens": reported_tokens
            if isinstance(reported_tokens, int) and reported_tokens >= 0
            else None,
            "overshoot_tokens": overshoot_tokens
            if isinstance(overshoot_tokens, int) and overshoot_tokens >= 0
            else 0,
            "report": _sanitize_report(report, request.prompt),
            "completed_utc": _utc_text(self.clock()),
        }

    def _publish(self, request: _Request, document: Mapping[str, object]) -> None:
        _atomic_write(
            self.completed, f"{request.request_id}.json", document, self.result_mode
        )

    def _run_claim(self, name: str) -> IterationResult:
        try:
            request = _parse_request(self.claims, name, self.owners)
        except InvestigatorRuntimeError as error:
            self._quarantine_document(name, error.reason)
            self._remove(self.claims, name)
            return IterationResult("quarantined", reason=error.reason)
        result_path = self.completed / f"{request.request_id}.json"
        try:
            status = result_path.lstat()
        except FileNotFoundError:
            status = None
        if status is not None:
            if (
                not stat.S_ISREG(status.st_mode)
                or stat.S_ISLNK(status.st_mode)
                or status.st_nlink != 1
            ):
                raise InvestigatorRuntimeError("result-path-invalid")
            self._remove(self.claims, name)
            return IterationResult("idempotent", request.request_id, "result-already-published")

        store: InvestigationStore | None = None
        client: AppServerClient | None = None
        try:
            store = InvestigationStore(self.config.database_path)
            os.chmod(self.config.database_path, 0o600)
            database_status = self.config.database_path.lstat()
            if (
                not stat.S_ISREG(database_status.st_mode)
                or stat.S_ISLNK(database_status.st_mode)
                or database_status.st_nlink != 1
                or database_status.st_uid != os.geteuid()
                or stat.S_IMODE(database_status.st_mode) != 0o600
            ):
                raise InvestigatorRuntimeError("database-permissions-invalid")
            if self._unknown_in_flight(store):
                document = self._result_document(
                    request, status="unavailable", reason="unknown-in-flight"
                )
            else:
                environment = private_codex_environment(self.config.service_home)
                transport = self.transport_factory(
                    self.config.app_server_argv,
                    environment=environment,
                )
                client = AppServerClient(transport, clock=self.monotonic)
                try:
                    client.initialize(timeout=min(30.0, self.config.turn_timeout_seconds))
                    investigator = Investigator(
                        client,
                        store,
                        now=self.clock,
                        native_helpers_verified=False,
                    )
                    result = investigator.investigate(
                        request.incident_id,
                        request.evidence_hash,
                        request.prompt,
                        severity=request.severity,
                        timeout=self.config.turn_timeout_seconds,
                    )
                    document = self._result_document(
                        request,
                        status=result.status,
                        reason=result.reason,
                        report=result.text,
                        episode_id=result.episode_id,
                        reported_tokens=result.reported_tokens,
                        overshoot_tokens=result.overshoot_tokens,
                    )
                except (InvestigatorError, TimeoutError, OSError, ValueError):
                    document = self._result_document(
                        request, status="unavailable", reason="runtime-unavailable"
                    )
            self._publish(request, document)
            self._remove(self.claims, name)
            return IterationResult(str(document["status"]), request.request_id, document["reason"])
        except InvestigatorRuntimeError:
            raise
        except Exception:
            document = self._result_document(
                request, status="unavailable", reason="runtime-unavailable"
            )
            self._publish(request, document)
            self._remove(self.claims, name)
            return IterationResult("unavailable", request.request_id, "runtime-unavailable")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            if store is not None:
                store.close()

    def run_iteration(self) -> IterationResult:
        """Process at most one request without sleeping."""
        lock_fd = self._acquire_lock()
        if lock_fd is None:
            return IterationResult("busy", reason="lead-owned")
        try:
            name = self._next_claim()
            if name is None:
                return IterationResult("idle")
            if isinstance(name, IterationResult):
                return name
            return self._run_claim(name)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def run(self, iterations: int, *, stop: Callable[[], bool] | None = None) -> int:
        """Run a bounded fixed-cadence polling loop and return iterations run."""
        if not isinstance(iterations, int) or isinstance(iterations, bool) or not 1 <= iterations <= MAX_RUN_ITERATIONS:
            raise InvestigatorRuntimeError("iteration-bound-invalid")
        completed = 0
        for index in range(iterations):
            if stop is not None and stop():
                break
            self.run_iteration()
            completed += 1
            if index + 1 < iterations and (stop is None or not stop()):
                self.sleeper(self.config.poll_seconds)
        return completed


def poll_iteration(
    config: InvestigatorRuntimeConfig,
    **kwargs: Any,
) -> IterationResult:
    """Construct a runtime and execute exactly one non-sleeping iteration."""
    return InvestigatorRuntime(config, **kwargs).run_iteration()


def run_loop(
    config: InvestigatorRuntimeConfig,
    iterations: int,
    **kwargs: Any,
) -> int:
    """Construct a runtime and execute the explicitly bounded service loop."""
    return InvestigatorRuntime(config, **kwargs).run(iterations)
