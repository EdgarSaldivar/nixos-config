"""Bounded command-line entry point; there is deliberately no remote command option."""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .capacity import (
    merge_events,
    prometheus_failure_event,
    reconcile_capacity,
    reconcile_market,
)
from .inventory import Inventory, capture_probe
from .observation_runtime import (
    HeartbeatPublisher,
    ObservationArchive,
    ObservationRuntimeError,
    RuntimeProgress,
)
from .prometheus import (
    AlertFreshness,
    AlertSnapshot,
    AlertState,
    MetricBatch,
    PrometheusAlert,
    PrometheusClient,
    PrometheusError,
)
from .redfish import RedfishClient, RedfishSnapshot, ResourceObservation, SensorReading
from .scheduler import (
    CollectionObservation,
    CollectionScheduler,
    CollectionStatus,
    daemon_collectors,
    in_collector_group,
)
from .state import StateStore
from .supervisor import Supervisor
from .telegram import (
    AuthenticatedInput,
    InputKind,
    SQLiteUpdateBackend,
    TelegramClient,
    TelegramError,
    TelegramUpdateConsumer,
    drain_outbox,
    drain_outbox_semantic,
    normalize_id,
    read_credential,
)
from .vast import VastClient, VastSnapshot
from .webhooks import SQLiteWebhookQueue, TARGET_MACHINE_ID, webhook_server

MAX_PROBE_BYTES = 512 * 1024
MAX_CONFIG_BYTES = 256 * 1024
SSH_REMOTE_COLLECTION_SECONDS = 45.0
SSH_CONNECT_MARGIN_SECONDS = 15.0
SSH_FRAMING_MARGIN_SECONDS = 2.0
SSH_IO_TIMEOUT_SECONDS = (
    SSH_REMOTE_COLLECTION_SECONDS
    + SSH_CONNECT_MARGIN_SECONDS
    + SSH_FRAMING_MARGIN_SECONDS
)
SSH_CLEANUP_GRACE_SECONDS = 0.5
SSH_TARGET = re.compile(r"^[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.:-]*$")
TARGET = "terracompute"


