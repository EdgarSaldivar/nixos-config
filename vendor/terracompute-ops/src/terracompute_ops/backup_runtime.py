"""Bounded restic transport for already-consistent LOCAL backup artifacts.

The process boundary is deliberately narrow: this module can add one snapshot,
list it for verification, and construct read-only check/local-restore commands.
It has no repository initialization, forget, prune, or deletion operation.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .backup import BackupResult, ValidationResult
from .maintenance import create_local_snapshot, verify_local_snapshot


MACHINE_ID = "17049"
DEFAULT_OUTPUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_MINIMUM_QUOTA_FREE_BYTES = 5 * 1024**3
RETENTION_COUNTS = MappingProxyType({
    "hourly": 48,
    "daily": 30,
    "weekly": 12,
    "monthly": 12,
    "yearly": 5,
})
BASE_TAG = "terracompute-ops"
MACHINE_TAG = f"machine:{MACHINE_ID}"
_HEX_ID = re.compile(r"^[0-9a-f]{64}$")
_SAFE_BACKUP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# Local snapshot directories named from their creation time, e.g. 20260916T164818439733Z.
_GENERATED_BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6,12}Z$")
# Full local copies kept after each is verified in the repository. Every hourly
# snapshot is a whole copy of the state database, so they are not kept forever.
LOCAL_SNAPSHOTS_KEPT = 6
_SAFE_TRANSPORT_PATH = re.compile(r"^/[A-Za-z0-9._+/@%=-]+$")
_SFTP_REPOSITORY = re.compile(
    r"^sftp:([A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*):"
    r"(/[A-Za-z0-9_./-]+)$"
)
_LEAKAGE = re.compile(
    r"(?i)(?:password|passwd|token|secret|credential|api[_-]?key)\s*(?:=|:)"
)
_BACKUP_MESSAGES = frozenset({"status", "verbose_status", "summary", "error"})
_SNAPSHOT_FIELDS = frozenset(
    {
        "excludes",
        "gid",
        "hostname",
        "id",
        "original",
        "parent",
        "paths",
        "program_version",
        "short_id",
        "summary",
        "tags",
        "time",
        "tree",
        "uid",
        "username",
    }
)
_PROTECTED_TAGS = frozenset(
    {
        "open-incident",
        "incident:open",
        "hardware",
        "hardware-change",
        "pre-action",
    }
)


class BackupRuntimeError(ValueError):
    """A fail-closed runtime check failed without exposing configured values."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ResticConfig:
    """Nonsecret, fixed restic process and repository configuration."""

    executable: Path
    repository: str
    credential_file: Path
    ssh_executable: Path
    ssh_identity_file: Path
    ssh_known_hosts_file: Path
    repository_mount: Path | None
    repository_quota_bytes: int
    lock_file: Path
    minimum_quota_free_bytes: int = DEFAULT_MINIMUM_QUOTA_FREE_BYTES
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES
    cleanup_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "executable",
            "credential_file",
            "ssh_executable",
            "ssh_identity_file",
            "ssh_known_hosts_file",
            "lock_file",
        ):
            value = Path(getattr(self, name))
            object.__setattr__(self, name, value)
            if not value.is_absolute():
                raise BackupRuntimeError("config_path_not_absolute")
        for name in ("ssh_executable", "ssh_identity_file", "ssh_known_hosts_file"):
            value = getattr(self, name)
            if ".." in value.parts or _SAFE_TRANSPORT_PATH.fullmatch(str(value)) is None:
                raise BackupRuntimeError("ssh_transport_path_invalid")
        if self.repository_mount is not None:
            mount = Path(self.repository_mount)
            object.__setattr__(self, "repository_mount", mount)
            if not mount.is_absolute():
                raise BackupRuntimeError("config_path_not_absolute")
        if (
            not isinstance(self.repository, str)
            or not self.repository
            or len(self.repository) > 1024
            or self.repository.startswith("-")
            or any(character.isspace() or ord(character) < 32 for character in self.repository)
            or _LEAKAGE.search(self.repository)
        ):
            raise BackupRuntimeError("repository_identity_invalid")
        parsed = urlsplit(self.repository) if "://" in self.repository else None
        if parsed is not None and (
            parsed.password is not None or parsed.query or parsed.fragment
        ):
            raise BackupRuntimeError("credential_shaped_repository")
        # Restic's native SFTP form is ``sftp:user@host:/path`` rather than a
        # URL, so urlsplit cannot identify an inline password in its userinfo.
        # A colon before the first ``@`` is therefore always credential-shaped.
        repository_body = self.repository.split(":", 1)[1] if ":" in self.repository else ""
        if "@" in repository_body and ":" in repository_body.split("@", 1)[0]:
            raise BackupRuntimeError("credential_shaped_repository")
        sftp_repository = _SFTP_REPOSITORY.fullmatch(self.repository)
        if sftp_repository is None or ".." in Path(sftp_repository.group(2)).parts:
            raise BackupRuntimeError("repository_identity_invalid")
        counts = (
            self.repository_quota_bytes,
            self.minimum_quota_free_bytes,
            self.output_limit_bytes,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in counts
        ):
            raise BackupRuntimeError("config_limit_invalid")
        if self.output_limit_bytes > DEFAULT_OUTPUT_LIMIT_BYTES:
            raise BackupRuntimeError("config_limit_invalid")
        if self.minimum_quota_free_bytes > self.repository_quota_bytes:
            raise BackupRuntimeError("config_quota_invalid")
        if (
            not isinstance(self.cleanup_grace_seconds, (int, float))
            or isinstance(self.cleanup_grace_seconds, bool)
            or not 0 < self.cleanup_grace_seconds <= 5
        ):
            raise BackupRuntimeError("config_cleanup_grace_invalid")


