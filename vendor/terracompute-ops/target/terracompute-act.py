#!/usr/bin/python3 -I
"""Forced-command action helper for the terracompute target.

Installed as /usr/local/libexec/terracompute-act and run as root through
``/usr/bin/sudo -n`` from the ``terracompute-actor`` forced SSH command. The only
input is ``SSH_ORIGINAL_COMMAND``; arguments and stdin are ignored. It accepts
exactly one of::

    status dcgm-exporter <request-id>
    restart dcgm-exporter <execution-id>
    result dcgm-exporter <execution-id>

Every external command comes from the fixed COMMANDS table. The only variable
arguments are container IDs that docker itself listed and that pass a strict
64-hex check. The helper prints one compact, sorted-keys JSON object and exits
0 for a protocol response (check ``ok``), 2 for a rejected request and 1 for an
internal failure.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


SCHEMA_VERSION = 1
MACHINE_ID = 17049
EXPECTED_HOSTNAME = "terracompute"
EXPECTED_BOARD = "ROME2D32GM-2T"
COMPONENT = "dcgm-exporter"
OPERATIONS = frozenset({"status", "restart", "result", "inspect", "observe", "session"})

# --- The session channel --------------------------------------------------
#
# ``session`` runs what the agent decides to run, as root, on this host. It is not a
# catalogue and not a sandbox: the agent manages this machine, and a fixed vocabulary
# is what stopped it managing anything.
#
# One boundary is enforced rather than asked for: tenant data. Not because the agent
# cannot be trusted with it, but because it is the channel through which someone
# else's text could reach the agent's judgement. Nine people rent this machine and
# write into its containers; none of that should be able to argue with the operator.
#
# Be precise about how strong that is. ``InaccessiblePaths`` stops these paths being
# read by this session. It cannot stop root from starting an unrestricted process and
# reading them anyway -- root is root. It is a boundary against what flows IN, and it
# holds exactly as long as nothing has already subverted the agent. That is not
# circular: it prevents the input that would cause the bypass.
#
# The remaining seam, stated so nobody rediscovers it: tenant-chosen process names and
# image strings still appear in diagnostics we need. "Which process holds this GPU" has
# to name a tenant's process. Blocking their files and logs closes most of the channel,
# not all of it.
SESSION_COMPONENT = "host"
SYSTEMD_RUN_CANDIDATES = (
    "/usr/bin/systemd-run",
    "/bin/systemd-run",
    "/run/current-system/sw/bin/systemd-run",
)
MAX_SESSION_PAYLOAD_BYTES = 64 * 1024
MAX_SESSION_OUTPUT_BYTES = 256 * 1024
SESSION_SECONDS = 300.0
# Conservative until the box has been enumerated through this very channel; the first
# job of the session is to find out what else on this host holds other people's data.
TENANT_DATA_PATHS = (
    "/var/lib/docker/containers",
    "/var/lib/docker/overlay2",
    "/var/lib/docker/volumes",
    "/var/lib/containerd",
)
# Walled off from an OBSERVATION only. The filesystem paths above stop a direct read of
# tenant data; these stop the read that goes through the runtime instead -- `docker logs`
# or `docker exec` on a tenant container reaches the same data with the files walled. An
# observation never needs to manage a container, so it loses nothing it needs by not
# holding the socket; the critical "who holds the GPU" fact comes from the gpu-handles
# topic, which the helper computes as root outside any session. A management session
# keeps the socket, because restarting or replacing a container is the whole point of it,
# and by then a human has approved the action.
RUNTIME_CONTROL_SOCKETS = (
    "/run/docker.sock",
    "/var/run/docker.sock",
    "/run/containerd/containerd.sock",
)
# Read-only topics. Each names a fixed command or file read; none takes a parameter,
# so nothing a caller sends ever reaches a command line.
READ_TOPICS = (
    "containers",
    "exporter-logs",
    "gpu-inventory",
    "gpu-handles",
    "gpu-processes",
    "kernel-gpu-log",
    "pci-errors",
)

MAX_REQUEST_BYTES = 256
# Room for the full tenant member list of MAX_TENANTS containers in a status response.
MAX_OUTPUT_BYTES = 64 * 1024
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
MAX_LEDGER_RECORD_BYTES = 256 * 1024
MAX_IDENTITY_FILE_BYTES = 256
MAX_LISTED_CONTAINERS = 1024
MAX_TENANTS = 256
MAX_CONTAINER_DEVICES = 64
MAX_PCI_ENTRIES = 4096
MAX_PCI_GPUS = 32
MAX_INSPECT_LINES = 200
MAX_SCANNED_PROCESSES = 4096
MAX_PROCESS_DESCRIPTORS = 1024
MAX_INSPECT_LINE_CHARS = 300

# The kernel drops the driver link before the NVIDIA remove step finishes, so a
# normal VM handover briefly shows the blocked signature. A stuck one persists.
HANDOVER_CONFIRM_SECONDS = 3.0
DOCKER_READ_TIMEOUT_SECONDS = 10.0
RESTART_TIMEOUT_SECONDS = 45.0
PROCESS_KILL_WAIT_SECONDS = 2.0

DOCKER = "/usr/bin/docker"
NVIDIA_SMI = "/usr/bin/nvidia-smi"
DMESG = "/usr/bin/dmesg"
COMMAND_ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}
HOSTNAME_PATH = Path("/etc/hostname")
BOARD_NAME_PATH = Path("/sys/class/dmi/id/board_name")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
PCI_DEVICES_PATH = Path("/sys/bus/pci/devices")
NVIDIA_DRIVER_GPUS_PATH = Path("/proc/driver/nvidia/gpus")
PROC_PATH = Path("/proc")
_AER_FILES = ("aer_dev_correctable", "aer_dev_fatal", "aer_dev_nonfatal")
LEDGER_DIRECTORY = Path("/var/lib/terracompute-actor/ledger")
LEDGER_OWNER_UID = 0

_REQUEST_CHARACTERS_RE = re.compile(r"[a-z0-9 -]+")
_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
_BOARD_RE = re.compile(r"[\x20-\x7e]{1,128}")
_BOOT_ID_RE = _ID_RE
_CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
_CONTAINER_NAME_RE = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_STARTED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z"
)
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_IMAGE_RE = re.compile(r"[\x21-\x7e]{1,256}")
_RUNTIME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_BDF_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
_DRIVER_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_TENANT_PREFIX = "C."
_KVM_DEVICE = "/dev/kvm"

# Docker templates. They request only the container ID and name, running state,
# start time, image, runtime and device host paths; never Env, Labels, Cmd or Mounts.
_LIST_FORMAT = "{{json .ID}}\t{{json .Names}}"
_EXPORTER_INSPECT_FORMAT = (
    "{{json .Id}}\t{{json .Name}}\t{{json .State.Running}}\t"
    "{{json .State.StartedAt}}\t{{json .Config.Image}}\t{{json .HostConfig.Runtime}}"
)
# A read topic, so plain readable fields rather than the TSV the parsers consume.
_CONTAINER_FORMAT = "{{.Names}} | {{.ID}} | {{.State}} | {{.Status}} | {{.Image}}"
_TENANT_INSPECT_FORMAT = (
    "{{json .Id}}\t{{json .Name}}\t{{json .State.StartedAt}}"
    "{{range .HostConfig.Devices}}\t{{json .PathOnHost}}{{end}}"
)


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    timeout_seconds: float
    accepts_container_ids: bool = False
    # Only for log reads, where the program writes its output to both streams.
    merge_stderr: bool = False


# The complete set of subprocess argument vectors the helper can run.
COMMANDS: dict[str, CommandSpec] = {
    "exporter_inspect": CommandSpec(
        (DOCKER, "inspect", "--type", "container", "--format", _EXPORTER_INSPECT_FORMAT, COMPONENT),
        DOCKER_READ_TIMEOUT_SECONDS,
    ),
    "exporter_list": CommandSpec(
        (DOCKER, "ps", "-a", "--no-trunc", "--filter", "name=^/?dcgm-exporter$", "--format", _LIST_FORMAT),
        DOCKER_READ_TIMEOUT_SECONDS,
    ),
    "tenant_list": CommandSpec(
        (DOCKER, "ps", "-a", "--no-trunc", "--filter", "name=C.", "--format", _LIST_FORMAT),
        DOCKER_READ_TIMEOUT_SECONDS,
    ),
    "tenant_inspect": CommandSpec(
        (DOCKER, "inspect", "--type", "container", "--format", _TENANT_INSPECT_FORMAT),
        DOCKER_READ_TIMEOUT_SECONDS,
        accepts_container_ids=True,
    ),
    "exporter_restart": CommandSpec(
        (DOCKER, "restart", "--time", "10", COMPONENT),
        RESTART_TIMEOUT_SECONDS,
    ),
    "exporter_logs": CommandSpec(
        (DOCKER, "logs", "--tail", str(MAX_INSPECT_LINES), "--timestamps", COMPONENT),
        DOCKER_READ_TIMEOUT_SECONDS,
        merge_stderr=True,
    ),
    "container_list": CommandSpec(
        (DOCKER, "ps", "-a", "--no-trunc", "--format", _CONTAINER_FORMAT),
        DOCKER_READ_TIMEOUT_SECONDS,
    ),
    "gpu_processes": CommandSpec(
        (NVIDIA_SMI, "--query-compute-apps=gpu_uuid,pid,process_name,used_memory",
         "--format=csv,noheader"),
        DOCKER_READ_TIMEOUT_SECONDS,
    ),
    "gpu_inventory": CommandSpec(
        (NVIDIA_SMI, "--query-gpu=index,pci.bus_id,name,driver_version,memory.total,"
         "utilization.gpu,persistence_mode", "--format=csv,noheader"),
        DOCKER_READ_TIMEOUT_SECONDS,
    ),
    "kernel_log": CommandSpec((DMESG, "--ctime", "--nopager"), DOCKER_READ_TIMEOUT_SECONDS),
}

RESTART_FAILURE_REASONS = {
    "timeout": "restart_timeout",
    "output_limit": "restart_output_limit",
    "launch_failed": "restart_launch_failed",
}
RECORD_REASONS = frozenset(
    {
        "identity_mismatch",
        "status_before_unavailable",
        "container_absent",
        "restart_timeout",
        "restart_output_limit",
        "restart_launch_failed",
        "restart_failed",
        "restart_nonzero_exit",
        "status_after_unavailable",
        "container_not_running",
        "started_at_unchanged",
        # Written by ``result`` or a replayed ``restart`` while holding the ledger lock.
        "execution_not_started",
        "execution_interrupted",
        "arm_record_failed",
    }
)
ENVELOPE_KEYS = (
    "schema_version",
    "operation",
    "id",
    "component",
    "observed_at",
    "hostname",
    "board",
    "boot_id",
    "machine_id",
    "ok",
)
EXECUTION_FIELDS = (
    "state",
    "requested_at",
    "completed_at",
    "execution_boot_id",
    "exit_code",
    "running_before",
    "running_after",
    "started_at_before",
    "started_at_after",
    "tenants_before",
    "tenants_after",
    "vm_containers_before",
    "vm_containers_after",
    "handover_blocked_before",
    "handover_blocked_after",
)
_SCALAR_EXECUTION_FIELDS = (
    "state",
    "requested_at",
    "completed_at",
    "execution_boot_id",
    "exit_code",
    "running_before",
    "running_after",
    "started_at_before",
    "started_at_after",
    "replayed",
)
_RECORD_KEYS = frozenset({"schema_version", "id", "component", "ok", "reason", *EXECUTION_FIELDS})
_PENDING_KEYS = frozenset(
    {"schema_version", "id", "component", "state", "requested_at", "execution_boot_id"}
)
# A claim is armed, with its baseline, immediately before the only mutating command.
_ARMED_KEYS = _PENDING_KEYS | {"running_before", "started_at_before"}


# --- Bounded subprocesses -------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str = ""
    failure: str | None = None


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    # The group ID stays reserved while its unreaped leader exists, so it is only
    # signalled before this invocation has reaped the leader.
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.wait(timeout=PROCESS_KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _bounded_exec(
    argv: tuple[str, ...],
    timeout_seconds: float,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    merge_stderr: bool = False,
) -> CommandResult:
    """Run one argv without a shell, with a deadline, output cap and PATH-only env."""
    deadline = time.monotonic() + timeout_seconds
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            start_new_session=True,
            env=dict(COMMAND_ENVIRONMENT),
            cwd="/",
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return CommandResult(None, failure="launch_failed")

    assert process.stdout is not None and process.stderr is not None
    captured = bytearray()
    total = 0
    failure: str | None = None
    returncode: int | None = None
    selector = selectors.DefaultSelector()
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map() and failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            for key, _events in selector.select(remaining):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > max_output_bytes:
                    failure = "output_limit"
                    break
                # stderr is drained and counted; returned only for catalogued log reads.
                if key.fileobj is process.stdout or merge_stderr:
                    captured.extend(chunk)
        if failure is None:
            try:
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "timeout"
    except (OSError, ValueError):
        failure = "capture_failed"
    finally:
        selector.close()
        if returncode is None:
            _kill_process_group(process)
        process.stdout.close()
        process.stderr.close()
    if failure is not None or returncode is None:
        return CommandResult(None, failure=failure or "capture_failed")
    return CommandResult(returncode, captured.decode("utf-8", errors="replace"))


def run_command(command_id: str, container_ids: tuple[str, ...] = ()) -> CommandResult:
    """Run only a catalogued command; container IDs must be docker-reported hex IDs."""
    spec = COMMANDS.get(command_id)
    if spec is None:
        return CommandResult(None, failure="unknown_command")
    ids = tuple(container_ids)
    if spec.accepts_container_ids != bool(ids) or len(ids) > MAX_TENANTS:
        return CommandResult(None, failure="invalid_arguments")
    if not all(isinstance(value, str) and _CONTAINER_ID_RE.fullmatch(value) for value in ids):
        return CommandResult(None, failure="invalid_arguments")
    return _bounded_exec(
        spec.argv + ids, spec.timeout_seconds, merge_stderr=spec.merge_stderr
    )


# --- Fixed file readers ---------------------------------------------------


def _read_bounded_file(path: Path, limit: int) -> str:
    with open(path, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file exceeds bound")
    return data.decode("ascii")


def _read_hostname() -> str:
    return _read_bounded_file(HOSTNAME_PATH, MAX_IDENTITY_FILE_BYTES)


def _read_board_name() -> str:
    return _read_bounded_file(BOARD_NAME_PATH, MAX_IDENTITY_FILE_BYTES)


def _read_boot_id() -> str:
    return _read_bounded_file(BOOT_ID_PATH, MAX_IDENTITY_FILE_BYTES)


def _driver_name(device: Path) -> str:
    link = device / "driver"
    if not link.is_symlink():
        return "unbound"
    name = os.path.basename(os.readlink(link))
    return name if _DRIVER_RE.fullmatch(name) else "unknown"


def read_gpu_handles(proc_path: Path = PROC_PATH) -> list[str]:
    """Name the processes holding an NVIDIA device open.

    Only the program name and its container are reported: a command line would carry
    tenant data, and this answer leaves the host.
    """
    lines: list[str] = []
    for name in sorted(os.listdir(proc_path))[:MAX_SCANNED_PROCESSES]:
        if not name.isdigit():
            continue
        process = proc_path / name
        try:
            descriptors = sorted(os.listdir(process / "fd"))[:MAX_PROCESS_DESCRIPTORS]
        except OSError:  # It exited, or it belongs to another user.
            continue
        devices = set()
        for descriptor in descriptors:
            try:
                target = os.readlink(process / "fd" / descriptor)
            except OSError:
                continue
            if target.startswith("/dev/nvidia"):
                devices.add(target.rsplit("/", 1)[-1])
        if not devices:
            continue
        try:
            command = _read_bounded_file(process / "comm", 256).strip()
        except OSError:
            command = "unknown"
        container = "host"
        try:
            for cgroup_line in _read_bounded_file(process / "cgroup", 4096).splitlines():
                match = re.search(r"docker[-/]([0-9a-f]{12,64})", cgroup_line)
                if match:
                    container = match.group(1)[:12]
                    break
        except OSError:
            pass
        lines.append(
            f"pid={name} comm={command} container={container} devices={','.join(sorted(devices))}"
        )
        if len(lines) >= MAX_INSPECT_LINES:
            break
    return lines or ["no process holds an NVIDIA device open"]


def read_pci_errors(pci_devices_path: Path = PCI_DEVICES_PATH) -> list[str]:
    """Report non-zero PCIe AER counters, which is where a failing card shows up."""
    lines: list[str] = []
    for name in sorted(os.listdir(pci_devices_path))[:MAX_PCI_ENTRIES]:
        if not _BDF_RE.fullmatch(name.lower()):
            continue
        device = pci_devices_path / name
        for kind in _AER_FILES:
            try:
                body = _read_bounded_file(device / kind, 4096)
            except OSError:
                continue
            counters = []
            for counter_line in body.splitlines():
                parts = counter_line.split()
                if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) > 0:
                    counters.append(f"{parts[0]}={parts[1]}")
            if counters:
                lines.append(f"{name} {kind.removeprefix('aer_dev_')} {' '.join(counters)}")
    return lines[:MAX_INSPECT_LINES] or ["no non-zero PCIe error counters"]


def read_gpu_functions(pci_devices_path: Path, nvidia_gpus_path: Path) -> list[dict[str, object]]:
    """Read NVIDIA display functions and their handover facts from sysfs and procfs."""
    names = os.listdir(pci_devices_path)
    if len(names) > MAX_PCI_ENTRIES:
        raise ValueError("PCI device listing exceeds bound")
    functions: list[dict[str, object]] = []
    for name in sorted(names):
        bdf = name.lower()
        if not _BDF_RE.fullmatch(bdf):
            raise ValueError("invalid PCI device name")
        device = pci_devices_path / name
        if _read_bounded_file(device / "vendor", 16).strip().lower() != "0x10de":
            continue
        device_class = _read_bounded_file(device / "class", 16).strip().lower()
        if not device_class.startswith(("0x0300", "0x0302")):
            continue
        audio_driver = "absent"
        if bdf.endswith(".0"):
            audio = pci_devices_path / (bdf[:-1] + "1")
            if audio.is_dir():
                audio_driver = _driver_name(audio)
        functions.append(
            {
                "pci_bdf": bdf,
                "driver": _driver_name(device),
                "audio_driver": audio_driver,
                "nvrm_registered": (nvidia_gpus_path / bdf).is_dir(),
            }
        )
        if len(functions) > MAX_PCI_GPUS:
            raise ValueError("PCI GPU inventory exceeds bound")
    return functions


def _read_system_gpu_functions() -> list[dict[str, object]]:
    return read_gpu_functions(PCI_DEVICES_PATH, NVIDIA_DRIVER_GPUS_PATH)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- Session execution ----------------------------------------------------
# Defined above Environment because the dataclass binds them as field defaults, which
# is evaluated when the class is created rather than when a session runs.


def _read_session_payload() -> str:
    """The command arrives on stdin, bounded before anything looks at it."""
    data = sys.stdin.buffer.read(MAX_SESSION_PAYLOAD_BYTES + 1)
    if len(data) > MAX_SESSION_PAYLOAD_BYTES:
        raise ValueError("session payload exceeds bound")
    return data.decode("utf-8", "replace")


def _find_session_launcher() -> str | None:
    for candidate in SYSTEMD_RUN_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _record_session(request_id: str, script: str, writable: bool = False) -> None:
    """Append what was asked for, before it runs. Best effort; never blocks the work.

    ``writable`` is recorded because "the agent changed the machine" and "the agent
    looked at it" are different events, and the reviewer of this log should not have to
    infer which one happened from the command text.
    """
    try:
        LEDGER_DIRECTORY.parent.mkdir(parents=True, exist_ok=True)
        path = LEDGER_DIRECTORY.parent / "session.log"
        record = json.dumps(
            {
                "at": _timestamp(_utc_now()),
                "request": request_id,
                "writable": writable,
                "sha256": hashlib.sha256(script.encode("utf-8", "replace")).hexdigest(),
                "script": script[:MAX_LEDGER_RECORD_BYTES],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(record + "\n")
        os.chmod(path, 0o600)
    except OSError:
        pass


def session_argv(launcher: str, script: str, *, writable: bool) -> tuple[str, ...]:
    """How a session is run: root, on this host, with other people's data walled off.

    Two profiles, and the difference is enforced by the kernel, not by trust:

    - Observation (``writable=False``) mounts every filesystem read-only, so a command
      that tries to change the machine fails with EROFS before it touches anything. This
      is the common path -- the agent looks far more than it changes -- and making it
      unable to write means a mistake or an injected instruction on that path cannot.
    - Management (``writable=True``) can write, restart units and pull images, because
      that is how the machine is managed. It is the rare, approved, audited path.

    Neither is a sandbox against the model: both are root, both keep the network a pull
    needs. What observation removes is the ability to write, nothing more.
    """
    argv = [
        launcher, "--pipe", "--collect", "--wait", "--quiet",
        f"--setenv=PATH={COMMAND_ENVIRONMENT['PATH']}",
        f"--property=RuntimeMaxSec={int(SESSION_SECONDS)}",
        "--property=TasksMax=512",
    ]
    if not writable:
        # The whole filesystem read-only, so an observation cannot leave a trace even
        # if the command it was given tries to. /dev stays as it is: reading a GPU is
        # observation, and this host's fault class needs it.
        argv += ["--property=ProtectSystem=strict", "--property=ProtectHome=read-only"]
    denied = TENANT_DATA_PATHS if writable else TENANT_DATA_PATHS + RUNTIME_CONTROL_SOCKETS
    argv += [f"--property=InaccessiblePaths=-{path}" for path in denied]
    argv += ["/bin/sh", "-c", script]
    return tuple(argv)


def run_session_command(launcher: str, script: str, *, writable: bool) -> CommandResult:
    return _bounded_exec(
        session_argv(launcher, script, writable=writable),
        SESSION_SECONDS + 15.0,
        max_output_bytes=MAX_SESSION_OUTPUT_BYTES,
        merge_stderr=True,
    )


@dataclass
class Environment:
    """Every OS interaction, injectable for offline tests."""

    runner: Callable[..., CommandResult] = run_command
    hostname_reader: Callable[[], str] = _read_hostname
    board_reader: Callable[[], str] = _read_board_name
    boot_id_reader: Callable[[], str] = _read_boot_id
    gpu_reader: Callable[[], list[dict[str, object]]] = _read_system_gpu_functions
    gpu_handle_reader: Callable[[], list[str]] = read_gpu_handles
    pci_error_reader: Callable[[], list[str]] = read_pci_errors
    clock: Callable[[], dt.datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep
    session_payload_reader: Callable[[], str] = _read_session_payload
    session_launcher: Callable[[], str | None] = _find_session_launcher
    session_runner: Callable[..., CommandResult] = run_session_command
    session_auditor: Callable[..., None] = _record_session
    ledger_root: Path = LEDGER_DIRECTORY
    ledger_owner_uid: int = LEDGER_OWNER_UID


# --- Request and envelope -------------------------------------------------


@dataclass(frozen=True)
class Request:
    operation: str
    component: str
    id: str


def parse_request(raw: object) -> Request | None:
    """Accept only the exact three-token grammar; anything else is rejected."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        encoded = raw.encode("ascii")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_REQUEST_BYTES or not _REQUEST_CHARACTERS_RE.fullmatch(raw):
        return None
    tokens = raw.split(" ")
    if len(tokens) != 3:
        return None
    operation, component, request_id = tokens
    if operation not in OPERATIONS or not _ID_RE.fullmatch(request_id):
        return None
    # ``inspect`` names a read topic; ``observe`` and ``session`` name the host; the
    # others name the component. The grammar stays three fixed tokens whatever the
    # operation: a session's command arrives on stdin, never through sshd.
    if operation == "inspect":
        allowed = READ_TOPICS
    elif operation in ("observe", "session"):
        allowed = (SESSION_COMPONENT,)
    else:
        allowed = (COMPONENT,)
    if component not in allowed:
        return None
    return Request(operation, component, request_id)