def load_probe(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_PROBE_BYTES:
        raise ValueError("probe exceeds the 512 KiB input limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("probe must be a JSON object")
    return value


def fixed_ssh_probe(
    ssh_binary: str,
    ssh_target: str,
    identity_file: Path,
    known_hosts_file: Path,
) -> dict[str, Any]:
    """Read one forced-command JSON response with an in-flight byte bound."""
    if not SSH_TARGET.fullmatch(ssh_target):
        raise ValueError("SSH target must be a plain user@host value")
    argv = [
        ssh_binary,
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"IdentityFile={identity_file}",
        "-o",
        f"ConnectTimeout={int(SSH_CONNECT_MARGIN_SECONDS)}",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "RequestTTY=no",
        ssh_target,
    ]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=not in_collector_group(),
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
        process.kill()
        raise OSError("ssh stdout pipe unavailable")
    output = bytearray()
    deadline = time.monotonic() + SSH_IO_TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(ssh_binary, SSH_IO_TIMEOUT_SECONDS)
            ready = selector.select(min(remaining, 0.25))
            if not ready:
                if process.poll() is None:
                    continue
                # An exited child can still have bytes buffered in the pipe.
            try:
                chunk = os.read(process.stdout.fileno(), 64 * 1024)
            except BlockingIOError:
                continue
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_PROBE_BYTES:
                raise ValueError("probe exceeds the 512 KiB input limit")
        return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, ssh_binary)
    finally:
        selector.close()
        if in_collector_group():
            # The scheduler owns cancellation of the complete inherited group.
            # Do not create a nested session that could survive controller shutdown.
            if process.poll() is None:
                process.kill()
                process.wait(timeout=SSH_CLEANUP_GRACE_SECONDS)
        elif not _terminate_subprocess_group(process):
            raise RuntimeError("SSH process group termination is unconfirmed")
        process.stdout.close()
    # The scheduler retains a further bounded allowance for JSON framing and
    # process-result serialization after this SSH connection deadline.
    return load_probe(bytes(output))


def _terminate_subprocess_group(process: subprocess.Popen[bytes]) -> bool:
    """Boundedly terminate an SSH process group, including any descendants."""

    def group_alive() -> bool:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    if process.poll() is None or group_alive():
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=SSH_CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None or group_alive():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=SSH_CLEANUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return False
    deadline = time.monotonic() + SSH_CLEANUP_GRACE_SECONDS
    while group_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    return process.poll() is not None and not group_alive()


def _utc(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"{field} must be a bounded absolute path")
    result = Path(value)
    if not result.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return result


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a JSON object")
    return dict(value)


def _only(config: Mapping[str, Any], allowed: set[str], field: str) -> None:
    extras = set(config) - allowed
    if extras:
        raise ValueError(f"{field} contains unsupported configuration keys")


def _enabled(config: Mapping[str, Any]) -> bool:
    value = config.get("enabled", True)
    if not isinstance(value, bool):
        raise ValueError("enabled must be boolean")
    return value


@dataclass(frozen=True)
class SSHConfig:
    target: str
    binary: str
    identity_file: Path
    known_hosts_file: Path


@dataclass(frozen=True)
class PrometheusConfig:
    endpoint: str
    vast_exporter_job: str = "vastai-exporter"
    dcgm_exporter_job: str = "dcgm-exporter"
    max_age_seconds: int = 180


@dataclass(frozen=True)
class VastConfig:
    api_key_file: Path


@dataclass(frozen=True)
class BMCConfig:
    username_file: Path
    password_file: Path
    cert_sha256_file: Path


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    token_file: Path | None
    chat_id_file: Path | None
    group_id: int | None
    inbox_path: Path | None
    input_enabled: bool
    poll_timeout_seconds: int
    bot_username: str = "TerraComputeBot"


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool
    secret_file: Path | None
    queue_path: Path | None
    host: str
    port: int
    max_connections: int
    request_deadline_seconds: float


@dataclass(frozen=True)
class RuntimeConfig:
    state_dir: Path
    ssh: SSHConfig | None
    prometheus: PrometheusConfig | None
    vast: VastConfig | None
    bmc: BMCConfig | None
    telegram: TelegramConfig
    webhook: WebhookConfig
    tick_seconds: float = 0.2


def load_config(path: Path) -> RuntimeConfig:
    """Load nonsecret JSON; integration values are paths, identities, and bounds only."""

    with path.open("rb") as handle:
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("configuration exceeds 256 KiB")
    document = json.loads(raw)
    root = _mapping(document, "configuration")
    _only(
        root,
        {"state_dir", "machine_id", "sources", "telegram", "webhook", "tick_seconds"},
        "configuration",
    )
    if str(root.get("machine_id", "17049")) != "17049":
        raise ValueError("configuration is scoped only to Vast machine 17049")
    state_dir = _path(root.get("state_dir"), "state_dir")
    sources = _mapping(root.get("sources", {}), "sources")
    _only(sources, {"ssh", "prometheus", "vast", "bmc"}, "sources")

    ssh: SSHConfig | None = None
    if "ssh" in sources:
        item = _mapping(sources["ssh"], "sources.ssh")
        _only(
            item,
            {"enabled", "target", "binary", "identity_file", "known_hosts_file"},
            "sources.ssh",
        )
        if _enabled(item):
            target = item.get("target")
            binary = item.get("binary", "ssh")
            if not isinstance(target, str) or not SSH_TARGET.fullmatch(target):
                raise ValueError("sources.ssh.target is invalid")
            if not isinstance(binary, str) or not binary or len(binary) > 4096:
                raise ValueError("sources.ssh.binary is invalid")
            ssh = SSHConfig(
                target,
                binary,
                _path(item.get("identity_file"), "sources.ssh.identity_file"),
                _path(item.get("known_hosts_file"), "sources.ssh.known_hosts_file"),
            )

    prometheus: PrometheusConfig | None = None
    if "prometheus" in sources:
        item = _mapping(sources["prometheus"], "sources.prometheus")
        _only(
            item,
            {
                "enabled",
                "endpoint",
                "vast_exporter_job",
                "dcgm_exporter_job",
                "max_age_seconds",
            },
            "sources.prometheus",
        )
        if _enabled(item):
            age = item.get("max_age_seconds", 180)
            if isinstance(age, bool) or not isinstance(age, int):
                raise ValueError("sources.prometheus.max_age_seconds is invalid")
            endpoint = item.get("endpoint")
            vast_job = item.get("vast_exporter_job", "vastai-exporter")
            dcgm_job = item.get("dcgm_exporter_job", "dcgm-exporter")
            # Constructor validation fixes the endpoint to an API origin and both jobs
            # to label literals; no incident content can select a URL or query.
            probe_client = PrometheusClient(str(endpoint), max_age_seconds=age)
            from .prometheus import validate_job_name

            prometheus = PrometheusConfig(
                probe_client.endpoint,
                validate_job_name(vast_job),
                validate_job_name(dcgm_job),
                age,
            )

    vast: VastConfig | None = None
    if "vast" in sources:
        item = _mapping(sources["vast"], "sources.vast")
        _only(item, {"enabled", "api_key_file"}, "sources.vast")
        if _enabled(item):
            vast = VastConfig(_path(item.get("api_key_file"), "sources.vast.api_key_file"))

    bmc: BMCConfig | None = None
    if "bmc" in sources:
        item = _mapping(sources["bmc"], "sources.bmc")
        _only(
            item,
            {"enabled", "username_file", "password_file", "cert_sha256_file"},
            "sources.bmc",
        )
        if _enabled(item):
            bmc = BMCConfig(
                _path(item.get("username_file"), "sources.bmc.username_file"),
                _path(item.get("password_file"), "sources.bmc.password_file"),
                _path(item.get("cert_sha256_file"), "sources.bmc.cert_sha256_file"),
            )

    telegram_item = _mapping(root.get("telegram", {}), "telegram")
    _only(
        telegram_item,
        {
            "enabled",
            "token_file",
            "chat_id_file",
            "group_id",
            "inbox_path",
            "input_enabled",
            "poll_timeout_seconds",
            "bot_username",
        },
        "telegram",
    )
    telegram_enabled = bool(telegram_item) and _enabled(telegram_item)
    input_enabled = telegram_item.get("input_enabled", False)
    poll_timeout = telegram_item.get("poll_timeout_seconds", 25)
    bot_username = telegram_item.get("bot_username", "TerraComputeBot")
    if (
        not isinstance(input_enabled, bool)
        or isinstance(poll_timeout, bool)
        or not isinstance(poll_timeout, int)
        or not 0 <= poll_timeout <= 50
        or not isinstance(bot_username, str)
        or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username)
    ):
        raise ValueError("telegram input configuration is invalid")
    telegram = TelegramConfig(
        telegram_enabled,
        _path(telegram_item.get("token_file"), "telegram.token_file") if telegram_enabled else None,
        _path(telegram_item.get("chat_id_file"), "telegram.chat_id_file")
        if telegram_enabled
        else None,
        normalize_id(telegram_item["group_id"], "telegram.group_id")
        if telegram_enabled and "group_id" in telegram_item
        else None,
        _path(telegram_item.get("inbox_path"), "telegram.inbox_path")
        if telegram_enabled and input_enabled
        else None,
        input_enabled,
        poll_timeout,
        bot_username,
    )
    if telegram_enabled and input_enabled and telegram.group_id is None:
        raise ValueError("telegram.group_id is required for authenticated input")

    webhook_item = _mapping(root.get("webhook", {}), "webhook")
    _only(
        webhook_item,
        {
            "enabled",
            "secret_file",
            "queue_path",
            "host",
            "port",
            "max_connections",
            "request_deadline_seconds",
        },
        "webhook",
    )
    webhook_enabled = bool(webhook_item) and _enabled(webhook_item)
    host = webhook_item.get("host", "127.0.0.1")
    port = webhook_item.get("port", 0)
    maximum = webhook_item.get("max_connections", 16)
    deadline = webhook_item.get("request_deadline_seconds", 10.0)
    if (
        not isinstance(host, str)
        or isinstance(port, bool)
        or not isinstance(port, int)
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
    ):
        raise ValueError("webhook listener configuration is invalid")
    webhook = WebhookConfig(
        webhook_enabled,
        _path(webhook_item.get("secret_file"), "webhook.secret_file") if webhook_enabled else None,
        _path(webhook_item.get("queue_path"), "webhook.queue_path")
        if (webhook_enabled or "queue_path" in webhook_item)
        else None,
        host,
        port,
        maximum,
        float(deadline),
    )
    tick = root.get("tick_seconds", 0.2)
    if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not 0.02 <= float(tick) <= 5:
        raise ValueError("tick_seconds is invalid")
    return RuntimeConfig(
        state_dir, ssh, prometheus, vast, bmc, telegram, webhook, float(tick)
    )