@dataclass(frozen=True)
class StoragePreflight:
    mounted: bool
    quota_bytes: int | None
    free_bytes: int | None


@dataclass(frozen=True)
class RepositorySnapshot:
    snapshot_id: str
    time: datetime
    paths: tuple[str, ...]
    tags: tuple[str, ...]
    hostname: str | None = None


@dataclass(frozen=True)
class BackupRunResult:
    success: bool
    machine_id: str
    backup_id: str
    local_snapshot: Path
    repository_snapshot_id: str
    local_verification: str
    repository_verification: str
    # Older local snapshots removed after this one reached the repository.
    pruned_local_snapshots: int = 0


@dataclass(frozen=True)
class RetentionDecision:
    snapshot_id: str
    snapshot_time: datetime
    retain: bool
    protected: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RetentionPlan:
    machine_id: str
    policy: Mapping[str, int]
    decisions: tuple[RetentionDecision, ...]

    @property
    def retained_ids(self) -> tuple[str, ...]:
        return tuple(item.snapshot_id for item in self.decisions if item.retain)

    @property
    def unretained_ids(self) -> tuple[str, ...]:
        return tuple(item.snapshot_id for item in self.decisions if not item.retain)


def _validate_machine(machine_id: str) -> None:
    if str(machine_id) != MACHINE_ID:
        raise BackupRuntimeError("wrong_target")


def _validate_deadline(deadline: datetime, clock: Callable[[], datetime]) -> float:
    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        raise BackupRuntimeError("deadline_invalid")
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise BackupRuntimeError("clock_invalid")
    remaining = (deadline.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise BackupRuntimeError("deadline_exceeded")
    return remaining


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not isinstance(snapshot_id, str) or not _HEX_ID.fullmatch(snapshot_id):
        raise BackupRuntimeError("snapshot_id_invalid")
    return snapshot_id


def _validate_backup_id(backup_id: str) -> str:
    if not isinstance(backup_id, str) or not _SAFE_BACKUP_ID.fullmatch(backup_id):
        raise BackupRuntimeError("backup_id_invalid")
    return backup_id


def _credential_file_is_private(path: Path) -> bool:
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        # systemd copies LoadCredential files into a private mount and makes
        # them readable by the service identity. Accept that owner as well as
        # a traditional root-owned credential file.
        and status.st_uid in {0, os.geteuid()}
        # NixOS systemd credentials are root-owned 0440 files inside a
        # service-private mount. Permit only the group-read bit used there.
        and stat.S_IMODE(status.st_mode) & 0o037 == 0
        and status.st_nlink == 1
        and status.st_size > 0
    )