def _timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now(env: Environment) -> str:
    try:
        return _timestamp(env.clock())
    except Exception:  # The envelope must still be produced after a clock fault.
        return _timestamp(_utc_now())


def _hostname(env: Environment) -> str:
    try:
        value = env.hostname_reader().strip()
    except Exception:  # Unreadable identity is reported, never raised.
        return "unknown"
    return value if _HOSTNAME_RE.fullmatch(value) else "unknown"


def _board(env: Environment) -> str:
    try:
        value = env.board_reader().strip()
    except Exception:  # Unreadable identity is reported, never raised.
        return "unknown"
    return value if _BOARD_RE.fullmatch(value) else "unknown"


def _boot_id(env: Environment) -> str:
    try:
        value = env.boot_id_reader().strip().lower()
    except Exception:  # Unreadable identity is reported, never raised.
        return "unknown"
    return value if _BOOT_ID_RE.fullmatch(value) else "unknown"


def _identity_matches(env: Environment) -> bool:
    return _hostname(env) == EXPECTED_HOSTNAME and _board(env) == EXPECTED_BOARD


def _envelope(env: Environment, request: Request | None) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": request.operation if request else None,
        "id": request.id if request else None,
        "component": request.component if request else None,
        "observed_at": _now(env),
        "hostname": _hostname(env),
        "board": _board(env),
        "boot_id": _boot_id(env),
        "machine_id": MACHINE_ID,
        "ok": False,
    }