@dataclass(frozen=True)
class SSHCollector:
    config: SSHConfig

    def __call__(self) -> dict[str, Any]:
        # File validation and SSH credential use occur only inside this child process.
        for required in (self.config.identity_file, self.config.known_hosts_file):
            if not required.is_file() or required.stat().st_size == 0:
                raise ValueError("ssh credential file unavailable")
        return fixed_ssh_probe(
            self.config.binary,
            self.config.target,
            self.config.identity_file,
            self.config.known_hosts_file,
        )


@dataclass(frozen=True)
class PrometheusCollector:
    config: PrometheusConfig

    def __call__(self) -> object:
        try:
            return PrometheusClient(
                self.config.endpoint,
                max_age_seconds=self.config.max_age_seconds,
            ).fetch("17049", self.config.vast_exporter_job, self.config.dcgm_exporter_job)
        except PrometheusError as error:
            return PrometheusFailure(error.reason, error.query_id)


@dataclass(frozen=True)
class PrometheusAlertsCollector:
    config: PrometheusConfig

    def __call__(self) -> object:
        try:
            return PrometheusClient(
                self.config.endpoint,
                max_age_seconds=self.config.max_age_seconds,
            ).fetch_alerts()
        except PrometheusError as error:
            return PrometheusFailure(error.reason, error.query_id)


@dataclass(frozen=True)
class VastCollector:
    config: VastConfig

    def __call__(self) -> VastSnapshot:
        return VastClient(
            read_credential(self.config.api_key_file), timeout_seconds=15
        ).collect()


@dataclass(frozen=True)
class BMCCollector:
    config: BMCConfig

    def __call__(self) -> RedfishSnapshot:
        return RedfishClient(
            read_credential(self.config.username_file),
            read_credential(self.config.password_file),
            read_credential(self.config.cert_sha256_file),
            # Leave two seconds inside the scheduler's 15-second hard boundary so
            # discover() can return and serialize its bounded partial snapshot.
            timeout_seconds=13,
        ).discover()


@dataclass(frozen=True)
class PrometheusFailure:
    reason: str
    query_id: str


def _source_probe(
    source: str,
    *,
    status: str,
    freshness: str,
    events: list[dict[str, object]],
    observed_at: str | None = None,
    boot_id: str = "unknown",
    snapshot: object | None = None,
) -> dict[str, Any]:
    return {
        "target": TARGET,
        "machine_id": "17049",
        "source": source,
        "boot_id": boot_id,
        "source_timestamp": observed_at or _utc(),
        "status": status,
        "freshness": freshness,
        "healthy": status == "healthy" and freshness == "fresh" and not events,
        "events": events,
        "snapshot": snapshot,
    }


def _unknown_source(source: str, category: str) -> dict[str, Any]:
    return _source_probe(
        source,
        status="unknown",
        freshness="unknown",
        events=[
            {
                "fault_family": "source",
                "component": source,
                "code": "source_collection_unknown",
                "severity": "error",
                "message": f"{source} collection did not produce complete evidence",
                "evidence": {"category": category},
            }
        ],
    )


def _vast_summary(snapshot: VastSnapshot) -> dict[str, object]:
    machine = snapshot.machine
    market = snapshot.market
    return {
        "machine": None
        if machine is None
        else {
            "machine_id": machine.machine_id,
            "listed": machine.listed,
            "rentable": machine.rentable,
            "rented": machine.rented,
            "total_gpus": machine.total_gpus,
            "rented_gpus": machine.rented_gpus,
        },
        "reports": None
        if snapshot.reports is None
        else [
            {
                "problem": report.problem,
                "message": report.message,
                "created_at": report.created_at,
            }
            for report in snapshot.reports
        ],
        "market": {
            "search_complete": market.search_complete,
            "advertised": market.advertised,
            "rentable": market.rentable,
            "launch_proven": market.launch_proven,
            "advertised_gpu_capacity": market.advertised_gpu_capacity,
            "rentable_gpu_capacity": market.rentable_gpu_capacity,
            "launch_proven_gpu_capacity": market.launch_proven_gpu_capacity,
            "holds": market.holds,
            "error": market.error,
        },
        "errors": list(snapshot.errors[:16]),
    }


def _alert_identity(alert: PrometheusAlert) -> tuple[str, str]:
    alert_name = alert.labels.get("alertname", "unnamed")
    device = next(
        (
            alert.labels[key]
            for key in ("gpu_uuid", "UUID", "uuid", "gpu", "device", "instance")
            if alert.labels.get(key)
        ),
        "instance-unknown",
    )
    return alert_name, device


