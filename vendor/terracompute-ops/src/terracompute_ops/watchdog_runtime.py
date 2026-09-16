"""Local, bounded runtime for the independent controller watchdog.

The evaluator and handoff remain credential-free.  The deployed notifier accepts one
Healthchecks.io ping URL from a credential file and reduces it immediately to two
private request paths.  It has no polling, approval, or action interface.  The older
transition outbox primitives remain available for local compatibility.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import socket
import sqlite3
import ssl
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from .maintenance import WatchdogResult, evaluate_watchdog
from .observation_runtime import HEARTBEAT_SCHEMA_VERSION, MAX_HEARTBEAT_BYTES
from .state import MACHINE_ID


WATCHDOG_OUTBOX_SCHEMA_VERSION = 1
MAX_HANDOFF_RESPONSE_BYTES = 4 * 1024
MAX_TELEGRAM_RESPONSE_BYTES = 256 * 1024
MAX_HEALTHCHECKS_RESPONSE_BYTES = 16
MAX_HEALTHCHECKS_CREDENTIAL_BYTES = 256
MAX_OUTBOX_ROWS = 256
MAX_SEND_BATCH = 8
# This bounds the stored counter; it is not a delivery-abandonment limit.
MAX_DELIVERY_ATTEMPTS = 6
MAX_OPERATION_SECONDS = 30.0
MAX_ALERT_TEXT_BYTES = 1024
WATCHDOG_SENDER_ROLE = "independent-watchdog-notifier"
HEALTHCHECKS_HOST = "hc-ping.com"
FAULT_STATUSES = frozenset({"invalid", "missing", "stale_work"})
HEALTHCHECKS_FAILURE_STATUSES = frozenset(
    {"invalid", "missing", "stale_work", "recovering"}
)
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEALTHCHECKS_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_HEALTHCHECKS_PATH = re.compile(
    r"^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:/fail)?$"
)
_HEARTBEAT_FIELDS = frozenset(
    {
        "schema_version",
        "machine_id",
        "controller_boot_id",
        "sequence",
        "sent_at",
        "collection_progress_at",
        "notification_progress_at",
    }
)
_FORBIDDEN_FIELD_PARTS = (
    "credential",
    "password",
    "secret",
    "tenant",
    "token",
    "rental",
)
_RESULT_REASONS = {
    "starting": frozenset({"startup_grace"}),
    "healthy": frozenset({"heartbeat_healthy", "no_new_heartbeat"}),
    "recovering": frozenset({"stable_recovery_pending", "no_new_heartbeat"}),
    "invalid": frozenset(
        {
            "schema_invalid",
            "wrong_target",
            "boot_id_invalid",
            "sequence_invalid",
            "malformed",
            "future_timestamp",
            "progress_after_send",
            "stale_heartbeat",
            "retired_boot_replay",
            "sequence_replay",
            "boot_history_full",
            "no_new_heartbeat",
        }
    ),
    "missing": frozenset({"heartbeat_missing"}),
    "stale_work": frozenset({"progress_stale", "no_new_heartbeat"}),
}
_RESULT_TRANSITIONS = frozenset(
    {
        "unchanged",
        "healthy_started",
        "invalid_started",
        "missing_started",
        "stale_work_started",
        "recovery_started",
        "recovered",
    }
)


class WatchdogRuntimeError(ValueError):
    """A fixed-category failure at a local watchdog boundary."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class HandoffReceipt:
    accepted: bool
    output: bytes = b""


class HeartbeatExportTransport(Protocol):
    """Injected one-way transport; it must publish one document atomically."""

    def export_atomic(
        self,
        payload: bytes,
        *,
        deadline: float,
        max_output_bytes: int,
    ) -> HandoffReceipt: ...


class HeartbeatReceiveTransport(Protocol):
    """Injected bounded snapshot receiver; ``None`` means no new heartbeat."""

    def receive(
        self,
        *,
        deadline: float,
        max_bytes: int,
    ) -> bytes | None: ...


@dataclass(frozen=True)
class WatchdogSenderIdentity:
    """Non-secret capability declaration for the independent Telegram identity."""

    name: str
    role: str = WATCHDOG_SENDER_ROLE
    consumes_updates: bool = False
    can_approve: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _IDENTITY.fullmatch(self.name)
            or self.role != WATCHDOG_SENDER_ROLE
            or self.consumes_updates
            or self.can_approve
        ):
            raise WatchdogRuntimeError("sender_identity_invalid")