def _finish(response: dict[str, object], ok: bool, reason: str | None) -> dict[str, object]:
    response["ok"] = bool(ok)
    response.pop("reason", None)
    if not ok:
        response["reason"] = reason or "internal_error"
    return response


def encode_response(response: dict[str, object]) -> str:
    """Encode compactly with sorted keys, falling back to a bounded object.

    An oversized execution response keeps its outcome and scalar fields and drops only
    the tenant and GPU lists (the ledger keeps them), so a real restart result is never
    reported as a failure. Any other oversized response becomes a bounded failure.
    """
    encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded) + 1 <= MAX_OUTPUT_BYTES:
        return encoded
    fallback = {key: response.get(key) for key in ENVELOPE_KEYS}
    for key in _SCALAR_EXECUTION_FIELDS:
        if key in response:
            fallback[key] = response[key]
    if "state" in response:
        fallback["ok"] = response.get("ok") is True
        if not fallback["ok"]:
            fallback["reason"] = response.get("reason") or "internal_error"
        fallback["truncated"] = True
    else:
        fallback["ok"] = False
        fallback["reason"] = "output_limit"
    return json.dumps(fallback, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# --- Status collection ----------------------------------------------------


class CollectionError(Exception):
    pass


_COLLECTION_ERRORS = (CollectionError, OSError, UnicodeError, ValueError, TypeError, KeyError)


def _require_success(result: CommandResult) -> str:
    if result.failure is not None or result.returncode != 0:
        raise CollectionError("docker read failed")
    return result.stdout


def _json_fields(line: str) -> list[object]:
    return [json.loads(field) for field in line.split("\t")]


def _lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line]


