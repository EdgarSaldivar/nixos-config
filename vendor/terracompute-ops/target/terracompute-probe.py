#!/usr/bin/env python3
"""Read-only, forced-command observation probe for the terracompute target.

The executable intentionally has no command-line or stdin interface.  Every
external command is selected from the fixed COMMANDS table below.
"""

from __future__ import annotations

import csv
import datetime as dt
import fcntl
import io
import json
import os
import re
import selectors
import signal
import socket
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


MACHINE_ID = 17049
EXPECTED_GPU_COUNT = 8
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PCI_DEVICES_PATH = Path("/sys/bus/pci/devices")
DMI_ID_PATH = Path("/sys/class/dmi/id")
PROC_PATH = Path("/proc")
OBSERVER_STATE_DIRECTORY = Path("/var/lib/terracompute-observer")
ADMISSION_LOCK_FILENAME = "probe.lock"
ADMISSION_STATE_FILENAME = "probe-state.json"
ADMISSION_STATE_VERSION = 1
MAX_ADMISSION_STATE_BYTES = 4096
MAX_PROC_ENTRIES = 131072
ADMISSION_RECONCILE_SECONDS = 1.0
COLLECTION_DEADLINE_SECONDS = 45.0
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_JSON_OUTPUT_BYTES = 512 * 1024
MAX_EVENTS = 96
MAX_EVENT_COUNT = 999
MAX_CONTAINERS = 256
MAX_CONTAINER_NAME_CHARS = 128
MAX_CONTAINER_STATUS_CHARS = 256
MAX_INVENTORY_VALUE_CHARS = 128
MAX_PCI_DEVICES = 32
MAX_PCI_ROOT_PATH_CHARS = 512
PROCESS_TERMINATE_GRACE_SECONDS = 0.25
PROCESS_KILL_WAIT_SECONDS = 0.25


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    timeout_seconds: float


# These are the complete set of subprocess argument vectors the probe can run.
COMMANDS: dict[str, CommandSpec] = {
    "gpu": CommandSpec(
        (
            "/usr/bin/nvidia-smi",
            "--query-gpu=uuid,pci.bus_id,name,temperature.gpu,pstate,serial,driver_version,vbios_version",
            "--format=csv,noheader,nounits",
        ),
        8.0,
    ),
    "kernel_journal": CommandSpec(
        (
            "/usr/bin/journalctl",
            "--boot=0",
            "-k",
            "--since=-15min",
            "--no-pager",
            "--output=cat",
        ),
        8.0,
    ),
    "docker_journal": CommandSpec(
        (
            "/usr/bin/journalctl",
            "--boot=0",
            "--unit=docker.service",
            "--since=-15min",
            "--no-pager",
            "--output=cat",
        ),
        8.0,
    ),
    "service_vastai": CommandSpec(
        ("/usr/bin/systemctl", "is-active", "vastai.service"), 4.0
    ),
    "service_docker": CommandSpec(
        ("/usr/bin/systemctl", "is-active", "docker.service"), 4.0
    ),
    "service_nvidia_persistenced": CommandSpec(
        ("/usr/bin/systemctl", "is-active", "nvidia-persistenced.service"), 4.0
    ),
    "docker_metadata": CommandSpec(
        (
            "/usr/bin/docker",
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .Names}}\t{{json .Status}}",
        ),
        8.0,
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str = ""
    failure: str | None = None
    cleanup_confirmed: bool = True


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    process_group_id: int
    session_id: int
    start_time_ticks: int


def _start_process(
    spec: CommandSpec, inherited_fd: int | None = None
) -> subprocess.Popen[bytes]:
    """Start one catalogued command in an isolated Unix process group."""
    arguments: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "close_fds": True,
        "start_new_session": True,
    }
    if inherited_fd is not None:
        arguments["pass_fds"] = (inherited_fd,)
    return subprocess.Popen(list(spec.argv), **arguments)  # type: ignore[arg-type]


def _read_process_identity(pid: int) -> ProcessIdentity:
    """Read immutable-enough Linux process identity without invoking a command."""
    if pid <= 0:
        raise ValueError("invalid process identifier")
    stat_path = PROC_PATH / str(pid) / "stat"
    if not PROC_PATH.is_dir():
        # Offline cleanup regression support on non-Linux development hosts.
        return ProcessIdentity(pid, os.getpgid(pid), os.getsid(pid), pid)
    with stat_path.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("process stat exceeds bound")
    closing_parenthesis = raw.rfind(b") ")
    if closing_parenthesis < 1:
        raise ValueError("invalid process stat")
    fields = raw[closing_parenthesis + 2 :].split()
    if len(fields) < 20:
        raise ValueError("short process stat")
    identity = ProcessIdentity(
        pid=pid,
        process_group_id=int(fields[2]),
        session_id=int(fields[3]),
        start_time_ticks=int(fields[19]),
    )
    if min(
        identity.process_group_id, identity.session_id, identity.start_time_ticks
    ) <= 0:
        raise ValueError("invalid process identity")
    return identity


def _process_identity_record(identity: ProcessIdentity) -> dict[str, int]:
    return {
        "pid": identity.pid,
        "process_group_id": identity.process_group_id,
        "session_id": identity.session_id,
        "start_time_ticks": identity.start_time_ticks,
    }