@dataclass(frozen=True)
class WatchdogAlert:
    event_id: int
    machine_id: str
    status: str
    reason: str
    transition: str
    priority: str
    observed_at: str
    text: str
    silent: bool = False


@dataclass(frozen=True)
class AlertSendReceipt:
    """Minimal semantic result accepted from the injected Telegram sender."""

    message_id: int
    delivery_semantics: str = "telegram-accepted"
    output_bytes: int = 0


class WatchdogTelegramSender(Protocol):
    """Send-only interface.  It intentionally has no getUpdates or approval method."""

    identity: WatchdogSenderIdentity

    def send_alert(
        self,
        alert: WatchdogAlert,
        *,
        deadline: float,
        max_response_bytes: int,
    ) -> AlertSendReceipt: ...


@dataclass(frozen=True)
class DrainResult:
    sent: int
    failed: int
    exhausted: int


@dataclass(frozen=True)
class WatchdogTick:
    evaluation: WatchdogResult
    queued_event_id: int | None
    delivery: DrainResult


@dataclass(frozen=True, repr=False)
class HealthchecksHTTPResponse:
    """A bounded, secret-free HTTP response used by injected transports."""

    status: int
    body: bytes

    def __repr__(self) -> str:
        return (
            "HealthchecksHTTPResponse("
            f"status={self.status!r},body=<redacted:{len(self.body)} bytes>)"
        )


class HealthchecksTransport(Protocol):
    """Make one request to the fixed Healthchecks.io origin."""

    def request(
        self,
        path: str,
        *,
        deadline: float,
        max_response_bytes: int,
    ) -> HealthchecksHTTPResponse: ...


class HealthchecksPingURL:
    """Validated private paths derived from a canonical Healthchecks.io ping URL.

    The original URL is deliberately not retained and neither request path is exposed
    through ``repr`` or ``str`` because both contain the secret UUID.
    """

    __slots__ = ("_success_path", "_failure_path")

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise WatchdogRuntimeError("healthchecks_url_invalid")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except (TypeError, ValueError):
            raise WatchdogRuntimeError("healthchecks_url_invalid") from None
        path_parts = parsed.path.split("/")
        if (
            len(value) > MAX_HEALTHCHECKS_CREDENTIAL_BYTES
            or parsed.scheme != "https"
            or parsed.hostname != HEALTHCHECKS_HOST
            or parsed.netloc != HEALTHCHECKS_HOST
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(path_parts) != 2
            or not _HEALTHCHECKS_UUID.fullmatch(path_parts[1])
        ):
            raise WatchdogRuntimeError("healthchecks_url_invalid")
        self._success_path = parsed.path
        self._failure_path = parsed.path + "/fail"

    def __repr__(self) -> str:
        return "HealthchecksPingURL(<redacted>)"

    def __str__(self) -> str:
        return "<redacted-healthchecks-ping-url>"

    def request_path(self, *, success: bool) -> str:
        return self._success_path if success else self._failure_path