def _parse_container_list(text: str) -> list[tuple[str, list[str]]]:
    entries: list[tuple[str, list[str]]] = []
    for line in _lines(text):
        fields = _json_fields(line)
        if len(fields) != 2:
            raise ValueError("unexpected container list fields")
        container_id, names = fields
        if not isinstance(container_id, str) or not _CONTAINER_ID_RE.fullmatch(container_id):
            raise ValueError("invalid container id")
        if not isinstance(names, str):
            raise ValueError("invalid container names")
        entries.append((container_id, names.split(",")))
        if len(entries) > MAX_LISTED_CONTAINERS:
            raise ValueError("container list exceeds bound")
    return entries


def _parse_exporter_inspect(text: str) -> dict[str, object]:
    lines = _lines(text)
    if len(lines) != 1:
        raise ValueError("expected one exporter")
    fields = _json_fields(lines[0])
    if len(fields) != 6:
        raise ValueError("unexpected exporter fields")
    container_id, name, running, started_at, image, runtime = fields
    if (
        not isinstance(container_id, str)
        or not _CONTAINER_ID_RE.fullmatch(container_id)
        or name != "/" + COMPONENT
        or not isinstance(running, bool)
        or not isinstance(started_at, str)
        or not _STARTED_AT_RE.fullmatch(started_at)
        or not isinstance(image, str)
        or not _IMAGE_RE.fullmatch(image)
        or not isinstance(runtime, str)
        or not _RUNTIME_RE.fullmatch(runtime)
    ):
        raise ValueError("invalid exporter inspect output")
    return {
        "present": True,
        "running": running,
        "started_at": started_at,
        "image": image,
        "runtime": runtime,
    }