def _executable_is_exact_file(path: Path) -> bool:
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        and bool(stat.S_IMODE(status.st_mode) & 0o111)
    )


def _sftp_transport_option(config: ResticConfig) -> str:
    match = _SFTP_REPOSITORY.fullmatch(config.repository)
    if match is None:
        raise BackupRuntimeError("repository_identity_invalid")
    endpoint = match.group(1)
    command = (
        str(config.ssh_executable),
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={config.ssh_known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-i",
        str(config.ssh_identity_file),
        endpoint,
        "-s",
        "sftp",
    )
    # Each dynamic value is restricted to one shellword above. Restic parses
    # this option directly into exec arguments; no shell or PATH lookup is used.
    return "sftp.command=" + " ".join(command)


def _restic_command_prefix(config: ResticConfig) -> tuple[str, ...]:
    return (str(config.executable), "--option", _sftp_transport_option(config))


def default_storage_preflight(config: ResticConfig) -> StoragePreflight:
    """Read local mount and free-space state; it performs no repository call."""
    mount = config.repository_mount
    if mount is None:
        return StoragePreflight(False, None, None)
    if mount.is_symlink() or not mount.is_dir() or not os.path.ismount(mount):
        return StoragePreflight(False, None, None)
    usage = shutil.disk_usage(mount)
    if usage.total != config.repository_quota_bytes:
        return StoragePreflight(True, None, usage.free)
    return StoragePreflight(True, config.repository_quota_bytes, usage.free)


def build_backup_command(
    config: ResticConfig, local_snapshot: Path, backup_id: str
) -> tuple[str, ...]:
    path = Path(local_snapshot)
    if not path.is_absolute():
        raise BackupRuntimeError("snapshot_path_not_absolute")
    safe_id = _validate_backup_id(backup_id)
    return _restic_command_prefix(config) + (
        "backup",
        "--json",
        "--tag",
        BASE_TAG,
        "--tag",
        MACHINE_TAG,
        "--tag",
        f"backup:{safe_id}",
        str(path),
    )


def build_snapshot_verification_command(
    config: ResticConfig, snapshot_id: str
) -> tuple[str, ...]:
    return _restic_command_prefix(config) + (
        "snapshots",
        "--json",
        _validate_snapshot_id(snapshot_id),
    )


def build_check_command(config: ResticConfig) -> tuple[str, ...]:
    """Construct a read-only repository check; execution is intentionally external."""
    return _restic_command_prefix(config) + ("check", "--json")


def build_restore_verification_command(
    config: ResticConfig, snapshot_id: str, destination: Path
) -> tuple[str, ...]:
    """Construct a local verified restore without executing it."""
    target = Path(destination)
    if not target.is_absolute():
        raise BackupRuntimeError("restore_path_not_absolute")
    return _restic_command_prefix(config) + (
        "restore",
        _validate_snapshot_id(snapshot_id),
        "--target",
        str(target),
        "--verify",
    )


def command_environment(config: ResticConfig) -> dict[str, str]:
    """Return the entire child environment; no parent variables are inherited."""
    return {
        "LC_ALL": "C",
        "RESTIC_PASSWORD_FILE": str(config.credential_file),
        "RESTIC_REPOSITORY": config.repository,
    }


def _validate_executable_command(config: ResticConfig, argv: Sequence[str]) -> None:
    command = tuple(argv)
    prefix = _restic_command_prefix(config)
    backup_shape = (
        len(command) == 12
        and command[:10]
        == (
            *prefix,
            "backup",
            "--json",
            "--tag",
            BASE_TAG,
            "--tag",
            MACHINE_TAG,
            "--tag",
        )
        and command[10].startswith("backup:")
        and _SAFE_BACKUP_ID.fullmatch(command[10][len("backup:") :]) is not None
        and Path(command[11]).is_absolute()
    )
    snapshot_shape = (
        len(command) == 6
        and command[:5] == (*prefix, "snapshots", "--json")
        and _HEX_ID.fullmatch(command[5]) is not None
    )
    if not backup_shape and not snapshot_shape:
        raise BackupRuntimeError("restic_command_not_allowed")


class _CappedOutput:
    def __init__(self, limit: int, on_overflow: Callable[[], None]):
        self._limit = limit
        self._on_overflow = on_overflow
        self._size = 0
        self._values: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        self._lock = threading.Lock()
        self.overflow = threading.Event()

    def read(self, name: str, stream: io.BufferedIOBase | None) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    chunk = str(chunk).encode("utf-8", "replace")
                first_overflow = False
                with self._lock:
                    available = max(0, self._limit - self._size)
                    self._values[name].extend(chunk[:available])
                    self._size += min(len(chunk), available)
                    if len(chunk) > available:
                        first_overflow = not self.overflow.is_set()
                        self.overflow.set()
                if first_overflow:
                    self._on_overflow()
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def value(self, name: str) -> bytes:
        return bytes(self._values[name])


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass


def _reject_json_constant(_value: str) -> None:
    raise ValueError("nonstandard JSON constant")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _load_json(value: str | bytes) -> object:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object,
    )