def _parse_process_identity(value: object) -> ProcessIdentity:
    required = {"pid", "process_group_id", "session_id", "start_time_ticks"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid process identity record")
    fields = [value.get(name) for name in required]
    if any(isinstance(field, bool) or not isinstance(field, int) for field in fields):
        raise ValueError("invalid process identity field")
    identity = ProcessIdentity(
        pid=value["pid"],
        process_group_id=value["process_group_id"],
        session_id=value["session_id"],
        start_time_ticks=value["start_time_ticks"],
    )
    if min(
        identity.pid,
        identity.process_group_id,
        identity.session_id,
        identity.start_time_ticks,
    ) <= 0:
        raise ValueError("invalid process identity values")
    return identity


def _process_group_identity_exists(
    identity: ProcessIdentity, deadline: float | None = None
) -> bool:
    """Observe whether any member of the exact recorded session/group remains."""
    if not PROC_PATH.is_dir():
        return _process_group_exists(identity.process_group_id)
    # killpg(0) is an authoritative, constant-time absence check.  Avoid a
    # fail-closed full /proc scan after the group has already disappeared; some
    # hardened hosts intentionally hide unrelated process stat files even from a
    # short-lived privileged observer.
    if not _process_group_exists(identity.process_group_id):
        return False
    scanned = 0
    for entry in PROC_PATH.iterdir():
        if deadline is not None and time.monotonic() >= deadline:
            raise OSError("process reconciliation deadline exceeded")
        if not entry.name.isdecimal():
            continue
        scanned += 1
        if scanned > MAX_PROC_ENTRIES:
            raise OSError("process scan bound exceeded")
        try:
            member = _read_process_identity(int(entry.name))
        except (
            FileNotFoundError,
            ProcessLookupError,
        ):
            continue
        except (PermissionError, ValueError) as error:
            raise OSError("ambiguous process identity") from error
        if (
            member.process_group_id == identity.process_group_id
            and member.session_id == identity.session_id
        ):
            # A leader start-time mismatch is ambiguous and therefore present;
            # callers never signal identities loaded from persisted state.
            return True
    return False


def _process_group_matches_for_signal(
    identity: ProcessIdentity, deadline: float
) -> bool:
    """Confirm a current in-memory identity before signalling its process group."""
    if not PROC_PATH.is_dir():
        return _process_group_exists(identity.process_group_id)
    matching_nonleaders = False
    scanned = 0
    for entry in PROC_PATH.iterdir():
        if time.monotonic() >= deadline:
            return False
        if not entry.name.isdecimal():
            continue
        scanned += 1
        if scanned > MAX_PROC_ENTRIES:
            return False
        try:
            member = _read_process_identity(int(entry.name))
        except (
            FileNotFoundError,
            ProcessLookupError,
        ):
            continue
        except (PermissionError, ValueError):
            return False
        if (
            member.process_group_id != identity.process_group_id
            or member.session_id != identity.session_id
        ):
            continue
        if member.pid == identity.pid:
            return member.start_time_ticks == identity.start_time_ticks
        matching_nonleaders = True
    # A session/group with surviving non-leaders cannot reuse its numeric PGID;
    # it remains the group created by this invocation even after leader exit.
    return matching_nonleaders


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class AdmissionError(Exception):
    """Fail-closed admission error whose details must not reach probe output."""


class AdmissionBusy(AdmissionError):
    """Another probe or an inherited probe command still owns admission."""


class _Admission:
    def __init__(
        self,
        directory_fd: int,
        lock_fd: int,
        boot_id: str,
        state_uid: int,
        state_gid: int,
    ) -> None:
        self.directory_fd = directory_fd
        self.lock_fd = lock_fd
        self.boot_id = boot_id
        self.state_uid = state_uid
        self.state_gid = state_gid
        self.cleanup_confirmed = True
        self.blocked_after: str | None = None

    def close(self) -> None:
        # Do not explicitly unlock: a command descendant may still hold the same
        # open-file description after a controller/SSH-side disconnect or crash.
        os.close(self.lock_fd)
        os.close(self.directory_fd)

    def _write_state(self, value: dict[str, object]) -> None:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if len(encoded) > MAX_ADMISSION_STATE_BYTES:
            raise AdmissionError("admission state exceeds bound")
        temp_fd, temp_path = tempfile.mkstemp(
            prefix=".probe-state-", dir=OBSERVER_STATE_DIRECTORY
        )
        replaced = False
        try:
            os.fchmod(temp_fd, 0o600)
            temp_metadata = os.fstat(temp_fd)
            if (
                temp_metadata.st_uid != self.state_uid
                or temp_metadata.st_gid != self.state_gid
            ):
                os.fchown(temp_fd, self.state_uid, self.state_gid)
            with os.fdopen(temp_fd, "wb", closefd=True) as stream:
                temp_fd = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                Path(temp_path).name,
                ADMISSION_STATE_FILENAME,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            replaced = True
            os.fsync(self.directory_fd)
        except (OSError, ValueError) as error:
            raise AdmissionError("cannot persist admission state") from error
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if not replaced:
                try:
                    os.unlink(Path(temp_path).name, dir_fd=self.directory_fd)
                except FileNotFoundError:
                    pass

    def _mark_idle(self) -> None:
        self._write_state({"version": ADMISSION_STATE_VERSION, "phase": "idle"})

    def run_command(self, command_id: str, timeout_seconds: float) -> CommandResult:
        if self.blocked_after is not None:
            return CommandResult(
                None, failure="cleanup_unconfirmed", cleanup_confirmed=False
            )
        spec = COMMANDS.get(command_id)
        if spec is None:
            return CommandResult(None, failure="unknown_command")
        try:
            coordinator = _read_process_identity(os.getpid())
            self._write_state(
                {
                    "version": ADMISSION_STATE_VERSION,
                    "phase": "launching",
                    "boot_id": self.boot_id,
                    "command_id": command_id,
                    "coordinator": _process_identity_record(coordinator),
                }
            )
        except (OSError, ValueError, AdmissionError):
            self.cleanup_confirmed = False
            self.blocked_after = command_id
            return CommandResult(
                None, failure="admission_state_failed", cleanup_confirmed=False
            )

        result = _bounded_exec(
            spec,
            timeout_seconds,
            admission=self,
            command_id=command_id,
        )
        if result.cleanup_confirmed:
            try:
                self._mark_idle()
            except AdmissionError:
                result = CommandResult(
                    result.returncode,
                    stdout=result.stdout,
                    failure="admission_state_failed",
                    cleanup_confirmed=False,
                )
        if not result.cleanup_confirmed:
            self.cleanup_confirmed = False
            self.blocked_after = command_id
        return result


def _validate_owned_file(
    file_descriptor: int, expected_mode: int, expected_uid: int, expected_gid: int
) -> None:
    metadata = os.fstat(file_descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or metadata.st_uid != expected_uid
        or metadata.st_gid != expected_gid
        or metadata.st_nlink != 1
    ):
        raise AdmissionError("unsafe admission file")


def _read_admission_state(
    directory_fd: int, expected_uid: int, expected_gid: int
) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        state_fd = os.open(ADMISSION_STATE_FILENAME, flags, dir_fd=directory_fd)
    except OSError as error:
        raise AdmissionError("admission state unavailable") from error
    try:
        _validate_owned_file(state_fd, 0o600, expected_uid, expected_gid)
        with os.fdopen(state_fd, "rb", closefd=False) as stream:
            encoded = stream.read(MAX_ADMISSION_STATE_BYTES + 1)
        if len(encoded) > MAX_ADMISSION_STATE_BYTES:
            raise AdmissionError("admission state exceeds bound")
        value = json.loads(encoded.decode("ascii"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise AdmissionError("invalid admission state") from error
    finally:
        os.close(state_fd)
    if not isinstance(value, dict):
        raise AdmissionError("invalid admission state type")
    return value


def _validate_admission_state(value: dict[str, object]) -> str:
    version = value.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != ADMISSION_STATE_VERSION
    ):
        raise AdmissionError("invalid admission version")
    if set(value) == {"version", "phase"} and value.get("phase") == "idle":
        return "idle"
    common = {"version", "phase", "boot_id", "command_id"}
    phase = value.get("phase")
    if phase == "launching":
        if set(value) != common | {"coordinator"}:
            raise AdmissionError("invalid launching state")
        _parse_process_identity(value.get("coordinator"))
    elif phase == "active":
        if set(value) != common | {"process"}:
            raise AdmissionError("invalid active state")
        _parse_process_identity(value.get("process"))
    else:
        raise AdmissionError("invalid admission phase")
    if (
        value.get("command_id") not in COMMANDS
        or not isinstance(value.get("boot_id"), str)
        or not _BOOT_ID_RE.fullmatch(value["boot_id"])
    ):
        raise AdmissionError("invalid admission fields")
    return str(phase)


def _acquire_admission(boot_id: str) -> _Admission:
    if not _BOOT_ID_RE.fullmatch(boot_id):
        raise AdmissionError("invalid current boot identity")
    try:
        directory_metadata = os.lstat(OBSERVER_STATE_DIRECTORY)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
            or os.geteuid() not in {0, directory_metadata.st_uid}
        ):
            raise AdmissionError("unsafe admission directory")
        directory_fd = os.open(
            OBSERVER_STATE_DIRECTORY,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise AdmissionError("admission directory unavailable") from error

    lock_fd = -1
    try:
        lock_fd = os.open(
            ADMISSION_LOCK_FILENAME,
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        _validate_owned_file(
            lock_fd,
            0o600,
            directory_metadata.st_uid,
            directory_metadata.st_gid,
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AdmissionBusy("probe admission is held") from error

        value = _read_admission_state(
            directory_fd,
            directory_metadata.st_uid,
            directory_metadata.st_gid,
        )
        phase = _validate_admission_state(value)
        admission = _Admission(
            directory_fd,
            lock_fd,
            boot_id,
            directory_metadata.st_uid,
            directory_metadata.st_gid,
        )
        if phase == "idle":
            return admission
        if value["boot_id"] != boot_id:
            admission._mark_idle()
            return admission
        if phase == "launching":
            # Acquiring the inherited lock proves the prior coordinator and every
            # trusted launched descendant that inherited it have closed the fd.
            admission._mark_idle()
            return admission
        identity = _parse_process_identity(value["process"])
        try:
            group_exists = _process_group_identity_exists(
                identity, time.monotonic() + ADMISSION_RECONCILE_SECONDS
            )
        except OSError as error:
            raise AdmissionError("cannot reconcile prior process group") from error
        if group_exists:
            raise AdmissionBusy("prior process group remains")
        admission._mark_idle()
        return admission
    except Exception:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)
        raise


def _wait_for_process_group(
    process: subprocess.Popen[bytes], identity: ProcessIdentity, deadline: float
) -> bool:
    while True:
        process.poll()
        try:
            if not _process_group_identity_exists(identity, deadline):
                return True
        except OSError:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _stop_process_group(
    process: subprocess.Popen[bytes], deadline: float, identity: ProcessIdentity
) -> bool:
    """Terminate every command descendant, escalating within the caller's deadline."""
    if _process_group_matches_for_signal(identity, deadline):
        try:
            os.killpg(identity.process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass

    terminate_deadline = min(
        deadline, time.monotonic() + PROCESS_TERMINATE_GRACE_SECONDS
    )
    cleanup_confirmed = _wait_for_process_group(process, identity, terminate_deadline)
    if not cleanup_confirmed:
        # Signal only the group identity created by this invocation. Reconciled
        # identities loaded from disk are observation-only and never reach here.
        try:
            if _process_group_matches_for_signal(identity, deadline):
                os.killpg(identity.process_group_id, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        kill_deadline = min(deadline, time.monotonic() + PROCESS_KILL_WAIT_SECONDS)
        cleanup_confirmed = _wait_for_process_group(process, identity, kill_deadline)

    remaining = max(0.0, deadline - time.monotonic())
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        pass
    return cleanup_confirmed


def _stop_new_process_without_identity(
    process: subprocess.Popen[bytes], deadline: float
) -> bool:
    """Stop only an unreaped process just returned by Popen when /proc raced."""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    remaining = max(0.0, deadline - time.monotonic())
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False
    return not _process_group_exists(process.pid)


def _bounded_exec(
    spec: CommandSpec,
    timeout_seconds: float | None = None,
    *,
    admission: _Admission | None = None,
    command_id: str | None = None,
) -> CommandResult:
    """Capture a command while enforcing wall-clock and combined-output bounds."""
    started_at = time.monotonic()
    effective_timeout = spec.timeout_seconds
    if timeout_seconds is not None:
        effective_timeout = min(effective_timeout, max(0.0, timeout_seconds))
    if effective_timeout <= 0:
        return CommandResult(None, failure="timeout")
    try:
        if admission is None:
            process = _start_process(spec)
        else:
            process = _start_process(spec, admission.lock_fd)
    except (OSError, subprocess.SubprocessError):
        return CommandResult(None, failure="launch_failed")

    deadline = started_at + effective_timeout
    try:
        identity = _read_process_identity(process.pid)
        if (
            identity.pid != identity.process_group_id
            or identity.pid != identity.session_id
        ):
            raise ValueError("command did not start in its own session")
        if admission is not None:
            if command_id not in COMMANDS:
                raise AdmissionError("uncatalogued admitted command")
            admission._write_state(
                {
                    "version": ADMISSION_STATE_VERSION,
                    "phase": "active",
                    "boot_id": admission.boot_id,
                    "command_id": command_id,
                    "process": _process_identity_record(identity),
                }
            )
    except (OSError, ValueError, AdmissionError):
        # This identity was obtained from the process object created above, not
        # from persisted state. Persisted numeric identifiers are never signalled.
        if "identity" in locals():
            cleanup_confirmed = _stop_process_group(process, deadline, identity)
        else:
            cleanup_confirmed = _stop_new_process_without_identity(process, deadline)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        return CommandResult(
            None,
            failure="admission_state_failed"
            if admission is not None
            else "capture_failed",
            cleanup_confirmed=cleanup_confirmed,
        )

    if process.stdout is None or process.stderr is None:
        cleanup_confirmed = _stop_process_group(process, deadline, identity)
        return CommandResult(
            None,
            failure="capture_failed" if cleanup_confirmed else "cleanup_unconfirmed",
            cleanup_confirmed=cleanup_confirmed,
        )

    selector = selectors.DefaultSelector()
    captured = bytearray()
    total_output_bytes = 0
    cleanup_reserve = min(
        effective_timeout,
        PROCESS_TERMINATE_GRACE_SECONDS + PROCESS_KILL_WAIT_SECONDS,
    )
    execution_deadline = deadline - cleanup_reserve
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)

        while selector.get_map():
            remaining_time = execution_deadline - time.monotonic()
            if remaining_time <= 0:
                cleanup_confirmed = _stop_process_group(process, deadline, identity)
                return CommandResult(
                    None,
                    failure="timeout" if cleanup_confirmed else "cleanup_unconfirmed",
                    cleanup_confirmed=cleanup_confirmed,
                )
            for key, _mask in selector.select(remaining_time):
                remaining_bytes = MAX_COMMAND_OUTPUT_BYTES - total_output_bytes
                if remaining_bytes <= 0:
                    cleanup_confirmed = _stop_process_group(process, deadline, identity)
                    return CommandResult(
                        None,
                        failure="output_limit"
                        if cleanup_confirmed
                        else "cleanup_unconfirmed",
                        cleanup_confirmed=cleanup_confirmed,
                    )
                try:
                    chunk = os.read(key.fd, min(65536, remaining_bytes + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if len(chunk) > remaining_bytes:
                    cleanup_confirmed = _stop_process_group(process, deadline, identity)
                    return CommandResult(
                        None,
                        failure="output_limit"
                        if cleanup_confirmed
                        else "cleanup_unconfirmed",
                        cleanup_confirmed=cleanup_confirmed,
                    )
                total_output_bytes += len(chunk)
                # stderr is deliberately consumed but never returned or reported.
                if key.fileobj is process.stdout:
                    captured.extend(chunk)

        remaining_time = execution_deadline - time.monotonic()
        if remaining_time <= 0:
            cleanup_confirmed = _stop_process_group(process, deadline, identity)
            return CommandResult(
                None,
                failure="timeout" if cleanup_confirmed else "cleanup_unconfirmed",
                cleanup_confirmed=cleanup_confirmed,
            )
        try:
            returncode = process.wait(timeout=remaining_time)
        except subprocess.TimeoutExpired:
            cleanup_confirmed = _stop_process_group(process, deadline, identity)
            return CommandResult(
                None,
                failure="timeout" if cleanup_confirmed else "cleanup_unconfirmed",
                cleanup_confirmed=cleanup_confirmed,
            )
        cleanup_confirmed = not _process_group_identity_exists(identity, deadline)
        if not cleanup_confirmed:
            cleanup_confirmed = _stop_process_group(process, deadline, identity)
        if not cleanup_confirmed:
            return CommandResult(
                returncode,
                captured.decode("utf-8", errors="replace"),
                failure="cleanup_unconfirmed",
                cleanup_confirmed=False,
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        cleanup_confirmed = _stop_process_group(process, deadline, identity)
        return CommandResult(
            None,
            failure="capture_failed" if cleanup_confirmed else "cleanup_unconfirmed",
            cleanup_confirmed=cleanup_confirmed,
        )
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()

    return CommandResult(
        returncode,
        captured.decode("utf-8", errors="replace"),
    )


def run_command(
    command_id: str, timeout_seconds: float | None = None
) -> CommandResult:
    """Run only a named, immutable command from the catalog."""
    spec = COMMANDS.get(command_id)
    if spec is None:
        return CommandResult(None, failure="unknown_command")
    return _bounded_exec(spec, timeout_seconds)


def _event(
    family: str,
    code: str,
    severity: str,
    message: str,
    evidence: dict[str, object] | None = None,
    count: int = 1,
) -> dict[str, object]:
    return {
        "fault_family": family,
        "code": code,
        "severity": severity,
        "message": message,
        "count": max(1, min(count, MAX_EVENT_COUNT)),
        "evidence": evidence or {},
    }


def _probe_failure(collector: str, failure: str = "invalid_output") -> dict[str, object]:
    allowed_failures = {
        "admission_state_failed",
        "launch_failed",
        "capture_failed",
        "cleanup_unconfirmed",
        "timeout",
        "output_limit",
        "nonzero_exit",
        "invalid_output",
    }
    normalized = failure if failure in allowed_failures else "invalid_output"
    evidence: dict[str, object] = {
        "collector": collector,
        "failure": normalized,
    }
    if normalized in {"admission_state_failed", "cleanup_unconfirmed"}:
        evidence["cleanup_confirmed"] = False
    return _event(
        "probe",
        f"{collector}_{normalized}",
        "error",
        f"Required {collector} collector failed",
        evidence,
    )


def _command_failure(collector: str, result: CommandResult) -> dict[str, object] | None:
    if result.failure:
        return _probe_failure(collector, result.failure)
    if result.returncode != 0:
        return _probe_failure(collector, "nonzero_exit")
    return None


_UUID_RE = re.compile(r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_BDF_RE = re.compile(
    r"(?<![0-9a-fA-F])(?P<bdf>[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?)(?![0-9a-fA-F])"
)
_XID_RE = re.compile(
    r"Xid\s*\(PCI:(?P<bdf>[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?)\)\s*:\s*(?P<xid>[0-9]{1,4})",
    re.IGNORECASE,
)
_BOOT_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)
_PCI_ROOT_RE = re.compile(r"pci[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}\Z")
_LINK_SPEED_RE = re.compile(r"[0-9]+(?:\.[0-9]+)? GT/s(?: PCIe)?\Z")
_UNKNOWN_INVENTORY_VALUES = {
    "",
    "n/a",
    "[n/a]",
    "none",
    "not available",
    "not specified",
    "to be filled by o.e.m.",
    "unknown",
}

_DMI_FIELDS = {
    "motherboard": {
        "vendor": "board_vendor",
        "name": "board_name",
        "version": "board_version",
        "serial": "board_serial",
    },
    "bios": {
        "vendor": "bios_vendor",
        "version": "bios_version",
        "date": "bios_date",
    },
}


def _normalize_bdf(value: str) -> str | None:
    match = _BDF_RE.fullmatch(value.strip())
    if not match:
        return None
    domain, bus, slot_function = match.group("bdf").lower().split(":")
    if "." not in slot_function:
        slot_function += ".0"
    return f"{int(domain, 16):04x}:{bus}:{slot_function}"


def _inventory_value(value: str, limit: int = MAX_INVENTORY_VALUE_CHARS) -> str:
    cleaned = _clean_text(value.strip(), limit)
    if cleaned.lower() in _UNKNOWN_INVENTORY_VALUES:
        return "unknown"
    return cleaned or "unknown"


def _parse_gpus(text: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    gpus: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    seen_uuids: set[str] = set()
    seen_bdfs: set[str] = set()
    try:
        rows = csv.reader(io.StringIO(text))
        for row in rows:
            if not row or all(not field.strip() for field in row):
                continue
            if len(row) != 8:
                raise ValueError
            (
                uuid,
                raw_bdf,
                name,
                raw_temperature,
                pstate,
                raw_serial,
                raw_driver_version,
                raw_vbios_version,
            ) = (
                field.strip() for field in row
            )
            bdf = _normalize_bdf(raw_bdf)
            if not _UUID_RE.fullmatch(uuid) or bdf is None:
                raise ValueError
            if uuid in seen_uuids or bdf in seen_bdfs:
                raise ValueError
            temperature = int(raw_temperature)
            if not 0 <= temperature <= 125:
                raise ValueError
            if not re.fullmatch(r"P(?:[0-9]|1[0-5])", pstate):
                raise ValueError
            clean_name = _clean_text(name, 96)
            serial = _inventory_value(raw_serial, 96)
            driver_version = _inventory_value(raw_driver_version, 64)
            vbios_version = _inventory_value(raw_vbios_version, 64)
            if not clean_name or driver_version == "unknown" or vbios_version == "unknown":
                raise ValueError
            seen_uuids.add(uuid)
            seen_bdfs.add(bdf)
            gpus.append(
                {
                    "uuid": uuid,
                    "pci_bdf": bdf,
                    "name": clean_name,
                    "temperature_c": temperature,
                    "pstate": pstate,
                    "serial": serial,
                    "driver_version": driver_version,
                    "vbios_version": vbios_version,
                }
            )
    except (csv.Error, TypeError, ValueError):
        return [], [_probe_failure("gpu_inventory")]

    gpus.sort(key=lambda gpu: (str(gpu["pci_bdf"]), str(gpu["uuid"])))
    for gpu in gpus:
        if int(gpu["temperature_c"]) >= 90:
            events.append(
                _event(
                    "gpu",
                    "gpu_temperature_high",
                    "error",
                    "GPU temperature is above the probe threshold",
                    {
                        "uuid": gpu["uuid"],
                        "pci_bdf": gpu["pci_bdf"],
                        "temperature_c": gpu["temperature_c"],
                    },
                )
            )
    return gpus, events


def _read_bounded_sysfs_text(path: Path, limit: int) -> str:
    with path.open("r", encoding="ascii") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("sysfs value exceeds bound")
    return value.strip()


def _pci_root_path(device: Path, bdf: str) -> str:
    resolved = device.resolve(strict=True)
    components: list[str] = []
    for component in resolved.parts:
        if _PCI_ROOT_RE.fullmatch(component):
            components = [component.lower()]
        elif components and _normalize_bdf(component) is not None:
            components.append(_normalize_bdf(component) or "")
    if not components or components[-1] != bdf:
        raise ValueError("invalid PCI sysfs path")
    root_path = "/sys/devices/" + "/".join(components)
    if len(root_path) > MAX_PCI_ROOT_PATH_CHARS:
        raise ValueError("PCI root path exceeds bound")
    return root_path


def _valid_pci_root_path(value: str, bdf: str) -> bool:
    prefix = "/sys/devices/"
    if not value.startswith(prefix) or len(value) > MAX_PCI_ROOT_PATH_CHARS:
        return False
    components = value[len(prefix) :].split("/")
    if not components or not _PCI_ROOT_RE.fullmatch(components[0]):
        return False
    normalized_bdfs = [_normalize_bdf(component) for component in components[1:]]
    return bool(normalized_bdfs) and all(normalized_bdfs) and normalized_bdfs[-1] == bdf


def _read_numa_node(device: Path) -> int:
    numa_node = int(_read_bounded_sysfs_text(device / "numa_node", 16))
    if not -1 <= numa_node <= 4095:
        raise ValueError("invalid NUMA node")
    return numa_node


def _read_link_width(device: Path, filename: str) -> int:
    width = int(_read_bounded_sysfs_text(device / filename, 16))
    if not 0 <= width <= 1024:
        raise ValueError("invalid PCI link width")
    return width


def _read_link_speed(device: Path, filename: str) -> str:
    speed = _read_bounded_sysfs_text(device / filename, 32)
    if not _LINK_SPEED_RE.fullmatch(speed):
        raise ValueError("invalid PCI link speed")
    return speed


def _read_pci_gpus() -> list[dict[str, object]]:
    """Read the fixed sysfs PCI inventory without invoking tenant-controlled code."""
    devices: list[dict[str, object]] = []
    for device in sorted(PCI_DEVICES_PATH.iterdir(), key=lambda path: path.name):
        vendor = _read_bounded_sysfs_text(device / "vendor", 16).lower()
        device_class = _read_bounded_sysfs_text(device / "class", 16).lower()
        if vendor != "0x10de" or not device_class.startswith(("0x0300", "0x0302")):
            continue
        bdf = _normalize_bdf(device.name)
        if bdf is None:
            raise ValueError("invalid PCI device name")
        driver_path = device / "driver"
        driver = driver_path.resolve().name if driver_path.is_symlink() else "unbound"
        driver = _inventory_value(driver, 64)
        devices.append(
            {
                "pci_bdf": bdf,
                "driver": driver,
                "pci_root_path": _pci_root_path(device, bdf),
                "numa_node": _read_numa_node(device),
                "current_link_speed": _read_link_speed(
                    device, "current_link_speed"
                ),
                "current_link_width": _read_link_width(
                    device, "current_link_width"
                ),
                "max_link_speed": _read_link_speed(device, "max_link_speed"),
                "max_link_width": _read_link_width(device, "max_link_width"),
            }
        )
        if len(devices) > MAX_PCI_DEVICES:
            raise ValueError("PCI GPU inventory exceeds bound")
    return devices


def _validate_pci_gpus(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_PCI_DEVICES:
        raise ValueError("invalid PCI GPU inventory")
    devices: list[dict[str, object]] = []
    seen_bdfs: set[str] = set()
    required_fields = {
        "pci_bdf",
        "driver",
        "pci_root_path",
        "numa_node",
        "current_link_speed",
        "current_link_width",
        "max_link_speed",
        "max_link_width",
    }
    for item in value:
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ValueError("invalid PCI GPU record")
        bdf_value = item.get("pci_bdf")
        driver_value = item.get("driver")
        root_path = item.get("pci_root_path")
        numa_node = item.get("numa_node")
        if not isinstance(bdf_value, str):
            raise TypeError("invalid PCI BDF")
        bdf = _normalize_bdf(bdf_value)
        if bdf is None or bdf in seen_bdfs:
            raise ValueError("invalid or duplicate PCI BDF")
        if (
            not isinstance(driver_value, str)
            or not driver_value
            or len(driver_value) > 64
        ):
            raise ValueError("invalid PCI driver")
        if (
            not isinstance(root_path, str)
            or not _valid_pci_root_path(root_path, bdf)
        ):
            raise ValueError("invalid PCI root path")
        if (
            isinstance(numa_node, bool)
            or not isinstance(numa_node, int)
            or not -1 <= numa_node <= 4095
        ):
            raise ValueError("invalid NUMA node")
        record: dict[str, object] = {
            "pci_bdf": bdf,
            "driver": driver_value,
            "pci_root_path": root_path,
            "numa_node": numa_node,
        }
        for field in ("current_link_speed", "max_link_speed"):
            speed = item.get(field)
            if not isinstance(speed, str) or not _LINK_SPEED_RE.fullmatch(speed):
                raise ValueError("invalid PCI link speed")
            record[field] = speed
        for field in ("current_link_width", "max_link_width"):
            width = item.get(field)
            if (
                isinstance(width, bool)
                or not isinstance(width, int)
                or not 0 <= width <= 1024
            ):
                raise ValueError("invalid PCI link width")
            record[field] = width
        devices.append(record)
        seen_bdfs.add(bdf)
    devices.sort(key=lambda device: str(device["pci_bdf"]))
    return devices


def _correlate_gpu_inventory(
    visible_gpus: list[dict[str, object]], pci_gpus: list[dict[str, object]]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    events: list[dict[str, object]] = []
    visible_by_bdf = {str(gpu["pci_bdf"]): gpu for gpu in visible_gpus}
    pci_by_bdf = {str(gpu["pci_bdf"]): gpu for gpu in pci_gpus}
    if len(pci_gpus) != EXPECTED_GPU_COUNT:
        events.append(
            _event(
                "gpu",
                "pci_gpu_count_mismatch",
                "error",
                "Physical PCI GPU count differs from expected count",
                {"expected": EXPECTED_GPU_COUNT, "observed": len(pci_gpus)},
            )
        )
    for bdf, pci_gpu in sorted(pci_by_bdf.items()):
        if bdf in visible_by_bdf or pci_gpu["driver"] == "vfio-pci":
            continue
        events.append(
            _event(
                "gpu",
                "gpu_driver_unavailable",
                "error",
                "Physical GPU is unavailable to NVIDIA and is not assigned to VFIO",
                {"pci_bdf": bdf, "driver": pci_gpu["driver"]},
            )
        )
    for bdf in sorted(set(visible_by_bdf) - set(pci_by_bdf)):
        events.append(
            _event(
                "gpu",
                "gpu_missing_from_pci",
                "critical",
                "NVIDIA reported a GPU absent from the physical PCI inventory",
                {"pci_bdf": bdf, "uuid": visible_by_bdf[bdf]["uuid"]},
            )
        )
    snapshot = {
        "expected_count": EXPECTED_GPU_COUNT,
        "pci_count": len(pci_gpus),
        "nvidia_count": len(visible_gpus),
        "vfio_count": sum(gpu["driver"] == "vfio-pci" for gpu in pci_gpus),
        "gpus": visible_gpus,
        "pci_devices": pci_gpus,
    }
    return snapshot, events


def _nonempty_lines(text: str) -> Iterable[str]:
    for line in text.splitlines():
        normalized = line.strip()
        if normalized:
            yield normalized


def _parse_kernel_events(
    text: str, gpu_by_bdf: dict[str, str]
) -> list[dict[str, object]]:
    aggregates: dict[tuple[str, str, str, str], int] = {}
    for line in _nonempty_lines(text):
        xid_match = _XID_RE.search(line)
        if xid_match:
            bdf = _normalize_bdf(xid_match.group("bdf")) or "unknown"
            xid = xid_match.group("xid")
            uuid = gpu_by_bdf.get(bdf, "unknown")
            severity = "critical" if xid == "79" else "error"
            key = (xid, severity, bdf, uuid)
            aggregates[key] = min(aggregates.get(key, 0) + 1, MAX_EVENT_COUNT)

        if "aer:" not in line.lower():
            continue
        bdf_match = _BDF_RE.search(line)
        bdf = _normalize_bdf(bdf_match.group("bdf")) if bdf_match else None
        lower = line.lower()
        if "uncorrected (fatal)" in lower or "fatal error" in lower:
            code, severity = "fatal", "fatal"
        elif "uncorrected" in lower or "non-fatal" in lower:
            code, severity = "nonfatal", "nonfatal"
        elif "corrected" in lower:
            code, severity = "correctable", "correctable"
        else:
            code, severity = "unknown", "warning"
        key = (code, severity, bdf or "unknown", "")
        aggregates[key] = min(aggregates.get(key, 0) + 1, MAX_EVENT_COUNT)

    events: list[dict[str, object]] = []
    for (code, severity, bdf, uuid), count in sorted(aggregates.items()):
        if uuid:
            xid = code
            events.append(
                _event(
                    "xid",
                    xid,
                    severity,
                    f"NVIDIA Xid {xid} observed in the current boot window",
                    {"xid": int(xid), "pci_bdf": bdf, "uuid": uuid},
                    count,
                )
            )
        else:
            events.append(
                _event(
                    "aer",
                    code,
                    severity,
                    "PCIe AER event observed in the current boot window",
                    {"pci_bdf": bdf},
                    count,
                )
            )
    return events


def _parse_cdi_events(text: str) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for line in _nonempty_lines(text):
        lower = line.lower()
        if "cdi" not in lower:
            continue
        if any(token in lower for token in ("unresolvable", "unresolved", "cannot resolve")):
            kind = "unresolvable"
        elif (
            "failed to inject" in lower
            or "injection failed" in lower
            or ("inject" in lower and "failed" in lower)
        ):
            kind = "injection_failed"
        else:
            continue
        counts[kind] = min(counts.get(kind, 0) + 1, MAX_EVENT_COUNT)

    return [
        _event(
            "cdi",
            "device-missing" if kind == "unresolvable" else "injection-failed",
            "error",
            "Docker reported a CDI device injection failure",
            {"failure": kind},
            count,
        )
        for kind, count in sorted(counts.items())
    ]


def _clean_text(value: str, limit: int) -> str:
    return "".join(character if character.isprintable() else "?" for character in value)[
        :limit
    ]


def _parse_containers(text: str) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    containers: list[dict[str, str]] = []
    total = 0
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                raise ValueError
            name_value = json.loads(fields[0])
            status_value = json.loads(fields[1])
            if not isinstance(name_value, str) or not isinstance(status_value, str):
                raise ValueError
            name = _clean_text(name_value, MAX_CONTAINER_NAME_CHARS)
            status = _clean_text(status_value, MAX_CONTAINER_STATUS_CHARS)
            if not name or not status:
                raise ValueError
            total += 1
            if len(containers) < MAX_CONTAINERS:
                containers.append({"name": name, "status": status})
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, _probe_failure("docker_metadata")

    containers.sort(key=lambda container: (container["name"], container["status"]))
    return {
        "total_count": total,
        "listed_count": len(containers),
        "truncated": total > len(containers),
        "containers": containers,
    }, None


def _read_boot_id() -> str:
    return BOOT_ID_PATH.read_text(encoding="ascii").strip().lower()


def _unknown_system_identity() -> dict[str, dict[str, str]]:
    return {
        category: {field: "unknown" for field in fields}
        for category, fields in _DMI_FIELDS.items()
    }


def _read_system_identity() -> dict[str, dict[str, str]]:
    """Read only the fixed motherboard and BIOS identity files from sysfs."""
    identity = _unknown_system_identity()
    for category, fields in _DMI_FIELDS.items():
        for field, filename in fields.items():
            try:
                raw_value = _read_bounded_sysfs_text(
                    DMI_ID_PATH / filename, MAX_INVENTORY_VALUE_CHARS
                )
            except (FileNotFoundError, PermissionError):
                continue
            identity[category][field] = _inventory_value(raw_value)
    return identity


def _validate_system_identity(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != set(_DMI_FIELDS):
        raise ValueError("invalid system identity")
    identity: dict[str, dict[str, str]] = {}
    for category, fields in _DMI_FIELDS.items():
        category_value = value.get(category)
        if not isinstance(category_value, dict) or set(category_value) != set(fields):
            raise ValueError("invalid system identity category")
        identity[category] = {}
        for field in fields:
            raw_value = category_value.get(field)
            if not isinstance(raw_value, str):
                raise TypeError("invalid system identity value")
            cleaned = _inventory_value(raw_value)
            if len(cleaned) > MAX_INVENTORY_VALUE_CHARS:
                raise ValueError("system identity value exceeds bound")
            identity[category][field] = cleaned
    return identity


def _utc_timestamp(now: dt.datetime | None = None) -> str:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc).replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def _unknown_probe_result(
    code: str, now: dt.datetime | None = None, boot_id: str = "unknown",
    hostname_reader: Callable[[], str] = socket.gethostname,
) -> dict[str, object]:
    # Retain the observed hostname even when command admission fails, so the
    # controller can preserve this failure under its existing identity check.
    target = "unknown"
    try:
        candidate = hostname_reader().strip().lower().rstrip(".")
        if len(candidate) <= 253 and _HOSTNAME_RE.fullmatch(candidate):
            target = candidate
    except (OSError, TypeError, ValueError, AttributeError):
        pass
    messages = {
        "probe_admission_busy": "Probe admission is held by prior unknown work",
        "probe_admission_state_unavailable": "Probe admission state is unavailable",
    }
    return {
        "target": target,
        "machine_id": MACHINE_ID,
        "boot_id": boot_id if _BOOT_ID_RE.fullmatch(boot_id) else "unknown",
        "observed_at": _utc_timestamp(now),
        "healthy": False,
        "cleanup_confirmed": False,
        "events": [
            _event(
                "probe",
                code,
                "error",
                messages.get(code, "Probe admission failed"),
                {"cleanup_confirmed": False, "commands_launched": 0},
            )
        ],
        "snapshot": {
            "gpu": {
                "expected_count": EXPECTED_GPU_COUNT,
                "pci_count": None,
                "nvidia_count": 0,
                "vfio_count": None,
                "gpus": [],
                "pci_devices": [],
            },
            "system_identity": _unknown_system_identity(),
            "services": {
                "vastai": "unknown",
                "docker": "unknown",
                "nvidia-persistenced": "unknown",
            },
            "docker": None,
        },
    }


def collect_probe(
    runner: Callable[[str], CommandResult] = run_command,
    hostname_reader: Callable[[], str] = socket.gethostname,
    boot_id_reader: Callable[[], str] = _read_boot_id,
    pci_gpu_reader: Callable[[], list[dict[str, object]]] = _read_pci_gpus,
    now: dt.datetime | None = None,
    system_identity_reader: Callable[
        [], dict[str, dict[str, str]]
    ] = _read_system_identity,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Collect one observation. Injectable readers exist solely for offline tests."""
    if runner is run_command:
        try:
            current_boot_id = boot_id_reader().strip().lower()
            if not _BOOT_ID_RE.fullmatch(current_boot_id):
                raise AdmissionError("invalid boot identity")
            admission = _acquire_admission(current_boot_id)
        except AdmissionBusy:
            return _unknown_probe_result(
                "probe_admission_busy", now, locals().get("current_boot_id", "unknown"),
                hostname_reader,
            )
        except (OSError, UnicodeError, ValueError, AdmissionError):
            return _unknown_probe_result(
                "probe_admission_state_unavailable",
                now,
                locals().get("current_boot_id", "unknown"),
                hostname_reader,
            )
        try:
            return collect_probe(
                runner=admission.run_command,
                hostname_reader=hostname_reader,
                boot_id_reader=lambda: current_boot_id,
                pci_gpu_reader=pci_gpu_reader,
                now=now,
                system_identity_reader=system_identity_reader,
                monotonic=monotonic,
            )
        finally:
            admission.close()

    events: list[dict[str, object]] = []
    service_ids = (
        ("vastai", "service_vastai"),
        ("docker", "service_docker"),
        ("nvidia-persistenced", "service_nvidia_persistenced"),
    )
    collector_order = (
        "hostname",
        "boot_id",
        "system_identity",
        "gpu_inventory",
        "pci_gpu_inventory",
        "kernel_journal",
        "docker_journal",
        *(f"systemd_{name.replace('-', '_')}" for name, _ in service_ids),
        "docker_metadata",
    )
    deadline = monotonic() + COLLECTION_DEADLINE_SECONDS
    attempted_collectors: list[str] = []
    completed_collectors: list[str] = []
    deadline_exhausted = False
    command_cleanup_confirmed = True
    cleanup_blocked_after: str | None = None

    def begin_collector(collector: str) -> float | None:
        nonlocal deadline_exhausted
        remaining = deadline - monotonic()
        if remaining <= 0:
            deadline_exhausted = True
            return None
        attempted_collectors.append(collector)
        return remaining

    def finish_collector(collector: str) -> None:
        nonlocal deadline_exhausted
        completed_collectors.append(collector)
        if monotonic() >= deadline:
            deadline_exhausted = True

    def collect_command(command_id: str, collector: str) -> CommandResult | None:
        nonlocal command_cleanup_confirmed, cleanup_blocked_after
        if not command_cleanup_confirmed:
            return None
        remaining = begin_collector(collector)
        if remaining is None:
            return None
        if isinstance(getattr(runner, "__self__", None), _Admission):
            result = runner(command_id, remaining)  # type: ignore[call-arg]
        elif runner is run_command:
            result = run_command(command_id, remaining)
        else:
            result = runner(command_id)
        if not result.cleanup_confirmed:
            command_cleanup_confirmed = False
            cleanup_blocked_after = collector
        finish_collector(collector)
        return result

    target = "unknown"
    if begin_collector("hostname") is not None:
        try:
            raw_target = hostname_reader()
            if not isinstance(raw_target, str):
                raise TypeError
            target = _clean_text(raw_target.strip().lower().rstrip("."), 253)
            if not _HOSTNAME_RE.fullmatch(target):
                raise ValueError
        except (OSError, TypeError, ValueError):
            target = "unknown"
            events.append(_probe_failure("hostname"))
        finish_collector("hostname")

    boot_id = "unknown"
    if begin_collector("boot_id") is not None:
        try:
            raw_boot_id = boot_id_reader()
            if not isinstance(raw_boot_id, str):
                raise TypeError
            boot_id = raw_boot_id.strip().lower()
            if not _BOOT_ID_RE.fullmatch(boot_id):
                raise ValueError
        except (OSError, UnicodeError, TypeError, ValueError):
            boot_id = "unknown"
            events.append(_probe_failure("boot_id"))
        finish_collector("boot_id")

    system_identity = _unknown_system_identity()
    if begin_collector("system_identity") is not None:
        try:
            system_identity = _validate_system_identity(system_identity_reader())
        except (OSError, UnicodeError, TypeError, ValueError):
            events.append(_probe_failure("system_identity"))
        finish_collector("system_identity")

    gpus: list[dict[str, object]] = []
    gpu_result = collect_command("gpu", "gpu_inventory")
    if gpu_result is not None:
        failure = _command_failure("gpu_inventory", gpu_result)
        if failure:
            events.append(failure)
        else:
            gpus, gpu_events = _parse_gpus(gpu_result.stdout)
            events.extend(gpu_events)
    gpu_by_bdf = {str(gpu["pci_bdf"]): str(gpu["uuid"]) for gpu in gpus}
    gpu_snapshot: dict[str, object] = {
        "expected_count": EXPECTED_GPU_COUNT,
        "pci_count": None,
        "nvidia_count": len(gpus),
        "vfio_count": None,
        "gpus": gpus,
        "pci_devices": [],
    }

    if begin_collector("pci_gpu_inventory") is not None:
        try:
            pci_gpus = _validate_pci_gpus(pci_gpu_reader())
            gpu_snapshot, gpu_correlation_events = _correlate_gpu_inventory(
                gpus, pci_gpus
            )
            events.extend(gpu_correlation_events)
        except (OSError, UnicodeError, TypeError, ValueError):
            events.append(_probe_failure("pci_gpu_inventory"))
        finish_collector("pci_gpu_inventory")

    kernel_result = collect_command("kernel_journal", "kernel_journal")
    if kernel_result is not None:
        failure = _command_failure("kernel_journal", kernel_result)
        if failure:
            events.append(failure)
        else:
            events.extend(_parse_kernel_events(kernel_result.stdout, gpu_by_bdf))

    docker_journal_result = collect_command("docker_journal", "docker_journal")
    if docker_journal_result is not None:
        failure = _command_failure("docker_journal", docker_journal_result)
        if failure:
            events.append(failure)
        else:
            events.extend(_parse_cdi_events(docker_journal_result.stdout))

    service_states: dict[str, str] = {
        service_name: "unknown" for service_name, _ in service_ids
    }
    valid_states = {
        "active",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "maintenance",
        "unknown",
    }
    for service_name, command_id in service_ids:
        collector_name = f"systemd_{service_name.replace('-', '_')}"
        service_result = collect_command(command_id, collector_name)
        if service_result is None:
            continue
        if service_result.failure:
            events.append(_probe_failure(collector_name, service_result.failure))
            continue
        state = service_result.stdout.strip().lower()
        if state not in valid_states or (service_result.returncode == 0) != (state == "active"):
            events.append(_probe_failure(collector_name))
            continue
        service_states[service_name] = state
        if state != "active":
            events.append(
                _event(
                    "systemd",
                    f"service_{service_name.replace('-', '_')}_not_active",
                    "error",
                    f"Required service {service_name} is not active",
                    {"service": service_name, "state": state},
                )
            )
    metadata: dict[str, object] | None = None
    containers_result = collect_command("docker_metadata", "docker_metadata")
    if containers_result is not None:
        failure = _command_failure("docker_metadata", containers_result)
        if failure:
            events.append(failure)
        else:
            metadata, parse_failure = _parse_containers(containers_result.stdout)
            if parse_failure:
                events.append(parse_failure)

    deadline_event = None
    if deadline_exhausted:
        deadline_event = _event(
            "probe",
            "collection_deadline_exceeded",
            "error",
            "Probe aggregate collection deadline was exhausted",
            {
                "deadline_seconds": COLLECTION_DEADLINE_SECONDS,
                "completed_collectors": completed_collectors,
                "skipped_collectors": [
                    collector
                    for collector in collector_order
                    if collector not in attempted_collectors
                ],
            },
        )
        events.append(deadline_event)

    if not command_cleanup_confirmed:
        events.append(
            _event(
                "probe",
                "collection_aborted_unconfirmed_cleanup",
                "error",
                "Remaining command collectors were not launched after cleanup could not be confirmed",
                {
                    "cleanup_confirmed": False,
                    "blocked_after": cleanup_blocked_after or "unknown",
                    "skipped_collectors": [
                        collector
                        for collector in collector_order
                        if collector not in attempted_collectors
                    ],
                },
            )
        )

    if len(events) > MAX_EVENTS:
        original_count = len(events)
        limit_event = _event(
            "probe",
            "event_limit_reached",
            "error",
            "Probe event limit reached",
            {"observed": original_count, "emitted": MAX_EVENTS},
        )
        if deadline_event is None:
            events = [*events[: MAX_EVENTS - 1], limit_event]
        else:
            events = [
                *events[: MAX_EVENTS - 2],
                limit_event,
                deadline_event,
            ]

    healthy = not events
    return {
        "target": target,
        "machine_id": MACHINE_ID,
        "boot_id": boot_id,
        "observed_at": _utc_timestamp(now),
        "healthy": healthy,
        "cleanup_confirmed": command_cleanup_confirmed,
        "events": events,
        "snapshot": {
            "gpu": gpu_snapshot,
            "system_identity": system_identity,
            "services": service_states,
            "docker": metadata,
        },
    }


def _encode_result(result: dict[str, object]) -> str:
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) < MAX_JSON_OUTPUT_BYTES:
        return encoded

    fallback = {
        "target": result.get("target", "unknown"),
        "machine_id": MACHINE_ID,
        "boot_id": result.get("boot_id", "unknown"),
        "observed_at": result.get("observed_at", _utc_timestamp()),
        "healthy": False,
        "cleanup_confirmed": False,
        "events": [
            _event(
                "probe",
                "json_output_limit_reached",
                "error",
                "Probe JSON output limit reached",
            )
        ],
    }
    return json.dumps(fallback, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def main() -> None:
    # Deliberately do not inspect argv, environment instructions, or stdin.
    try:
        result = collect_probe()
    except Exception:  # Last-resort schema preservation; exception details stay private.
        result = {
            "target": "unknown",
            "machine_id": MACHINE_ID,
            "boot_id": "unknown",
            "observed_at": _utc_timestamp(),
            "healthy": False,
            "cleanup_confirmed": False,
            "events": [_probe_failure("probe")],
        }
    print(_encode_result(result), flush=True)


if __name__ == "__main__":
    main()