def _collect_container(runner: Callable[..., CommandResult]) -> dict[str, object]:
    inspected = runner("exporter_inspect", ())
    if inspected.failure is None and inspected.returncode == 0:
        return _parse_exporter_inspect(inspected.stdout)
    if inspected.failure is not None:
        raise CollectionError("exporter inspect failed")
    # A non-zero inspect is either an absent container or a docker fault.
    listed = _parse_container_list(_require_success(runner("exporter_list", ())))
    if any(COMPONENT in names for _container_id, names in listed):
        raise CollectionError("exporter is listed but could not be inspected")
    return {"present": False, "running": False, "started_at": None, "image": None, "runtime": None}


def _parse_tenant_inspect(text: str, requested: list[str]) -> list[dict[str, object]]:
    expected = set(requested)
    seen: set[str] = set()
    rows: list[dict[str, object]] = []
    for line in _lines(text):
        fields = _json_fields(line)
        if not 3 <= len(fields) <= 3 + MAX_CONTAINER_DEVICES:
            raise ValueError("unexpected tenant inspect fields")
        container_id, name, started_at = fields[:3]
        devices = fields[3:]
        if (
            not isinstance(container_id, str)
            or container_id not in expected
            or container_id in seen
            or not isinstance(name, str)
            or not _CONTAINER_NAME_RE.fullmatch(name)
            or not isinstance(started_at, str)
            or not _STARTED_AT_RE.fullmatch(started_at)
            or not all(isinstance(device, str) for device in devices)
        ):
            raise ValueError("invalid tenant inspect output")
        seen.add(container_id)
        if not name.startswith("/" + _TENANT_PREFIX):
            continue
        rows.append(
            {
                "id": container_id,
                "name": name[1:],
                "started_at": started_at,
                "kvm": _KVM_DEVICE in devices,
            }
        )
    if seen != expected:
        raise ValueError("tenant inspect output is incomplete")
    return rows


def tenant_digest(rows: list[dict[str, object]]) -> str:
    """sha256 over sorted ``name id started_at`` lines, each terminated by a newline."""
    lines = sorted(f"{row['name']} {row['id']} {row['started_at']}" for row in rows)
    return hashlib.sha256("".join(line + "\n" for line in lines).encode("ascii")).hexdigest()


def _collect_tenants(
    runner: Callable[..., CommandResult],
) -> tuple[dict[str, object], list[str]]:
    listed = _parse_container_list(_require_success(runner("tenant_list", ())))
    # docker treats the name filter as an unanchored regex, so filter strictly here.
    candidates = sorted(
        {
            container_id
            for container_id, names in listed
            if any(name.startswith(_TENANT_PREFIX) for name in names)
        }
    )
    if len(candidates) > MAX_TENANTS:
        raise ValueError("tenant count exceeds bound")
    rows: list[dict[str, object]] = []
    if candidates:
        inspected = _require_success(runner("tenant_inspect", tuple(candidates)))
        rows = _parse_tenant_inspect(inspected, candidates)
    names = sorted(str(row["name"]) for row in rows)
    # Per-tenant identity lets the controller tell a restarted or recreated tenant apart
    # from the expected removal of a stuck VM rental.
    members = sorted(
        ({"name": str(row["name"]), "id": str(row["id"]), "started_at": str(row["started_at"])}
         for row in rows),
        key=lambda member: (member["name"], member["id"]),
    )
    tenants = {"count": len(rows), "digest": tenant_digest(rows), "names": names, "members": members}
    vm_containers = sorted(str(row["name"]) for row in rows if row["kvm"])
    return tenants, vm_containers


def _blocked_bdfs(functions: list[dict[str, object]]) -> set[str]:
    if not isinstance(functions, list) or len(functions) > MAX_PCI_GPUS:
        raise ValueError("invalid GPU function inventory")
    blocked: set[str] = set()
    for function in functions:
        bdf = function["pci_bdf"]
        if not isinstance(bdf, str) or not _BDF_RE.fullmatch(bdf):
            raise ValueError("invalid GPU function address")
        if (
            function["driver"] == "unbound"
            and function["audio_driver"] == "vfio-pci"
            and function["nvrm_registered"] is True
        ):
            blocked.add(bdf)
    return blocked


def _collect_gpu_state(env: Environment) -> dict[str, object]:
    functions = env.gpu_reader()
    candidates = _blocked_bdfs(functions)
    confirmed: set[str] = set()
    if candidates:
        env.sleep(HANDOVER_CONFIRM_SECONDS)
        functions = env.gpu_reader()
        confirmed = _blocked_bdfs(functions) & candidates
    return {
        "handover_blocked": sorted(confirmed),
        "nvidia_visible_count": sum(1 for function in functions if function["driver"] == "nvidia"),
        "pci_gpu_count": len(functions),
    }


def collect_status(env: Environment) -> tuple[dict[str, object], list[str]]:
    """Collect the status fields; a failed part is null and named in the failures."""
    snapshot: dict[str, object] = {
        "container": None,
        "handover_blocked": None,
        "nvidia_visible_count": None,
        "pci_gpu_count": None,
        "tenants": None,
        "vm_containers": None,
    }
    failures: list[str] = []
    try:
        snapshot["container"] = _collect_container(env.runner)
    except _COLLECTION_ERRORS:
        failures.append("container_unavailable")
    try:
        snapshot["tenants"], snapshot["vm_containers"] = _collect_tenants(env.runner)
    except _COLLECTION_ERRORS:
        failures.append("tenants_unavailable")
    try:
        snapshot.update(_collect_gpu_state(env))
    except _COLLECTION_ERRORS:
        failures.append("gpu_state_unavailable")
    return snapshot, failures


# --- Ledger ---------------------------------------------------------------


class LedgerError(Exception):
    pass


_INVALID_RECORD = object()