def _execute_command(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    deadline: datetime,
    clock: Callable[[], datetime],
    output_limit: int,
    cleanup_grace: float,
    popen_factory: Callable[..., subprocess.Popen[bytes]],
) -> bytes:
    remaining = _validate_deadline(deadline, clock)
    try:
        process = popen_factory(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise BackupRuntimeError("restic_spawn_failed") from None
    output = _CappedOutput(output_limit, lambda: _kill_process(process))
    threads = tuple(
        threading.Thread(target=output.read, args=(name, stream), daemon=True)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    )
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process(process)
    except Exception:
        _kill_process(process)
        try:
            process.wait(timeout=cleanup_grace)
        except Exception:
            pass
        raise BackupRuntimeError("restic_wait_failed") from None
    if output.overflow.is_set() and process.poll() is None:
        _kill_process(process)
    if timed_out or process.poll() is None:
        try:
            process.wait(timeout=cleanup_grace)
        except subprocess.TimeoutExpired as exc:
            raise BackupRuntimeError("process_cleanup_failed") from exc
    for thread in threads:
        thread.join(timeout=cleanup_grace)
    if any(thread.is_alive() for thread in threads):
        _kill_process(process)
        for thread in threads:
            thread.join(timeout=cleanup_grace)
        if any(thread.is_alive() for thread in threads):
            raise BackupRuntimeError("process_cleanup_failed")
    if timed_out:
        raise BackupRuntimeError("deadline_exceeded")
    if output.overflow.is_set():
        raise BackupRuntimeError("restic_output_exceeded")
    if process.returncode != 0:
        raise BackupRuntimeError("restic_command_failed")
    return output.value("stdout")


def parse_backup_json(raw: bytes) -> str:
    """Parse restic's JSON-lines backup output and return its snapshot id."""
    if not isinstance(raw, bytes) or len(raw) > DEFAULT_OUTPUT_LIMIT_BYTES:
        raise BackupRuntimeError("restic_output_exceeded")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupRuntimeError("restic_json_malformed") from exc
    summary: str | None = None
    saw_record = False
    for line in text.splitlines():
        if not line.strip():
            continue
        saw_record = True
        try:
            value = _load_json(line)
        except (ValueError, json.JSONDecodeError) as exc:
            raise BackupRuntimeError("restic_json_malformed") from exc
        if not isinstance(value, dict) or value.get("message_type") not in _BACKUP_MESSAGES:
            raise BackupRuntimeError("restic_json_unknown")
        message_type = value["message_type"]
        if message_type == "error":
            raise BackupRuntimeError("restic_reported_error")
        if message_type == "summary":
            if summary is not None:
                raise BackupRuntimeError("restic_json_unknown")
            summary = _validate_snapshot_id(value.get("snapshot_id"))
    if not saw_record or summary is None:
        raise BackupRuntimeError("restic_summary_missing")
    return summary


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise BackupRuntimeError("restic_json_malformed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupRuntimeError("restic_json_malformed") from exc
    if parsed.tzinfo is None:
        raise BackupRuntimeError("restic_json_malformed")
    return parsed.astimezone(timezone.utc)


def parse_snapshot_json(raw: bytes, *, machine_id: str = MACHINE_ID) -> tuple[RepositorySnapshot, ...]:
    """Strictly parse the bounded output from ``restic snapshots --json``."""
    _validate_machine(machine_id)
    if not isinstance(raw, bytes) or len(raw) > DEFAULT_OUTPUT_LIMIT_BYTES:
        raise BackupRuntimeError("restic_output_exceeded")
    try:
        value = _load_json(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise BackupRuntimeError("restic_json_malformed") from exc
    if not isinstance(value, list):
        raise BackupRuntimeError("restic_json_unknown")
    snapshots: list[RepositorySnapshot] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or not set(item).issubset(_SNAPSHOT_FIELDS):
            raise BackupRuntimeError("restic_json_unknown")
        snapshot_id = _validate_snapshot_id(item.get("id"))
        paths = item.get("paths")
        tags = item.get("tags")
        hostname = item.get("hostname")
        if (
            snapshot_id in seen
            or not isinstance(paths, list)
            or not paths
            or any(not isinstance(path, str) or not Path(path).is_absolute() for path in paths)
            or len(set(paths)) != len(paths)
            or not isinstance(tags, list)
            or any(not isinstance(tag, str) or len(tag) > 160 for tag in tags)
            or len(set(tags)) != len(tags)
            or (hostname is not None and not isinstance(hostname, str))
        ):
            raise BackupRuntimeError("restic_json_unknown")
        if BASE_TAG not in tags or MACHINE_TAG not in tags:
            raise BackupRuntimeError("wrong_target")
        seen.add(snapshot_id)
        snapshots.append(
            RepositorySnapshot(
                snapshot_id,
                _parse_time(item.get("time")),
                tuple(paths),
                tuple(tags),
                hostname,
            )
        )
    return tuple(snapshots)


def verify_repository_snapshot(
    raw: bytes,
    *,
    expected_snapshot_id: str,
    expected_path: Path,
    backup_id: str,
) -> RepositorySnapshot:
    snapshots = parse_snapshot_json(raw)
    expected_id = _validate_snapshot_id(expected_snapshot_id)
    expected_tag = f"backup:{_validate_backup_id(backup_id)}"
    if len(snapshots) != 1:
        raise BackupRuntimeError("snapshot_postcondition_failed")
    snapshot = snapshots[0]
    if (
        snapshot.snapshot_id != expected_id
        or snapshot.paths != (str(Path(expected_path)),)
        or expected_tag not in snapshot.tags
    ):
        raise BackupRuntimeError("snapshot_postcondition_failed")
    return snapshot


def _is_protected(tags: Iterable[str]) -> bool:
    for tag in tags:
        if tag in _PROTECTED_TAGS or any(
            tag.startswith(prefix + ":")
            for prefix in ("open-incident", "hardware", "hardware-change", "pre-action")
        ):
            return True
    return False


def _bucket_key(kind: str, value: datetime) -> object:
    utc = value.astimezone(timezone.utc)
    if kind == "hourly":
        return utc.year, utc.month, utc.day, utc.hour
    if kind == "daily":
        return utc.year, utc.month, utc.day
    if kind == "weekly":
        iso = utc.isocalendar()
        return iso.year, iso.week
    if kind == "monthly":
        return utc.year, utc.month
    if kind == "yearly":
        return utc.year
    raise BackupRuntimeError("retention_policy_invalid")


def prune_local_snapshots(
    snapshot_root: Path, *, keep: Path, retain: int = LOCAL_SNAPSHOTS_KEPT
) -> int:
    """Remove local snapshots beyond the newest ``retain``; never ``keep``.

    Called only after ``keep`` is verified in the repository. Only real
    directories named like generated backup ids are candidates, and a removal
    failure leaves the remaining snapshots in place without failing the backup.
    """
    root = Path(snapshot_root)
    keep = Path(keep)
    try:
        candidates = sorted(
            entry.name
            for entry in os.scandir(root)
            if _GENERATED_BACKUP_ID.fullmatch(entry.name)
            and entry.is_dir(follow_symlinks=False)
        )
    except OSError:
        return 0
    removed = 0
    for name in candidates[: max(0, len(candidates) - retain)]:
        path = root / name
        if path == keep:
            continue
        try:
            _remove_read_only_tree(path)
        except OSError:
            continue
        removed += 1
    return removed


def _remove_read_only_tree(path: Path) -> None:
    """Remove a published read-only snapshot tree without following symlinks."""
    for directory, dirnames, _filenames in os.walk(path, topdown=True):
        # os.walk does not follow symlinks, and symlinked entries are skipped.
        os.chmod(directory, 0o700)
        dirnames[:] = [
            name for name in dirnames if not os.path.islink(os.path.join(directory, name))
        ]
    shutil.rmtree(path)


def plan_retention(snapshots: Iterable[RepositorySnapshot]) -> RetentionPlan:
    """Return keep/unretained decisions; this function cannot run restic."""
    ordered = tuple(sorted(snapshots, key=lambda item: (item.time, item.snapshot_id), reverse=True))
    if len({item.snapshot_id for item in ordered}) != len(ordered):
        raise BackupRuntimeError("restic_json_unknown")
    reasons: dict[str, set[str]] = {item.snapshot_id: set() for item in ordered}
    protected: dict[str, bool] = {}
    for snapshot in ordered:
        _validate_snapshot_id(snapshot.snapshot_id)
        if (
            not isinstance(snapshot.time, datetime)
            or snapshot.time.tzinfo is None
            or not snapshot.paths
            or any(
                not isinstance(path, str) or not Path(path).is_absolute()
                for path in snapshot.paths
            )
            or len(set(snapshot.paths)) != len(snapshot.paths)
            or any(not isinstance(tag, str) or len(tag) > 160 for tag in snapshot.tags)
            or len(set(snapshot.tags)) != len(snapshot.tags)
        ):
            raise BackupRuntimeError("restic_json_malformed")
        if BASE_TAG not in snapshot.tags or MACHINE_TAG not in snapshot.tags:
            raise BackupRuntimeError("wrong_target")
        protected[snapshot.snapshot_id] = _is_protected(snapshot.tags)
        if protected[snapshot.snapshot_id]:
            reasons[snapshot.snapshot_id].add("protected-tag")
    for kind, count in RETENTION_COUNTS.items():
        buckets: set[object] = set()
        for snapshot in ordered:
            key = _bucket_key(kind, snapshot.time)
            if key in buckets or len(buckets) >= count:
                continue
            buckets.add(key)
            reasons[snapshot.snapshot_id].add(kind)
    decisions = tuple(
        RetentionDecision(
            snapshot.snapshot_id,
            snapshot.time.astimezone(timezone.utc),
            bool(reasons[snapshot.snapshot_id]),
            protected[snapshot.snapshot_id],
            tuple(sorted(reasons[snapshot.snapshot_id])),
        )
        for snapshot in ordered
    )
    return RetentionPlan(MACHINE_ID, dict(RETENTION_COUNTS), decisions)


class BackupRuntime:
    """Single-flight coordinator for one configured repository identity."""

    def __init__(
        self,
        config: ResticConfig,
        *,
        preflight_probe: Callable[[ResticConfig], StoragePreflight] = default_storage_preflight,
        snapshot_creator: Callable[..., BackupResult] = create_local_snapshot,
        snapshot_verifier: Callable[[Path], ValidationResult] = verify_local_snapshot,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.config = config
        self._preflight_probe = preflight_probe
        self._snapshot_creator = snapshot_creator
        self._snapshot_verifier = snapshot_verifier
        self._popen_factory = popen_factory
        self._clock = clock

    def _validate_runtime_files(self) -> None:
        if not _executable_is_exact_file(self.config.executable):
            raise BackupRuntimeError("restic_executable_invalid")
        if not _executable_is_exact_file(self.config.ssh_executable):
            raise BackupRuntimeError("ssh_executable_invalid")
        if not _credential_file_is_private(self.config.credential_file):
            raise BackupRuntimeError("credential_file_invalid")
        if not _credential_file_is_private(self.config.ssh_identity_file):
            raise BackupRuntimeError("ssh_identity_file_invalid")
        if not _credential_file_is_private(self.config.ssh_known_hosts_file):
            raise BackupRuntimeError("ssh_known_hosts_file_invalid")

    def _preflight(self) -> None:
        try:
            result = self._preflight_probe(self.config)
        except Exception:
            raise BackupRuntimeError("repository_preflight_failed") from None
        if (
            not isinstance(result, StoragePreflight)
            or result.mounted is not True
            or result.quota_bytes != self.config.repository_quota_bytes
            or not isinstance(result.free_bytes, int)
            or isinstance(result.free_bytes, bool)
            or result.free_bytes < self.config.minimum_quota_free_bytes
            or result.free_bytes > result.quota_bytes
        ):
            raise BackupRuntimeError("repository_preflight_failed")

    def _run(self, argv: Sequence[str], deadline: datetime) -> bytes:
        _validate_executable_command(self.config, argv)
        return _execute_command(
            argv,
            command_environment(self.config),
            deadline=deadline,
            clock=self._clock,
            output_limit=self.config.output_limit_bytes,
            cleanup_grace=float(self.config.cleanup_grace_seconds),
            popen_factory=self._popen_factory,
        )

    def run_backup(
        self,
        state_dir: Path,
        snapshot_root: Path,
        *,
        deadline: datetime,
        machine_id: str = MACHINE_ID,
        backup_id: str | None = None,
    ) -> BackupRunResult:
        _validate_machine(machine_id)
        _validate_deadline(deadline, self._clock)
        self._validate_runtime_files()
        try:
            lock_fd = os.open(
                self.config.lock_file,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            raise BackupRuntimeError("backup_lock_invalid") from None
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BackupRuntimeError("backup_already_running") from exc
            except OSError:
                raise BackupRuntimeError("backup_lock_invalid") from None
            lock_status = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_status.st_mode)
                or stat.S_IMODE(lock_status.st_mode) & 0o077
                or lock_status.st_nlink != 1
            ):
                raise BackupRuntimeError("backup_lock_invalid")
            self._preflight()
            _validate_deadline(deadline, self._clock)
            try:
                result = self._snapshot_creator(
                    Path(state_dir), Path(snapshot_root), backup_id=backup_id, clock=self._clock
                )
            except Exception:
                raise BackupRuntimeError("local_snapshot_failed") from None
            if not result.success or result.published_path is None or result.backup_id is None:
                raise BackupRuntimeError("local_snapshot_failed")
            local_path = result.published_path
            local_id = _validate_backup_id(result.backup_id)
            try:
                validation = self._snapshot_verifier(local_path)
            except Exception:
                raise BackupRuntimeError("local_snapshot_verification_failed") from None
            if (
                not validation.valid
                or validation.snapshot_sha256 != result.snapshot_sha256
                or validation.manifest_sha256 != result.manifest_sha256
            ):
                raise BackupRuntimeError("local_snapshot_verification_failed")
            backup_output = self._run(
                build_backup_command(self.config, local_path, local_id), deadline
            )
            repository_id = parse_backup_json(backup_output)
            verify_output = self._run(
                build_snapshot_verification_command(self.config, repository_id), deadline
            )
            verify_repository_snapshot(
                verify_output,
                expected_snapshot_id=repository_id,
                expected_path=local_path,
                backup_id=local_id,
            )
            try:
                final_validation = self._snapshot_verifier(local_path)
            except Exception:
                raise BackupRuntimeError("local_snapshot_postcondition_failed") from None
            if final_validation != validation:
                raise BackupRuntimeError("local_snapshot_postcondition_failed")
            return BackupRunResult(
                True,
                MACHINE_ID,
                local_id,
                local_path,
                repository_id,
                "isolated_restore_and_hashes_ok",
                "snapshot_identity_and_tags_ok",
                prune_local_snapshots(Path(snapshot_root), keep=local_path),
            )
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