def _alert_event(alert: PrometheusAlert) -> dict[str, object]:
    alert_name, device = _alert_identity(alert)
    unknown = alert.state is AlertState.UNKNOWN
    state = alert.state.value
    severity = str(alert.labels.get("severity", "error")).casefold()
    if severity not in {"info", "warning", "error", "critical"}:
        severity = "error"
    return {
        "fault_family": "source" if unknown else "prometheus-alert",
        "component": f"prometheus-alert:{alert_name}",
        "device": device,
        "code": f"prometheus_alert_{state}",
        "severity": "warning" if unknown or alert.state is AlertState.PENDING else severity,
        "message": (
            "Prometheus returned an alert with an unknown state"
            if unknown
            else f"Prometheus alert is {state}"
        ),
        "evidence": {
            "alertname": alert_name,
            "device_or_instance": device,
            "state": state,
            "labels": dict(alert.labels),
            "annotations": dict(alert.annotations),
            "active_at": _utc(alert.active_at) if alert.active_at is not None else None,
            "value": alert.value,
        },
    }


def _prometheus_alerts_probe(
    snapshot: AlertSnapshot, *, now: datetime | None = None
) -> dict[str, Any]:
    freshness = snapshot.freshness_at(now or datetime.now(timezone.utc))
    events = [_alert_event(alert) for alert in snapshot.alerts]
    has_unknown = bool(snapshot.unknown) or freshness is not AlertFreshness.FRESH
    has_active = bool(snapshot.firing) or bool(snapshot.pending)
    status = "unknown" if has_unknown or not snapshot.complete else "unhealthy" if has_active else "healthy"
    return _source_probe(
        "prometheus-alerts",
        status=status,
        # Supervisor rejects future source timestamps before ordering changes;
        # retain the typed detail while exposing the source freshness as unknown.
        freshness=(
            "unknown" if freshness is AlertFreshness.FUTURE else freshness.value
        ),
        events=events,
        observed_at=_utc(snapshot.observed_at),
        snapshot={
            "complete": snapshot.complete,
            "freshness": freshness.value,
            "firing": len(snapshot.firing),
            "pending": len(snapshot.pending),
            "unknown": len(snapshot.unknown),
            "alerts": [
                {
                    "labels": dict(alert.labels),
                    "annotations": dict(alert.annotations),
                    "state": alert.state.value,
                    "active_at": _utc(alert.active_at) if alert.active_at is not None else None,
                    "value": alert.value,
                }
                for alert in snapshot.alerts
            ],
        },
    )


def _sensor_document(item: SensorReading) -> dict[str, object]:
    return asdict(item)


def _resource_document(item: ResourceObservation) -> dict[str, object]:
    return asdict(item)


def _unhealthy_status(state: str | None, health: str | None) -> bool:
    return (health is not None and health.casefold() != "ok") or (
        state is not None
        and state.casefold()
        in {
            "absent",
            "disabled",
            "quiesced",
            "standbyoffline",
            "unavailable",
            "unavailableoffline",
        }
    )


def _redfish_probe(snapshot: RedfishSnapshot) -> dict[str, Any]:
    bad = [
        item
        for item in snapshot.resources
        if (item.health is not None and item.health.lower() not in {"ok"})
        or (item.state is not None and item.state.lower() in {"absent", "disabled", "unavailable"})
    ]
    useful_health = any(
        item.health is not None or item.state is not None for item in snapshot.resources
    )
    events: list[dict[str, object]] = []
    if bad:
        events.append(
            {
                "fault_family": "bmc",
                "component": "redfish",
                "code": "redfish_health_unhealthy",
                "severity": "critical" if any(
                    str(item.health).casefold() == "critical" for item in bad
                ) else "error",
                "message": "Redfish reports unhealthy or unavailable resources",
                "evidence": {"resource_count": len(bad)},
            }
        )
    sensor_event_limit = 128 - int(not snapshot.complete)
    for resource in snapshot.resources:
        for sensor in (*resource.power, *resource.thermal, *resource.sensors):
            if not _unhealthy_status(sensor.state, sensor.health):
                continue
            if len(events) >= sensor_event_limit:
                continue
            identity = sensor.member_id or sensor.name or sensor.kind
            events.append(
                {
                    "fault_family": "bmc",
                    "component": f"{resource.path}:{sensor.kind}:{identity}",
                    "code": "redfish_sensor_unhealthy",
                    "severity": "critical" if str(sensor.health).casefold() == "critical" else "error",
                    "message": "Redfish reports an unhealthy sensor",
                    "evidence": {
                        "resource_path": resource.path,
                        "sensor": _sensor_document(sensor),
                    },
                }
            )
    if not snapshot.complete:
        events.append(
            {
                "fault_family": "source",
                "component": "bmc",
                "code": "redfish_discovery_partial",
                "severity": "error",
                "message": "Redfish discovery is partial",
                "evidence": {"errors": list(snapshot.errors[:16])},
            }
        )
    sensor_unhealthy = any(
        event.get("code") == "redfish_sensor_unhealthy" for event in events
    )
    status = (
        "unhealthy"
        if bad or sensor_unhealthy
        else "unknown"
        if not snapshot.complete or not useful_health
        else "healthy"
    )
    return _source_probe(
        "bmc",
        status=status,
        freshness="fresh",
        events=events,
        observed_at=_utc(snapshot.observed_at),
        snapshot={
            "complete": snapshot.complete,
            # Structured dataclass serialization retains power state, identity,
            # readings, and historical log entries without treating logs as current.
            "resources": [_resource_document(item) for item in snapshot.resources],
            "errors": list(snapshot.errors),
        },
    )