class Ledger:
    """Root-only execution ledger, addressed through one verified directory descriptor."""

    def __init__(self, root: Path, owner_uid: int) -> None:
        self.owner_uid = owner_uid
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(self.fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise LedgerError("ledger directory is not owner-only")
        except BaseException:
            os.close(self.fd)
            raise

    def close(self) -> None:
        os.close(self.fd)

    def try_lock(self) -> bool:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            return False
        return True

    def read(self, execution_id: str) -> object:
        """Return the stored JSON value, None when absent, or _INVALID_RECORD."""
        try:
            fd = os.open(
                f"{execution_id}.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.fd,
            )
        except FileNotFoundError:
            return None
        except OSError:
            return _INVALID_RECORD
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.owner_uid
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                return _INVALID_RECORD
            data = bytearray()
            while len(data) <= MAX_LEDGER_RECORD_BYTES:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > MAX_LEDGER_RECORD_BYTES:
                return _INVALID_RECORD
            return json.loads(data.decode("ascii"))
        except (OSError, UnicodeError, ValueError):
            return _INVALID_RECORD
        finally:
            os.close(fd)

    def _write_new(self, name: str, record: dict[str, object]) -> None:
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("ascii")
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def create_pending(self, execution_id: str, record: dict[str, object]) -> bool:
        """Claim the execution ID with O_CREAT|O_EXCL; False when it already exists."""
        return self.create(execution_id, record)

    def create(self, execution_id: str, record: dict[str, object]) -> bool:
        """Create the record for a new execution ID; False when it already exists."""
        try:
            self._write_new(f"{execution_id}.json", record)
        except FileExistsError:
            return False
        os.fsync(self.fd)
        return True

    def replace(self, execution_id: str, record: dict[str, object]) -> None:
        """Atomically replace the pending record with the final record."""
        temporary = f".{execution_id}.tmp"
        try:
            os.unlink(temporary, dir_fd=self.fd)
        except FileNotFoundError:
            pass
        self._write_new(temporary, record)
        os.replace(temporary, f"{execution_id}.json", src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)


def _optional(value: object, kind: type, pattern: re.Pattern[str] | None = None) -> bool:
    if value is None:
        return True
    if kind is int and isinstance(value, bool):
        return False
    if not isinstance(value, kind):
        return False
    return pattern is None or bool(pattern.fullmatch(value))  # type: ignore[arg-type]


def _valid_tenants(value: object) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"count", "digest", "names", "members"}
        and isinstance(value["members"], list)
        and all(
            isinstance(member, dict)
            and set(member) == {"name", "id", "started_at"}
            and all(isinstance(member[key], str) for key in member)
            for member in value["members"]
        )
        and isinstance(value["count"], int)
        and not isinstance(value["count"], bool)
        and isinstance(value["digest"], str)
        and bool(_DIGEST_RE.fullmatch(value["digest"]))
        and isinstance(value["names"], list)
        and all(isinstance(name, str) for name in value["names"])
    )


def _valid_string_list(value: object) -> bool:
    return value is None or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def _unknown_view(reason: str, pending: dict[str, object] | None = None) -> dict[str, object]:
    view: dict[str, object] = {field: None for field in EXECUTION_FIELDS}
    view["state"] = "unknown"
    if pending is not None:
        view["requested_at"] = pending["requested_at"]
        view["execution_boot_id"] = pending["execution_boot_id"]
        view["running_before"] = pending.get("running_before")
        view["started_at_before"] = pending.get("started_at_before")
    view["ok"] = False
    view["reason"] = reason
    return view


def _record_view(stored: object, execution_id: str) -> dict[str, object]:
    """Validate a stored record; anything not a complete final record is unknown."""
    if not isinstance(stored, dict) or stored.get("schema_version") != SCHEMA_VERSION:
        return _unknown_view("ledger_record_invalid")
    if stored.get("id") != execution_id or stored.get("component") != COMPONENT:
        return _unknown_view("ledger_record_invalid")
    if stored.get("state") in ("pending", "armed"):
        if (
            set(stored) == (_PENDING_KEYS if stored["state"] == "pending" else _ARMED_KEYS)
            and isinstance(stored["requested_at"], str)
            and _TIMESTAMP_RE.fullmatch(stored["requested_at"])
            and isinstance(stored["execution_boot_id"], str)
            and len(stored["execution_boot_id"]) <= 64
            and _optional(stored.get("running_before"), bool)
            and _optional(stored.get("started_at_before"), str, _STARTED_AT_RE)
        ):
            return _unknown_view("execution_state_unknown", stored)
        return _unknown_view("ledger_record_invalid")
    valid = (
        set(stored) == _RECORD_KEYS
        and stored["state"] in ("refused", "executed", "interrupted")
        and isinstance(stored["ok"], bool)
        and (stored["reason"] is None if stored["ok"] else stored["reason"] in RECORD_REASONS)
        and (not stored["ok"] or stored["state"] == "executed")
        and _optional(stored["requested_at"], str, _TIMESTAMP_RE)
        and _optional(stored["completed_at"], str, _TIMESTAMP_RE)
        and isinstance(stored["execution_boot_id"], str)
        and len(stored["execution_boot_id"]) <= 64
        and _optional(stored["exit_code"], int)
        and _optional(stored["running_before"], bool)
        and _optional(stored["running_after"], bool)
        and _optional(stored["started_at_before"], str, _STARTED_AT_RE)
        and _optional(stored["started_at_after"], str, _STARTED_AT_RE)
        and _valid_tenants(stored["tenants_before"])
        and _valid_tenants(stored["tenants_after"])
        and _valid_string_list(stored["vm_containers_before"])
        and _valid_string_list(stored["vm_containers_after"])
        and _valid_string_list(stored["handover_blocked_before"])
        and _valid_string_list(stored["handover_blocked_after"])
    )
    if not valid:
        return _unknown_view("ledger_record_invalid")
    view = {field: stored[field] for field in EXECUTION_FIELDS}
    view["ok"] = stored["ok"]
    view["reason"] = stored["reason"]
    return view


# --- Operations -----------------------------------------------------------


def _status(env: Environment, request: Request) -> dict[str, object]:
    snapshot, failures = collect_status(env)
    response = _envelope(env, request)
    response.update(snapshot)
    return _finish(response, not failures, failures[0] if failures else None)


def _execution_response(
    env: Environment, request: Request, view: dict[str, object], replayed: bool | None
) -> dict[str, object]:
    response = _envelope(env, request)
    for field in EXECUTION_FIELDS:
        response[field] = view[field]
    if replayed is not None:
        response["replayed"] = replayed
    return _finish(response, bool(view["ok"]), view["reason"])  # type: ignore[arg-type]


def _record_status(record: dict[str, object], snapshot: dict[str, object], suffix: str) -> None:
    container = snapshot["container"]
    if isinstance(container, dict):
        record[f"running_{suffix}"] = container["running"]
        record[f"started_at_{suffix}"] = container["started_at"]
    record[f"tenants_{suffix}"] = snapshot["tenants"]
    record[f"vm_containers_{suffix}"] = snapshot["vm_containers"]
    record[f"handover_blocked_{suffix}"] = snapshot["handover_blocked"]