def read_healthchecks_ping_url(path: Path) -> HealthchecksPingURL:
    """Read one root-only-style credential without including its value in failures."""

    credential_path = _absolute_path(Path(path), "healthchecks_credential_path_invalid")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(credential_path, flags)
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid not in {0, os.geteuid()}
            or status.st_mode & 0o077
            or status.st_size > MAX_HEALTHCHECKS_CREDENTIAL_BYTES
        ):
            raise WatchdogRuntimeError("healthchecks_credential_invalid")
        raw = os.read(descriptor, MAX_HEALTHCHECKS_CREDENTIAL_BYTES + 1)
    except WatchdogRuntimeError:
        raise
    except OSError:
        raise WatchdogRuntimeError("healthchecks_credential_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > MAX_HEALTHCHECKS_CREDENTIAL_BYTES:
        raise WatchdogRuntimeError("healthchecks_credential_invalid")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw or any(byte <= 0x20 or byte >= 0x7F for byte in raw):
        raise WatchdogRuntimeError("healthchecks_credential_invalid")
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        raise WatchdogRuntimeError("healthchecks_credential_invalid") from None
    return HealthchecksPingURL(value)


class StdlibHealthchecksTransport:
    """HTTPS transport pinned to ``hc-ping.com`` with an absolute deadline.

    ``http.client`` does not use ambient proxy variables and never follows redirects.
    A timer also closes the socket at the absolute deadline so multiple blocking socket
    operations cannot each consume a fresh timeout budget.
    """

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._monotonic = monotonic

    def __repr__(self) -> str:
        return "StdlibHealthchecksTransport(host='hc-ping.com')"

    def request(
        self,
        path: str,
        *,
        deadline: float,
        max_response_bytes: int,
    ) -> HealthchecksHTTPResponse:
        _check_deadline(deadline, self._monotonic, starting=True)
        if (
            not isinstance(path, str)
            or not _HEALTHCHECKS_PATH.fullmatch(path)
            or max_response_bytes != MAX_HEALTHCHECKS_RESPONSE_BYTES
        ):
            raise WatchdogRuntimeError("healthchecks_request_invalid")
        remaining = deadline - self._monotonic()
        connection = http.client.HTTPSConnection(
            HEALTHCHECKS_HOST,
            443,
            timeout=remaining,
            context=self._ssl_context,
        )
        expired = threading.Event()
        connected_socket = None
        response = None

        def expire() -> None:
            expired.set()
            sock = connected_socket or connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

        timer = threading.Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            connected_socket = connection.sock
            _check_deadline(deadline, self._monotonic)
            if expired.is_set():
                raise WatchdogRuntimeError("healthchecks_deadline_expired")
            connection.request(
                "GET",
                path,
                body=None,
                headers={"Accept": "text/plain", "Connection": "close"},
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    declared_length = int(length)
                except ValueError:
                    raise WatchdogRuntimeError("healthchecks_response_invalid") from None
                if not 0 <= declared_length <= max_response_bytes:
                    raise WatchdogRuntimeError("healthchecks_response_too_large")
            body = response.read(max_response_bytes + 1)
            _check_deadline(deadline, self._monotonic)
            if expired.is_set():
                raise WatchdogRuntimeError("healthchecks_deadline_expired")
            if len(body) > max_response_bytes:
                raise WatchdogRuntimeError("healthchecks_response_too_large")
            return HealthchecksHTTPResponse(response.status, body)
        except WatchdogRuntimeError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            raise WatchdogRuntimeError("healthchecks_transport_failed") from None
        finally:
            timer.cancel()
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
            connection.close()


@dataclass(frozen=True)
class HealthchecksPingReceipt:
    success: bool
    response_bytes: int


class HealthchecksPinger:
    """Send only success or failure pings; the credential is never rendered."""

    def __init__(
        self,
        ping_url: HealthchecksPingURL,
        *,
        transport: HealthchecksTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(ping_url, HealthchecksPingURL):
            raise WatchdogRuntimeError("healthchecks_url_invalid")
        self._ping_url = ping_url
        self._transport = transport or StdlibHealthchecksTransport(monotonic=monotonic)
        self._monotonic = monotonic

    def __repr__(self) -> str:
        return "HealthchecksPinger(url=<redacted>)"

    def ping(self, *, success: bool, deadline: float) -> HealthchecksPingReceipt:
        if not isinstance(success, bool):
            raise WatchdogRuntimeError("healthchecks_status_invalid")
        _check_deadline(deadline, self._monotonic, starting=True)
        try:
            response = self._transport.request(
                self._ping_url.request_path(success=success),
                deadline=deadline,
                max_response_bytes=MAX_HEALTHCHECKS_RESPONSE_BYTES,
            )
        except Exception:
            raise WatchdogRuntimeError("healthchecks_transport_failed") from None
        _check_deadline(deadline, self._monotonic)
        if not isinstance(response, HealthchecksHTTPResponse):
            raise WatchdogRuntimeError("healthchecks_response_invalid")
        if response.status != 200:
            raise WatchdogRuntimeError("healthchecks_response_rejected")
        if response.body != b"OK":
            raise WatchdogRuntimeError("healthchecks_response_invalid")
        return HealthchecksPingReceipt(success, len(response.body))


@dataclass(frozen=True)
class HealthchecksTick:
    evaluation: WatchdogResult
    ping: HealthchecksPingReceipt | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise WatchdogRuntimeError("clock_invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _check_deadline(
    deadline: float,
    monotonic: Callable[[], float],
    *,
    starting: bool = False,
) -> None:
    current = monotonic()
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or deadline <= current
        or (starting and deadline - current > MAX_OPERATION_SECONDS)
    ):
        raise WatchdogRuntimeError("deadline_invalid" if starting else "deadline_expired")


def operation_deadline(
    timeout_seconds: float,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= MAX_OPERATION_SECONDS
    ):
        raise WatchdogRuntimeError("timeout_invalid")
    return monotonic() + float(timeout_seconds)


def _absolute_path(path: Path, reason: str) -> Path:
    value = Path(path)
    if not value.is_absolute():
        raise WatchdogRuntimeError(reason)
    return value


def _canonical_heartbeat(heartbeat: Mapping[str, object]) -> bytes:
    if not isinstance(heartbeat, Mapping) or set(heartbeat) != _HEARTBEAT_FIELDS:
        raise WatchdogRuntimeError("heartbeat_fields_invalid")
    for key in heartbeat:
        lowered = key.lower()
        if any(part in lowered for part in _FORBIDDEN_FIELD_PARTS):
            raise WatchdogRuntimeError("heartbeat_sensitive_field")
    if (
        heartbeat.get("schema_version") != HEARTBEAT_SCHEMA_VERSION
        or heartbeat.get("machine_id") != MACHINE_ID
    ):
        raise WatchdogRuntimeError("heartbeat_identity_invalid")
    boot_id = heartbeat.get("controller_boot_id")
    sequence = heartbeat.get("sequence")
    if not isinstance(boot_id, str) or not _IDENTITY.fullmatch(boot_id):
        raise WatchdogRuntimeError("heartbeat_malformed")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or not 0 <= sequence <= 9_223_372_036_854_775_807
    ):
        raise WatchdogRuntimeError("heartbeat_malformed")
    timestamps = []
    for field in ("sent_at", "collection_progress_at", "notification_progress_at"):
        value = heartbeat.get(field)
        if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
            raise WatchdogRuntimeError("heartbeat_malformed")
        try:
            timestamps.append(datetime.fromisoformat(value[:-1] + "+00:00"))
        except ValueError as error:
            raise WatchdogRuntimeError("heartbeat_malformed") from error
    sent_at, collection_at, notification_at = timestamps
    if collection_at > sent_at or notification_at > sent_at:
        raise WatchdogRuntimeError("heartbeat_malformed")
    try:
        payload = json.dumps(
            dict(heartbeat),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii") + b"\n"
    except (TypeError, ValueError, RecursionError) as error:
        raise WatchdogRuntimeError("heartbeat_malformed") from error
    if len(payload) > MAX_HEARTBEAT_BYTES:
        raise WatchdogRuntimeError("heartbeat_oversize")
    return payload


def _atomic_write(path: Path, payload: bytes) -> None:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise WatchdogRuntimeError("handoff_parent_invalid")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise WatchdogRuntimeError("handoff_path_invalid")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".watchdog-handoff-", dir=parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as error:
        raise WatchdogRuntimeError("handoff_write_failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class HeartbeatExporter:
    """Export one exact, credential-free heartbeat to a path or injected transport."""

    def __init__(
        self,
        *,
        handoff_path: Path | None = None,
        transport: HeartbeatExportTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if (handoff_path is None) == (transport is None):
            raise WatchdogRuntimeError("handoff_configuration_invalid")
        self.path = (
            _absolute_path(Path(handoff_path), "handoff_path_not_absolute")
            if handoff_path is not None
            else None
        )
        self.transport = transport
        self.monotonic = monotonic

    def export(self, heartbeat: Mapping[str, object], *, deadline: float) -> HandoffReceipt:
        _check_deadline(deadline, self.monotonic, starting=True)
        payload = _canonical_heartbeat(heartbeat)
        if self.path is not None:
            _atomic_write(self.path, payload)
            receipt = HandoffReceipt(True)
        else:
            assert self.transport is not None
            receipt = self.transport.export_atomic(
                payload,
                deadline=deadline,
                max_output_bytes=MAX_HANDOFF_RESPONSE_BYTES,
            )
            if not isinstance(receipt, HandoffReceipt):
                raise WatchdogRuntimeError("handoff_response_invalid")
            if not isinstance(receipt.output, bytes):
                raise WatchdogRuntimeError("handoff_response_invalid")
            if len(receipt.output) > MAX_HANDOFF_RESPONSE_BYTES:
                raise WatchdogRuntimeError("handoff_response_oversize")
            if not receipt.accepted:
                raise WatchdogRuntimeError("handoff_not_accepted")
        _check_deadline(deadline, self.monotonic)
        return receipt


class HeartbeatReceiver:
    """Receive one bounded heartbeat snapshot without a URL or command surface."""

    def __init__(
        self,
        *,
        handoff_path: Path | None = None,
        transport: HeartbeatReceiveTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if (handoff_path is None) == (transport is None):
            raise WatchdogRuntimeError("receiver_configuration_invalid")
        self.path = (
            _absolute_path(Path(handoff_path), "handoff_path_not_absolute")
            if handoff_path is not None
            else None
        )
        self.transport = transport
        self.monotonic = monotonic
        self._last_snapshot_sha256: str | None = None

    def receive(self, *, deadline: float) -> Mapping[str, object] | None:
        _check_deadline(deadline, self.monotonic, starting=True)
        if self.path is not None:
            if not self.path.exists():
                _check_deadline(deadline, self.monotonic)
                return None
            if self.path.is_symlink() or not self.path.is_file():
                _check_deadline(deadline, self.monotonic)
                return {}
            try:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    status = os.fstat(descriptor)
                    if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_HEARTBEAT_BYTES:
                        _check_deadline(deadline, self.monotonic)
                        return {}
                    payload = os.read(descriptor, MAX_HEARTBEAT_BYTES + 1)
                finally:
                    os.close(descriptor)
            except OSError:
                _check_deadline(deadline, self.monotonic)
                return None
        else:
            assert self.transport is not None
            try:
                payload = self.transport.receive(
                    deadline=deadline,
                    max_bytes=MAX_HEARTBEAT_BYTES,
                )
            except Exception:
                _check_deadline(deadline, self.monotonic)
                return None
            if payload is None:
                _check_deadline(deadline, self.monotonic)
                return None
        _check_deadline(deadline, self.monotonic)
        if not isinstance(payload, bytes) or len(payload) > MAX_HEARTBEAT_BYTES:
            return {}
        digest = hashlib.sha256(payload).hexdigest()
        if digest == self._last_snapshot_sha256:
            return None
        self._last_snapshot_sha256 = digest
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return {}
        return value if isinstance(value, dict) else {}


def _alert_text(status: str, reason: str) -> str:
    descriptions = {
        "invalid": "heartbeat rejected",
        "missing": "controller heartbeat missing",
        "stale_work": "controller work progress stale",
        "healthy": "controller heartbeat recovered after stable window",
    }
    text = (
        f"Terracompute independent watchdog: machine {MACHINE_ID} "
        f"{descriptions[status]} ({reason})."
    )
    if len(text.encode("utf-8")) > MAX_ALERT_TEXT_BYTES:
        raise WatchdogRuntimeError("alert_text_oversize")
    return text


class WatchdogOutbox:
    """Bounded durable transition outbox, independent of controller state."""

    def __init__(self, path: Path):
        self.path = _absolute_path(Path(path), "outbox_path_not_absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise WatchdogRuntimeError("outbox_path_invalid")
        try:
            self.connection = sqlite3.connect(self.path, timeout=5)
            self.connection.row_factory = sqlite3.Row
            self.connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS watchdog_runtime_schema (
                  schema_version INTEGER NOT NULL CHECK(schema_version = 1)
                );
                INSERT INTO watchdog_runtime_schema(schema_version)
                  SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM watchdog_runtime_schema);
                CREATE TABLE IF NOT EXISTS watchdog_runtime_state (
                  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                  machine_id TEXT NOT NULL CHECK(machine_id = '17049'),
                  last_status TEXT,
                  last_reason TEXT
                );
                INSERT OR IGNORE INTO watchdog_runtime_state(
                  singleton,machine_id,last_status,last_reason
                ) VALUES(1,'17049',NULL,NULL);
                CREATE TABLE IF NOT EXISTS watchdog_transition_outbox (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  machine_id TEXT NOT NULL CHECK(machine_id = '17049'),
                  status TEXT NOT NULL CHECK(status IN ('invalid','missing','stale_work','healthy')),
                  reason TEXT NOT NULL,
                  transition_name TEXT NOT NULL,
                  priority TEXT NOT NULL CHECK(priority IN ('critical','recovery')),
                  observed_at TEXT NOT NULL,
                  message TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 6),
                  next_attempt_at TEXT,
                  acknowledged_at TEXT,
                  telegram_message_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS watchdog_transition_due
                  ON watchdog_transition_outbox(acknowledged_at,next_attempt_at,event_id);
                UPDATE watchdog_transition_outbox
                   SET next_attempt_at=observed_at
                 WHERE acknowledged_at IS NULL AND next_attempt_at IS NULL;
                """
            )
            version = self.connection.execute(
                "SELECT schema_version FROM watchdog_runtime_schema"
            ).fetchone()
            if version is None or int(version[0]) != WATCHDOG_OUTBOX_SCHEMA_VERSION:
                raise WatchdogRuntimeError("outbox_schema_invalid")
            self.connection.commit()
            os.chmod(self.path, 0o600)
        except WatchdogRuntimeError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise WatchdogRuntimeError("outbox_unavailable") from error

    def close(self) -> None:
        self.connection.close()

    def record_evaluation(self, result: WatchdogResult, *, now: datetime) -> int | None:
        observed_at = _utc_text(now)
        if (
            not isinstance(result, WatchdogResult)
            or result.machine_id != MACHINE_ID
            or result.status not in _RESULT_REASONS
            or result.reason not in _RESULT_REASONS[result.status]
            or result.transition not in _RESULT_TRANSITIONS
        ):
            raise WatchdogRuntimeError("evaluation_result_invalid")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            state = self.connection.execute(
                "SELECT last_status,last_reason FROM watchdog_runtime_state WHERE singleton=1"
            ).fetchone()
            if state is None:
                raise WatchdogRuntimeError("outbox_state_invalid")
            previous_status = state["last_status"]
            should_alert = result.status in FAULT_STATUSES and previous_status != result.status
            should_recover = (
                result.status == "healthy"
                and previous_status in FAULT_STATUSES.union({"recovering"})
            )
            event_id = None
            if should_alert or should_recover:
                count = int(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM watchdog_transition_outbox"
                    ).fetchone()[0]
                )
                if count >= MAX_OUTBOX_ROWS:
                    excess = count - MAX_OUTBOX_ROWS + 1
                    acknowledged = self.connection.execute(
                        """SELECT event_id FROM watchdog_transition_outbox
                           WHERE acknowledged_at IS NOT NULL ORDER BY event_id LIMIT ?""",
                        (excess,),
                    ).fetchall()
                    if len(acknowledged) != excess:
                        raise WatchdogRuntimeError("outbox_full")
                    self.connection.executemany(
                        "DELETE FROM watchdog_transition_outbox WHERE event_id=?",
                        ((row[0],) for row in acknowledged),
                    )
                priority = "critical" if should_alert else "recovery"
                transition = result.transition
                if transition == "unchanged":
                    transition = (
                        f"{result.status}_started"
                        if should_alert
                        else "recovered"
                    )
                cursor = self.connection.execute(
                    """INSERT INTO watchdog_transition_outbox(
                         machine_id,status,reason,transition_name,priority,observed_at,
                         message,next_attempt_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        MACHINE_ID,
                        result.status,
                        result.reason,
                        transition,
                        priority,
                        observed_at,
                        _alert_text(result.status, result.reason),
                        observed_at,
                    ),
                )
                event_id = int(cursor.lastrowid)
            self.connection.execute(
                """UPDATE watchdog_runtime_state SET last_status=?,last_reason=?
                   WHERE singleton=1 AND machine_id=?""",
                (result.status, result.reason, MACHINE_ID),
            )
            self.connection.commit()
            return event_id
        except WatchdogRuntimeError:
            self.connection.rollback()
            raise
        except sqlite3.Error as error:
            self.connection.rollback()
            raise WatchdogRuntimeError("outbox_persist_failed") from error

    def due(self, *, now: datetime, limit: int = MAX_SEND_BATCH) -> tuple[WatchdogAlert, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SEND_BATCH:
            raise WatchdogRuntimeError("send_batch_invalid")
        rows = self.connection.execute(
            """SELECT event_id,machine_id,status,reason,transition_name,priority,
                      observed_at,message
               FROM watchdog_transition_outbox
               WHERE acknowledged_at IS NULL AND next_attempt_at IS NOT NULL
                 AND next_attempt_at <= ?
               ORDER BY CASE priority WHEN 'critical' THEN 0 ELSE 1 END,event_id
               LIMIT ?""",
            (_utc_text(now), limit),
        ).fetchall()
        return tuple(
            WatchdogAlert(
                event_id=int(row["event_id"]),
                machine_id=str(row["machine_id"]),
                status=str(row["status"]),
                reason=str(row["reason"]),
                transition=str(row["transition_name"]),
                priority=str(row["priority"]),
                observed_at=str(row["observed_at"]),
                text=str(row["message"]),
                silent=False,
            )
            for row in rows
        )

    def acknowledge(self, event_id: int, message_id: int, *, now: datetime) -> None:
        if (
            not isinstance(event_id, int)
            or isinstance(event_id, bool)
            or event_id <= 0
            or not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise WatchdogRuntimeError("delivery_receipt_invalid")
        cursor = self.connection.execute(
            """UPDATE watchdog_transition_outbox
               SET acknowledged_at=?,telegram_message_id=?,next_attempt_at=NULL
               WHERE event_id=? AND acknowledged_at IS NULL""",
            (_utc_text(now), message_id, event_id),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise WatchdogRuntimeError("outbox_acknowledgement_invalid")

    def fail(self, event_id: int, *, now: datetime) -> bool:
        row = self.connection.execute(
            """SELECT attempts FROM watchdog_transition_outbox
               WHERE event_id=? AND acknowledged_at IS NULL""",
            (event_id,),
        ).fetchone()
        if row is None:
            raise WatchdogRuntimeError("outbox_failure_invalid")
        attempts = min(MAX_DELIVERY_ATTEMPTS, int(row[0]) + 1)
        delay_seconds = min(300, 30 * (2 ** (attempts - 1)))
        next_attempt = _utc_text(now + timedelta(seconds=delay_seconds))
        self.connection.execute(
            """UPDATE watchdog_transition_outbox SET attempts=?,next_attempt_at=?
               WHERE event_id=? AND acknowledged_at IS NULL""",
            (attempts, next_attempt, event_id),
        )
        self.connection.commit()
        return False

    def event_state(self, event_id: int) -> Mapping[str, object]:
        """Return fixed outbox metadata for local tests and operational inspection."""
        row = self.connection.execute(
            """SELECT machine_id,status,reason,transition_name,priority,attempts,
                      next_attempt_at,acknowledged_at,telegram_message_id,message
               FROM watchdog_transition_outbox WHERE event_id=?""",
            (event_id,),
        ).fetchone()
        if row is None:
            raise WatchdogRuntimeError("outbox_event_missing")
        return dict(row)


class WatchdogNotifier:
    """Drain the watchdog outbox through one distinct send-only identity."""

    def __init__(
        self,
        outbox: WatchdogOutbox,
        sender: WatchdogTelegramSender,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        identity = getattr(sender, "identity", None)
        if not isinstance(identity, WatchdogSenderIdentity):
            raise WatchdogRuntimeError("sender_identity_missing")
        # Reconstructing re-runs all capability checks even for unusual subclasses.
        WatchdogSenderIdentity(
            identity.name,
            role=identity.role,
            consumes_updates=identity.consumes_updates,
            can_approve=identity.can_approve,
        )
        forbidden_capabilities = (
            "get_updates",
            "poll_updates",
            "poll_once",
            "approve",
            "create_approval",
            "consume_approval",
        )
        if any(callable(getattr(sender, name, None)) for name in forbidden_capabilities):
            raise WatchdogRuntimeError("sender_capability_invalid")
        self.outbox = outbox
        self.sender = sender
        self.monotonic = monotonic

    def send_due(self, *, now: datetime, deadline: float) -> DrainResult:
        _check_deadline(deadline, self.monotonic, starting=True)
        sent = failed = exhausted = 0
        for alert in self.outbox.due(now=now):
            try:
                _check_deadline(deadline, self.monotonic)
                receipt = self.sender.send_alert(
                    alert,
                    deadline=deadline,
                    max_response_bytes=MAX_TELEGRAM_RESPONSE_BYTES,
                )
                if (
                    getattr(receipt, "delivery_semantics", None) != "telegram-accepted"
                    or not isinstance(getattr(receipt, "message_id", None), int)
                    or isinstance(receipt.message_id, bool)
                    or receipt.message_id <= 0
                    or not isinstance(getattr(receipt, "output_bytes", None), int)
                    or isinstance(receipt.output_bytes, bool)
                    or not 0 <= receipt.output_bytes <= MAX_TELEGRAM_RESPONSE_BYTES
                ):
                    raise WatchdogRuntimeError("delivery_receipt_invalid")
                _check_deadline(deadline, self.monotonic)
                self.outbox.acknowledge(alert.event_id, receipt.message_id, now=now)
                sent += 1
            except Exception:
                failed += 1
                if self.outbox.fail(alert.event_id, now=now):
                    exhausted += 1
        _check_deadline(deadline, self.monotonic)
        return DrainResult(sent, failed, exhausted)


class WatchdogRuntime:
    """One deterministic receive/evaluate/enqueue/send watchdog tick."""

    def __init__(
        self,
        receiver: HeartbeatReceiver,
        state_path: Path,
        outbox: WatchdogOutbox,
        notifier: WatchdogNotifier,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.receiver = receiver
        self.state_path = _absolute_path(Path(state_path), "state_path_not_absolute")
        self.outbox = outbox
        if notifier.outbox is not outbox:
            raise WatchdogRuntimeError("notifier_outbox_mismatch")
        self.notifier = notifier
        self.clock = clock
        self.monotonic = monotonic

    def tick(self, *, deadline: float) -> WatchdogTick:
        _check_deadline(deadline, self.monotonic, starting=True)
        now = self.clock()
        _utc_text(now)
        heartbeat = self.receiver.receive(deadline=deadline)
        evaluation = evaluate_watchdog(self.state_path, heartbeat, now=now)
        queued = self.outbox.record_evaluation(evaluation, now=now)
        delivery = self.notifier.send_due(now=now, deadline=deadline)
        _check_deadline(deadline, self.monotonic)
        return WatchdogTick(evaluation, queued, delivery)


class HealthchecksWatchdogRuntime:
    """One deterministic evaluation followed by one status-mapped ping.

    Startup grace is deliberately silent.  Every other window reports its current
    state, including every recovering window, so Healthchecks.io observes the complete
    watchdog cadence rather than only state transitions.
    """

    def __init__(
        self,
        receiver: HeartbeatReceiver,
        state_path: Path,
        pinger: HealthchecksPinger,
        *,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(pinger, HealthchecksPinger):
            raise WatchdogRuntimeError("healthchecks_pinger_invalid")
        self.receiver = receiver
        self.state_path = _absolute_path(Path(state_path), "state_path_not_absolute")
        self.pinger = pinger
        self.clock = clock
        self.monotonic = monotonic

    def tick(self, *, deadline: float) -> HealthchecksTick:
        _check_deadline(deadline, self.monotonic, starting=True)
        now = self.clock()
        _utc_text(now)
        heartbeat = self.receiver.receive(deadline=deadline)
        evaluation = evaluate_watchdog(self.state_path, heartbeat, now=now)
        if evaluation.status == "starting":
            ping = None
        elif evaluation.status == "healthy":
            ping = self.pinger.ping(success=True, deadline=deadline)
        elif evaluation.status in HEALTHCHECKS_FAILURE_STATUSES:
            ping = self.pinger.ping(success=False, deadline=deadline)
        else:
            raise WatchdogRuntimeError("evaluation_result_invalid")
        _check_deadline(deadline, self.monotonic)
        return HealthchecksTick(evaluation, ping)