class DaemonRuntime:
    """Parent-owned persistence and correlation around process-isolated collectors."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        store: StateStore | None = None,
        supervisor: Supervisor | None = None,
        execution: object | None = None,
        clock: Callable[[], float] = time.monotonic,
        collector_overrides: Mapping[str, Callable[[], object]] | None = None,
    ):
        self.config = config
        self.store = store or StateStore(config.state_dir)
        self._owns_store = store is None
        self.supervisor = supervisor or Supervisor(
            self.store, expected_machine_id="17049"
        )
        self.latest_ssh: dict[str, Any] | None = None
        self.latest_prometheus: MetricBatch | None = None
        self.latest_vast: VastSnapshot | None = None
        self.persistence_failures = 0
        self._vast_generation = 0
        self._webhook_waiting: dict[str, int] = {}
        overrides = dict(collector_overrides or {})
        alerts_collector = overrides.get("prometheus-alerts") or (
            PrometheusAlertsCollector(config.prometheus) if config.prometheus else None
        )
        specs = daemon_collectors(
            ssh=overrides.get("ssh") or (SSHCollector(config.ssh) if config.ssh else None),
            prometheus=overrides.get("prometheus") or (
                PrometheusCollector(config.prometheus) if config.prometheus else None
            ),
            prometheus_alerts=alerts_collector,
            vast=overrides.get("vast") or (VastCollector(config.vast) if config.vast else None),
            bmc=overrides.get("bmc") or (BMCCollector(config.bmc) if config.bmc else None),
        )
        scheduler_arguments: dict[str, object] = {
            "on_observation": self.on_collection,
            "clock": clock,
        }
        if execution is not None:
            scheduler_arguments["execution"] = execution
        self.scheduler = CollectionScheduler(specs, **scheduler_arguments)  # type: ignore[arg-type]
        self.archive: ObservationArchive | None = None
        self.progress: RuntimeProgress | None = None
        self.heartbeat: HeartbeatPublisher | None = None
        self.inventory: Inventory | None = None
        if isinstance(getattr(self.store, "db", None), sqlite3.Connection):
            self.inventory = Inventory(self.store.db)
            self.archive = ObservationArchive(self.store.db, config.state_dir)
            self.progress = RuntimeProgress(self.store.db)
            self.heartbeat = HeartbeatPublisher(config.state_dir, self.progress)
        self.webhook_queue = (
            SQLiteWebhookQueue(config.webhook.queue_path)
            if config.webhook.queue_path is not None
            else None
        )

    def tick(self) -> tuple[CollectionObservation, ...]:
        if self.webhook_queue is not None:
            for event_id in self.webhook_queue.pending_reconcile_ids():
                if event_id in self._webhook_waiting:
                    continue
                try:
                    running = "vast" in self.scheduler.running
                    self.scheduler.trigger("vast")
                except KeyError:
                    # Keep the durable signal pending until the Vast source is configured.
                    break
                # If Vast is already running, the trigger requests a retained follow-up;
                # the current generation cannot consume this newly arrived signal.
                self._webhook_waiting[event_id] = self._vast_generation + (2 if running else 1)
        observations = self.scheduler.tick()
        if self.heartbeat is not None:
            try:
                self.heartbeat.publish_if_due()
            except ObservationRuntimeError:
                self.persistence_failures += 1
        return observations

    def on_collection(self, observation: CollectionObservation) -> None:
        completed = False
        try:
            if observation.status is not CollectionStatus.SUCCESS:
                completed = self._persist(
                    _unknown_source(
                        observation.source,
                        observation.error_category or observation.status.value,
                    ),
                    material=False,
                )
                return
            if observation.name == "ssh":
                if not isinstance(observation.value, dict):
                    raise ValueError("invalid ssh result")
                probe = dict(observation.value)
                if str(probe.get("machine_id")) != "17049" or probe.get("target") != TARGET:
                    raise ValueError("SSH probe identity mismatch")
                probe["source"] = "ssh"
                probe["freshness"] = "fresh"
                probe["status"] = "healthy" if probe.get("healthy") is True else "unhealthy"
                retained = self._persist(probe, material=False)
                completed = retained
                if retained:
                    self.latest_ssh = probe
                    self._reconcile_capacity()
                    self._reconcile_market()
            elif observation.name == "prometheus":
                if isinstance(observation.value, PrometheusFailure):
                    error = PrometheusError(observation.value.reason, observation.value.query_id)
                    probe = _source_probe(
                        "prometheus",
                        status="unknown",
                        freshness="unknown",
                        events=[prometheus_failure_event(error)],
                    )
                elif isinstance(observation.value, MetricBatch):
                    probe = _source_probe(
                        "prometheus",
                        status="healthy",
                        freshness="fresh",
                        events=[],
                        snapshot={"fixed_query_set": True, "metrics": asdict(observation.value)},
                    )
                else:
                    raise ValueError("invalid prometheus result")
                retained = self._persist(probe, material=False)
                completed = retained
                if retained and isinstance(observation.value, MetricBatch):
                    self.latest_prometheus = observation.value
                    self._reconcile_capacity()
            elif observation.name == "prometheus-alerts":
                if isinstance(observation.value, PrometheusFailure):
                    probe = _source_probe(
                        "prometheus-alerts",
                        status="unknown",
                        freshness="unknown",
                        events=[
                            {
                                "fault_family": "source",
                                "component": "prometheus-alerts",
                                "code": "prometheus_alerts_unknown",
                                "severity": "error",
                                "message": "Prometheus alerts collection is unknown",
                                "evidence": {
                                    "reason": observation.value.reason,
                                    "query_id": observation.value.query_id,
                                },
                            }
                        ],
                    )
                elif isinstance(observation.value, AlertSnapshot):
                    probe = _prometheus_alerts_probe(observation.value)
                else:
                    raise ValueError("invalid prometheus alerts result")
                completed = self._persist(probe, material=True)
            elif observation.name == "vast":
                if not isinstance(observation.value, VastSnapshot):
                    raise ValueError("invalid Vast result")
                events = reconcile_market(
                    self.latest_ssh,
                    observation.value,
                    now=datetime.now(timezone.utc),
                    max_age_seconds=(
                        self.config.prometheus.max_age_seconds
                        if self.config.prometheus is not None
                        else 180
                    ),
                )
                status = (
                    "unknown"
                    if observation.value.machine is None
                    or bool(observation.value.errors)
                    or not observation.value.market.search_complete
                    else "unhealthy"
                    if events
                    else "healthy"
                )
                retained = self._persist(
                    _source_probe(
                        "vast",
                        status=status,
                        freshness="fresh",
                        events=events,
                        observed_at=_utc(observation.value.observed_at),
                        snapshot=_vast_summary(observation.value),
                    ),
                    material=True,
                )
                completed = retained
                if retained:
                    self.latest_vast = observation.value
                    self._complete_vast_generation()
            elif observation.name == "bmc":
                if not isinstance(observation.value, RedfishSnapshot):
                    raise ValueError("invalid BMC result")
                completed = self._persist(_redfish_probe(observation.value), material=True)
            else:
                raise ValueError("unknown collector")
        except Exception:
            # The scheduler deliberately isolates callbacks. Retain a measurable local
            # failure and best-effort source-unknown record without exposing exception text.
            self.persistence_failures += 1
            try:
                self.supervisor.observe(_unknown_source(observation.source, "normalization-failed"))
            except Exception:
                self.persistence_failures += 1

        finally:
            if completed and self.progress is not None:
                try:
                    self.progress.record_collection()
                except ObservationRuntimeError:
                    self.persistence_failures += 1

    def _protected_capture(self, probe: Mapping[str, object]) -> bool:
        if probe.get("status") == "unhealthy":
            return True
        events = probe.get("events")
        if isinstance(events, list) and any(
            isinstance(event, dict)
            and str(event.get("severity", "")).casefold() in {"error", "critical"}
            for event in events
        ):
            return True
        database = getattr(self.store, "db", None)
        source = probe.get("source")
        if isinstance(database, sqlite3.Connection) and isinstance(source, str):
            if source == "ssh":
                previous_capture = database.execute(
                    """SELECT a.document_json
                       FROM inventory_probe_captures AS c
                       JOIN terracompute_observation_artifacts AS a
                         ON a.sha256=c.payload_hash AND a.machine_id='17049'
                            AND a.source='ssh'
                       WHERE c.state='complete'
                       ORDER BY c.observed_at DESC LIMIT 1"""
                ).fetchone()
                # The initial map and changed hardware/software facts use the
                # protected reserve. Temperature and power state remain routine.
                def hardware_facts(document):
                    snapshot = document.get("snapshot", {})
                    gpu = dict(snapshot.get("gpu", {}))
                    gpu["gpus"] = [
                        {key: value for key, value in item.items()
                         if key not in {"temperature_c", "pstate"}}
                        for item in gpu.get("gpus", [])
                    ]
                    return gpu, snapshot.get("system_identity")

                try:
                    changed = previous_capture is None or hardware_facts(probe) != hardware_facts(
                        json.loads(previous_capture[0])
                    )
                except (AttributeError, TypeError, ValueError):
                    # Shape errors are evidence too. Preserve them before the
                    # inventory normalizer reports the malformed observation.
                    return True
                if changed:
                    return True
            previous = database.execute(
                """SELECT status,boot_id FROM source_state
                   WHERE target=? AND source=?""",
                (str(probe.get("target", "")), source.casefold()),
            ).fetchone()
            if previous is not None and (
                str(previous["status"]) != str(probe.get("status", ""))
                or str(previous["boot_id"]) != str(probe.get("boot_id", ""))
            ):
                return True
        return False

    def _storage_observation(
        self,
        *,
        admitted: bool,
        warning: bool,
        reasons: tuple[str, ...],
        projected_usage_bytes: int | None = None,
        projected_free_bytes: int | None = None,
    ) -> None:
        events: list[dict[str, object]] = []
        if warning or not admitted:
            events.append(
                {
                    "fault_family": "storage",
                    "component": "observation-archive",
                    "code": "local_storage_pressure",
                    "severity": "error" if not admitted else "warning",
                    "message": "Local observation storage admission is restricted"
                    if not admitted
                    else "Local observation storage is under pressure",
                    "evidence": {
                        "admitted": admitted,
                        "reasons": list(reasons[:4]),
                        "projected_usage_bytes": projected_usage_bytes,
                        "projected_free_bytes": projected_free_bytes,
                        "no_pruning_attempted": True,
                    },
                }
            )
        self.supervisor.observe(
            _source_probe(
                "observation-storage",
                status="unhealthy" if events else "healthy",
                freshness="fresh",
                events=events,
                observed_at=_utc(self.store.clock()),
                snapshot={
                    "admitted": admitted,
                    "warning": warning,
                    "projected_usage_bytes": projected_usage_bytes,
                    "projected_free_bytes": projected_free_bytes,
                },
            )
        )

    def _persist(self, probe: dict[str, Any], *, material: bool) -> bool:
        retained = probe
        if self.archive is not None:
            capture_class = "protected" if self._protected_capture(probe) else "routine"
            try:
                archived = self.archive.archive(probe, capture_class=capture_class)
            except ObservationRuntimeError as error:
                if error.reason not in {
                    "storage_accounting_limit",
                    "storage_accounting_unavailable",
                    "source_archive_unavailable",
                }:
                    raise
                # If the state database itself is unavailable this best-effort write
                # will also fail; no code path claims that source evidence was saved.
                self._storage_observation(
                    admitted=False,
                    warning=True,
                    reasons=(error.reason,),
                )
                return False
            self._storage_observation(
                admitted=archived.admission.admitted,
                warning=archived.admission.warning,
                reasons=archived.admission.reasons,
                projected_usage_bytes=archived.admission.projected_usage_bytes,
                projected_free_bytes=archived.admission.projected_free_bytes,
            )
            if not archived.saved:
                # The pressure incident above is the only persisted claim.  Never
                # pretend that the rejected source document or inventory was saved.
                return False
            retained = archived.document
        result = self.supervisor.observe(retained)
        if self.inventory is not None and retained.get("source") == "ssh":
            # Supervisor validates source_timestamp; inventory uses observed_at.
            # Validate that independent clock before it can advance its checkpoint.
            observed = datetime.fromisoformat(str(retained["observed_at"]).replace("Z", "+00:00"))
            if observed.tzinfo is None or (observed - self.store.clock()).total_seconds() > 30:
                raise ValueError("inventory timestamp exceeds allowed future skew")
            latest = self.store.db.execute(
                "SELECT MAX(observed_at) FROM inventory_probe_captures WHERE state='complete'"
            ).fetchone()[0]
            if latest is not None and observed < datetime.fromisoformat(latest.replace("Z", "+00:00")):
                # Supervisor has retained this as historical source evidence.
                # Expected ordering rejection is not a new present-time fault.
                return False
            capture_probe(self.inventory, retained)
        if material and result.material_changed and probe.get("source") != "ssh":
            self.scheduler.material_event()
        return True

    def _reconcile_capacity(self) -> None:
        if self.latest_ssh is None or self.latest_prometheus is None:
            return
        events = reconcile_capacity(
            self.latest_ssh,
            self.latest_prometheus,
            now=datetime.now(timezone.utc),
            max_age_seconds=(
                self.config.prometheus.max_age_seconds
                if self.config.prometheus is not None
                else 180
            ),
        )
        self._persist(
            _source_probe(
                "capacity-reconciliation",
                status="unhealthy" if events else "healthy",
                freshness="fresh",
                events=events,
                boot_id=str(self.latest_ssh.get("boot_id", "unknown")),
                snapshot={"sources": ["ssh", "prometheus"]},
            ),
            material=bool(events),
        )

    def _reconcile_market(self) -> None:
        if self.latest_ssh is None or self.latest_vast is None:
            return
        events = reconcile_market(
            self.latest_ssh,
            self.latest_vast,
            now=datetime.now(timezone.utc),
        )
        market_unknown = (
            self.latest_vast.machine is None
            or bool(self.latest_vast.errors)
            or not self.latest_vast.market.search_complete
        )
        self._persist(
            _source_probe(
                "market-reconciliation",
                status="unknown" if market_unknown else "unhealthy" if events else "healthy",
                freshness="fresh",
                events=events,
                boot_id=str(self.latest_ssh.get("boot_id", "unknown")),
                snapshot={"sources": ["ssh", "vast"]},
            ),
            material=bool(events),
        )

    def _complete_vast_generation(self) -> None:
        self._vast_generation += 1
        if self.webhook_queue is None:
            return
        completed = [
            event_id
            for event_id, required in self._webhook_waiting.items()
            if required <= self._vast_generation
        ]
        for event_id in completed:
            self.webhook_queue.mark_reconciled(event_id, TARGET_MACHINE_ID)
            del self._webhook_waiting[event_id]

    def close(self) -> None:
        self.scheduler.shutdown()
        if self.webhook_queue is not None:
            self.webhook_queue.close()
        if self._owns_store:
            self.store.close()


def _stop_flag() -> tuple[Callable[[], bool], Callable[[], None]]:
    stopped = False

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        nonlocal stopped
        stopped = True

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    return lambda: stopped, stop


def run_daemon(config: RuntimeConfig) -> int:
    runtime = DaemonRuntime(config)
    stopped, _stop = _stop_flag()
    try:
        while not stopped():
            runtime.tick()
            time.sleep(config.tick_seconds)
        return 0
    finally:
        runtime.close()


def _acknowledge_if_supported(store: StateStore, envelope: AuthenticatedInput) -> bool:
    if envelope.kind is not InputKind.ACKNOWLEDGEMENT or envelope.subject_id is None:
        return False
    acknowledge = getattr(store, "acknowledge_incident", None)
    if not callable(acknowledge):
        return False
    # The state owner controls the incident transition. This integration passes only the
    # normalized incident ID, never SQL. The durable input retains sender identity.
    return bool(acknowledge(envelope.subject_id))


def run_notify(config: RuntimeConfig) -> int:
    settings = config.telegram
    if not settings.enabled or settings.token_file is None or settings.chat_id_file is None:
        raise ValueError("Telegram notification integration is disabled")
    token = read_credential(settings.token_file)
    chat_id = read_credential(settings.chat_id_file)
    client = TelegramClient(token, timeout=5)
    store = StateStore(config.state_dir)
    progress = (
        RuntimeProgress(store.db)
        if isinstance(getattr(store, "db", None), sqlite3.Connection)
        else None
    )
    stopped, _stop = _stop_flag()
    next_delivery = 0.0
    try:
        while not stopped():
            now = time.monotonic()
            if now >= next_delivery:
                # Collapse each bounded due batch into one operator digest.
                result = drain_outbox_semantic(store, client, chat_id, limit=100)
                if progress is not None and result.failed == 0:
                    # A zero-failure bounded drain either delivered its selected batch
                    # or directly verified that the due outbox was empty.
                    progress.record_notification()
                if result.retry_after is not None:
                    next_delivery = now + result.retry_after
            time.sleep(config.tick_seconds)
        return 0
    finally:
        store.close()


def run_operator_input(config: RuntimeConfig) -> int:
    """Poll authenticated operator input without performing outbound delivery."""

    settings = config.telegram
    if (
        not settings.enabled
        or not settings.input_enabled
        or settings.token_file is None
        or settings.group_id is None
        or settings.inbox_path is None
    ):
        raise ValueError("Telegram operator input integration is disabled")
    client = TelegramClient(read_credential(settings.token_file))
    store = StateStore(config.state_dir)
    backend = SQLiteUpdateBackend(settings.inbox_path)
    consumer = TelegramUpdateConsumer(
        client,
        backend,
        group_id=settings.group_id,
        enabled=True,
        bot_username=settings.bot_username,
    )
    stopped, _stop = _stop_flag()
    try:
        while not stopped():
            try:
                consumer.poll_once(poll_timeout=settings.poll_timeout_seconds)
            except TelegramError:
                # Cursor ordering leaves the unavailable update retryable.
                time.sleep(config.tick_seconds)
            for envelope in backend.pending_inputs(consumer.namespace):
                try:
                    handled = _acknowledge_if_supported(store, envelope)
                except Exception:
                    handled = False
                if handled:
                    backend.mark_handled(consumer.namespace, envelope.update_id)
                # The durable row (including sender identity) is retained after
                # acknowledgement; questions and approvals remain pending future work.
        return 0
    finally:
        backend.close()
        store.close()


def run_webhook(config: RuntimeConfig) -> int:
    settings = config.webhook
    if not settings.enabled or settings.secret_file is None or settings.queue_path is None:
        raise ValueError("webhook integration is disabled")
    secret = read_credential(settings.secret_file)
    queue = SQLiteWebhookQueue(settings.queue_path)
    server = webhook_server(
        settings.host,
        settings.port,
        secret,
        queue,
        max_connections=settings.max_connections,
        request_deadline=settings.request_deadline_seconds,
    )
    try:
        server.serve_forever(poll_interval=0.5)
        return 0
    finally:
        server.server_close()
        queue.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="terracompute-ops")
    sub = result.add_subparsers(dest="action", required=True)
    observe = sub.add_parser("observe", help="process normalized JSON from stdin")
    observe.add_argument("--state-dir", required=True, type=Path)
    observe.add_argument("--machine-id", default="17049")

    run = sub.add_parser("run", help="perform one bounded observation and notify")
    run.add_argument("--state-dir", required=True, type=Path)
    run.add_argument("--machine-id", default="17049")
    run.add_argument("--ssh-target", required=True)
    run.add_argument("--ssh-binary", default="ssh")
    run.add_argument("--ssh-identity", required=True, type=Path)
    run.add_argument("--known-hosts", required=True, type=Path)
    run.add_argument("--prometheus-endpoint", required=True)
    run.add_argument("--vast-exporter-job", default="vastai-exporter")
    run.add_argument("--dcgm-exporter-job", default="dcgm-exporter")
    run.add_argument("--max-metric-age-seconds", default=180, type=int)
    run.add_argument("--telegram-token", required=True, type=Path)
    run.add_argument("--telegram-chat-id", required=True, type=Path)

    daemon = sub.add_parser("daemon", help="run independent observation collectors")
    daemon.add_argument("--config", required=True, type=Path)
    notify = sub.add_parser("notify", help="run dedicated outbound Telegram delivery")
    notify.add_argument("--config", required=True, type=Path)
    operator_input = sub.add_parser(
        "operator-input", help="run dedicated authenticated Telegram input"
    )
    operator_input.add_argument("--config", required=True, type=Path)
    webhook = sub.add_parser("webhook", help="run loopback signed Vast webhook ingress")
    webhook.add_argument("--config", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.action in {"daemon", "notify", "operator-input", "webhook"}:
        try:
            config = load_config(args.config)
            if args.action == "daemon":
                return run_daemon(config)
            if args.action == "notify":
                return run_notify(config)
            if args.action == "operator-input":
                return run_operator_input(config)
            return run_webhook(config)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            print(
                f"terracompute-ops: {args.action} failed ({type(error).__name__})",
                file=sys.stderr,
            )
            return 1
    store = StateStore(args.state_dir)
    try:
        supervisor = Supervisor(store, expected_machine_id=args.machine_id)
        if args.action == "observe":
            result = supervisor.observe(load_probe(sys.stdin.buffer.read(MAX_PROBE_BYTES + 1)))
            print(
                json.dumps(
                    {
                        "healthy": result.healthy,
                        "created": len(result.created),
                        "duplicates": result.duplicates,
                    },
                    sort_keys=True,
                )
            )
            return 0

        prometheus = PrometheusClient(
            args.prometheus_endpoint,
            max_age_seconds=args.max_metric_age_seconds,
        )
        # Read all mandatory credentials before contacting the target. Missing or
        # malformed inputs therefore fail the run closed.
        token = read_credential(args.telegram_token)
        chat_id = read_credential(args.telegram_chat_id)
        for required in (args.ssh_identity, args.known_hosts):
            if not required.is_file() or required.stat().st_size == 0:
                raise ValueError("required SSH credential file is absent or empty")
        # An unreachable target must not strand an older notification. Credential
        # validation still happens first, so delivery never weakens fail-closed
        # identity handling.
        drain_outbox(store, token, chat_id)
        probe = fixed_ssh_probe(
            args.ssh_binary, args.ssh_target, args.ssh_identity, args.known_hosts
        )
        try:
            metric_batch = prometheus.fetch(
                args.machine_id, args.vast_exporter_job, args.dcgm_exporter_job
            )
            capacity_events = reconcile_capacity(
                probe,
                metric_batch,
                now=datetime.now(timezone.utc),
                max_age_seconds=args.max_metric_age_seconds,
            )
        except PrometheusError as error:
            capacity_events = [prometheus_failure_event(error)]
        supervisor.observe(merge_events(probe, capacity_events))
        drain_outbox(store, token, chat_id)
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        # Never render exception text: URL-bearing transport errors can include a
        # Telegram bot token, and credential paths do not aid unattended recovery.
        print(f"terracompute-ops: observation failed ({type(error).__name__})", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