def _execute_restart(
    env: Environment,
    request: Request,
    pending: dict[str, object],
    arm: Callable[[dict[str, object]], bool],
) -> dict[str, object]:
    """Verify, restart exactly once and verify again. Never retries.

    ``arm`` durably replaces the claim with an armed record carrying the baseline
    start time, immediately before ``docker restart``. A claim that was never armed
    therefore proves the restart never started.
    """
    record: dict[str, object] = {field: None for field in EXECUTION_FIELDS}
    record.update(
        schema_version=SCHEMA_VERSION,
        id=request.id,
        component=COMPONENT,
        state="refused",
        requested_at=pending["requested_at"],
        execution_boot_id=pending["execution_boot_id"],
        ok=False,
        reason=None,
    )

    def refuse(reason: str) -> dict[str, object]:
        record["reason"] = reason
        record["completed_at"] = _now(env)
        return record

    if not _identity_matches(env):
        return refuse("identity_mismatch")
    before, before_failures = collect_status(env)
    _record_status(record, before, "before")
    container = before["container"]
    if isinstance(container, dict) and container["present"] is False:
        return refuse("container_absent")
    if before_failures:
        return refuse("status_before_unavailable")
    # Identity is confirmed again immediately before the only mutating command.
    if not _identity_matches(env):
        return refuse("identity_mismatch")
    armed = dict(pending, state="armed", running_before=record["running_before"],
                 started_at_before=record["started_at_before"])
    if not arm(armed):
        return refuse("arm_record_failed")

    result = env.runner("exporter_restart", ())
    record["state"] = "executed"
    record["exit_code"] = result.returncode if result.failure is None else None
    after, _after_failures = collect_status(env)
    _record_status(record, after, "after")
    after_container = after["container"]

    reason: str | None = None
    if result.failure is not None:
        reason = RESTART_FAILURE_REASONS.get(result.failure, "restart_failed")
    elif result.returncode != 0:
        reason = "restart_nonzero_exit"
    elif not isinstance(after_container, dict):
        reason = "status_after_unavailable"
    elif after_container["present"] is not True or after_container["running"] is not True:
        reason = "container_not_running"
    elif (
        record["started_at_before"] is None
        or record["started_at_after"] == record["started_at_before"]
    ):
        reason = "started_at_unchanged"
    # Tenant changes are recorded for the controller, which owns that judgement.
    record["ok"] = reason is None
    record["reason"] = reason
    record["completed_at"] = _now(env)
    return record


def _final_record(
    env: Environment, request: Request, state: str, reason: str, pending: dict[str, object] | None
) -> dict[str, object]:
    record: dict[str, object] = {field: None for field in EXECUTION_FIELDS}
    record.update(
        schema_version=SCHEMA_VERSION,
        id=request.id,
        component=COMPONENT,
        state=state,
        requested_at=None if pending is None else pending["requested_at"],
        completed_at=_now(env),
        execution_boot_id=_boot_id(env) if pending is None else pending["execution_boot_id"],
        running_before=None if pending is None else pending.get("running_before"),
        started_at_before=None if pending is None else pending.get("started_at_before"),
        ok=False,
        reason=reason,
    )
    return record


def _settled_view(
    env: Environment, request: Request, ledger: Ledger, stored: object
) -> dict[str, object]:
    """View a stored record while holding the ledger lock.

    Every helper run holds the lock from before its claim until it exits, so a claim
    seen under the lock belongs to a run that has died, and is finalized:

    - never armed: ``docker restart`` was never started (``execution_not_started``);
    - armed: it may have been started (``execution_interrupted``, with the baseline).

    Finalizing stops any later helper run for the ID. A ``docker restart`` the dead run
    had already started may still complete in the daemon, so the controller waits
    before judging an interrupted record.
    """
    view = _record_view(stored, request.id)
    if view["state"] != "unknown" or view["reason"] != "execution_state_unknown":
        return view
    if not isinstance(stored, dict) or stored.get("state") not in ("pending", "armed"):
        return view
    if stored["state"] == "pending":
        record = _final_record(env, request, "refused", "execution_not_started", stored)
    else:
        record = _final_record(env, request, "interrupted", "execution_interrupted", stored)
    try:
        ledger.replace(request.id, record)
    except OSError:
        return view
    return _record_view(record, request.id)


def _open_ledger(env: Environment) -> Ledger | None:
    try:
        return Ledger(env.ledger_root, env.ledger_owner_uid)
    except (OSError, LedgerError):
        return None


def _redact_process(line: str) -> str:
    """Keep the program name, drop the path: tenant paths carry customer identity.

    nvidia-smi does not quote its CSV and a tenant chooses its own path, so the name
    field is taken from both ends rather than by counting separators: anything between
    the pid and the memory figure is the path, however many separators it contains.
    """
    uuid, found, rest = line.partition(", ")
    if not found:
        return line
    pid, found, rest = rest.partition(", ")
    if not found:
        return line
    name, found, memory = rest.rpartition(", ")
    if not found:
        return line
    return ", ".join((uuid, pid, name.rsplit("/", 1)[-1], memory))


# Topics answered by reading /proc and sysfs rather than by running a command.
READ_READERS = {"gpu-handles": "gpu_handle_reader", "pci-errors": "pci_error_reader"}
# Each remaining topic names its catalogued command and how its output is cleaned.
READ_SOURCES: dict[str, tuple[str, Callable[[str], str] | None]] = {
    "containers": ("container_list", None),
    "exporter-logs": ("exporter_logs", None),
    "gpu-inventory": ("gpu_inventory", None),
    "gpu-processes": ("gpu_processes", _redact_process),
    "kernel-gpu-log": ("kernel_log", None),
}
_KERNEL_LOG_RE = re.compile(r"NVRM|nvidia|vfio|pcieport|IOMMU|Xid|AER", re.I)
_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")


def _session(env: Environment, request: Request, *, writable: bool) -> dict[str, object]:
    """Run what the agent decided to run, as root, and report what happened.

    The command arrives on stdin rather than in SSH_ORIGINAL_COMMAND, so the forced
    command's grammar stays three fixed tokens and nothing of arbitrary length is ever
    parsed by sshd. ``writable`` picks the profile: an observation cannot write, a
    management session can. What bounds either is not a vocabulary: it is the tenant-data
    boundary, the read-only mount on the observe path, a wall-clock cap, an output cap,
    and a record written before it runs.
    """
    response = _envelope(env, request)
    response["writable"] = writable
    try:
        script = env.session_payload_reader()
    except (OSError, ValueError, UnicodeError):
        return _finish(response, False, "session_payload_unreadable")
    if not script.strip():
        return _finish(response, False, "session_payload_missing")
    if "\x00" in script:
        return _finish(response, False, "session_payload_invalid")
    launcher = env.session_launcher()
    if launcher is None:
        # Failing closed here is deliberate: without systemd-run there is no tenant
        # boundary and no read-only mount, and running anyway would quietly remove the
        # walls we enforce.
        return _finish(response, False, "session_boundary_unavailable")
    # Recorded before execution, so a command that panics the box is still attributable.
    env.session_auditor(request.id, script, writable)
    outcome = env.session_runner(launcher, script, writable=writable)
    if outcome.failure is not None:
        response["lines"] = []
        response["truncated"] = False
        return _finish(response, False, f"session_{outcome.failure}")
    lines = outcome.stdout.splitlines()
    kept = [
        _PRINTABLE_RE.sub(" ", line)[:MAX_INSPECT_LINE_CHARS]
        for line in lines[-MAX_INSPECT_LINES:]
    ]
    response["lines"] = kept
    response["truncated"] = len(lines) > len(kept)
    response["exit_code"] = outcome.returncode
    return _finish(response, True, None)


def _inspect(env: Environment, request: Request) -> dict[str, object]:
    """Answer one catalogued read. It changes nothing on the host."""
    response = _envelope(env, request)
    response["topic"] = request.component
    if request.component in READ_READERS:
        try:
            lines, failure = getattr(env, READ_READERS[request.component])(), None
        except (OSError, ValueError, UnicodeError) as error:
            lines, failure = [], f"read_failed_{type(error).__name__.lower()}"[:64]
        clean = None
    else:
        command_id, clean = READ_SOURCES[request.component]
        result = env.runner(command_id, ())
        if result.failure is not None or result.returncode != 0:
            lines, failure = [], result.failure or "read_failed"
        else:
            lines, failure = result.stdout.splitlines(), None
    if request.component == "kernel-gpu-log":
        lines = [line for line in lines if _KERNEL_LOG_RE.search(line)]
    if clean is not None:
        lines = [clean(line) for line in lines]
    # Printable ASCII only: a log line is data from the machine, not a control sequence.
    kept = [
        _PRINTABLE_RE.sub(" ", line)[:MAX_INSPECT_LINE_CHARS]
        for line in lines[-MAX_INSPECT_LINES:]
    ]
    response["lines"] = kept
    response["truncated"] = len(lines) > len(kept)
    return _finish(response, failure is None, failure)


def _restart(env: Environment, request: Request) -> dict[str, object]:
    ledger = _open_ledger(env)
    if ledger is None:
        return _finish(_envelope(env, request), False, "ledger_unavailable")
    try:
        if not ledger.try_lock():
            return _finish(_envelope(env, request), False, "actor_busy")
        stored = ledger.read(request.id)
        if stored is not None:
            return _execution_response(env, request, _settled_view(env, request, ledger, stored), True)
        pending: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "id": request.id,
            "component": COMPONENT,
            "state": "pending",
            "requested_at": _now(env),
            "execution_boot_id": _boot_id(env),
        }
        try:
            created = ledger.create_pending(request.id, pending)
        except OSError:
            return _finish(_envelope(env, request), False, "ledger_unavailable")
        if not created:
            stored = ledger.read(request.id)
            return _execution_response(env, request, _settled_view(env, request, ledger, stored), True)

        def arm(armed: dict[str, object]) -> bool:
            try:
                ledger.replace(request.id, armed)
            except OSError:
                return False
            return True

        record = _execute_restart(env, request, pending, arm)
        view = _record_view(record, request.id)
        try:
            ledger.replace(request.id, record)
        except OSError:
            response = _execution_response(env, request, dict(record), False)
            return _finish(response, False, "ledger_write_failed")
        return _execution_response(env, request, view, False)
    finally:
        ledger.close()


def _result(env: Environment, request: Request) -> dict[str, object]:
    ledger = _open_ledger(env)
    if ledger is None:
        return _finish(_envelope(env, request), False, "ledger_unavailable")
    try:
        locked = ledger.try_lock()
        stored = ledger.read(request.id)
        if not locked:
            # A run holds the lock. It may be this execution, even before its claim.
            if stored is None:
                return _finish(_envelope(env, request), False, "execution_in_progress")
            view = _record_view(stored, request.id)
            if view["state"] == "unknown" and view["reason"] == "execution_state_unknown":
                view["reason"] = "execution_in_progress"
            return _execution_response(env, request, view, None)
        if stored is None:
            # No run holds the lock and none claimed this ID. Record that, so a late
            # restart request for the same ID replays the refusal instead of acting.
            tombstone = _final_record(env, request, "refused", "execution_not_started", None)
            try:
                created = ledger.create(request.id, tombstone)
            except OSError:
                return _finish(_envelope(env, request), False, "ledger_unavailable")
            stored = tombstone if created else ledger.read(request.id)
        return _execution_response(env, request, _settled_view(env, request, ledger, stored), None)
    finally:
        ledger.close()


# Operations a read-only key may never reach: the two that change the machine. A
# read-only key exists so the investigator -- the service that holds the model -- can
# look at the target directly for its own reasoning, while remaining unable to change
# it. Mutation authority stays solely with the actor key the actions service holds.
READONLY_FORBIDDEN = frozenset({"restart", "session"})


def handle(
    request: Request | None, env: Environment, *, read_only: bool = False
) -> tuple[dict[str, object], int]:
    if request is None:
        return _finish(_envelope(env, None), False, "invalid_request"), 2
    if read_only and request.operation in READONLY_FORBIDDEN:
        # This key cannot change the machine, whatever it is asked for. The refusal is
        # not a policy the caller could argue with; the key simply has no such reach.
        return _finish(_envelope(env, request), False, "operation_not_permitted_readonly"), 2
    if request.operation == "status":
        return _status(env, request), 0
    if request.operation == "restart":
        return _restart(env, request), 0
    if request.operation == "inspect":
        return _inspect(env, request), 0
    if request.operation == "observe":
        return _session(env, request, writable=False), 0
    if request.operation == "session":
        return _session(env, request, writable=True), 0
    return _result(env, request), 0


def _read_only_mode(argv: Sequence[str] | None) -> bool:
    """Whether this invocation is the read-only key.

    Decided by the forced command's own arguments, which the target's authorized_keys
    sets and the SSH client cannot influence -- unlike SSH_ORIGINAL_COMMAND, which is
    the untrusted request. The read-only key's forced command is
    ``terracompute-act readonly``; the actor key's is ``terracompute-act``.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    return tokens[:1] == ["readonly"]


def main(
    environ: Mapping[str, str] | None = None,
    environment: Environment | None = None,
    argv: Sequence[str] | None = None,
) -> int:
    # The only request is SSH_ORIGINAL_COMMAND; stdin carries a session's script. argv is
    # not request data -- it is the forced command's own fixed arguments, read only to
    # tell the read-only key from the actor key.
    source = os.environ if environ is None else environ
    env = environment or Environment()
    read_only = _read_only_mode(argv)
    request = parse_request(source.get("SSH_ORIGINAL_COMMAND"))
    try:
        response, exit_code = handle(request, env, read_only=read_only)
    except Exception:  # Last-resort schema preservation; exception details stay private.
        response, exit_code = _finish(_envelope(env, request), False, "internal_error"), 1
    sys.stdout.write(encode_response(response) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    # A dropped SSH session must not interrupt a restart before its outcome is recorded.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    sys.exit(main())
