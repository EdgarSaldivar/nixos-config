"""Verification and durable handoff for Vast notification webhooks.

This module implements the official Vast.ai wire contract: HMAC-SHA256 of
``<X-Vast-Timestamp>.<raw request body bytes>`` and a
``X-Vast-Signature-256`` value prefixed with ``sha256=``.  The integer header
timestamp, not the floating-point payload timestamp, is the replay input.

Integration contract: the included loopback-only HTTP adapter requires the exact
``POST /vast/v1/events`` route, passes the unchanged body and headers to
:func:`verify_and_enqueue`, and never follows a redirect.  ``enqueue`` owns
durability and event-ID uniqueness and must return only after its transaction
commits.  Both ACCEPTED and DUPLICATE are successful handoffs; any exception or
other return value is failure and must not produce HTTP 2xx.  The enqueued event
is evidence plus a fixed reconciliation request, never an action request.
Unknown event type strings are accepted as bounded evidence rather than guessed
or assigned semantics.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import ipaddress
import json
import math
import re
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Protocol


TARGET_MACHINE_ID = 17049
MAX_BODY_BYTES = 256 * 1024
MAX_REPLAY_AGE_SECONDS = 300
MAX_EVENT_ID_CHARS = 128
MAX_TYPE_CHARS = 128
MAX_SUBJECT_CHARS = 1024
MAX_MESSAGE_CHARS = 16 * 1024
_SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}\Z")
_EVENT_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
WEBHOOK_ROUTE = "/vast/v1/events"
MAX_CONNECTIONS = 16
REQUEST_DEADLINE_SECONDS = 10.0


class WebhookError(ValueError):
    """A fixed, secret-free rejection reason for the eventual HTTP adapter."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class EnqueueResult(Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


class DurableEnqueue(Protocol):
    """Persist an event/reconciliation atomically and deduplicate event_id."""

    def __call__(self, event: "WebhookEvent") -> EnqueueResult: ...


@dataclass(frozen=True, repr=False)
class WebhookEvent:
    event_id: str
    delivery_attempt: int
    signed_timestamp: int
    user_id: int
    notif_type: str
    subject: str
    message: str
    event_timestamp: float
    machine_id: int | None
    target_match: bool | None
    reconcile_machine_id: int
    raw_body: bytes

    def __repr__(self) -> str:
        # Message/body are external evidence and may contain tenant data.
        return (
            f"WebhookEvent(event_id={self.event_id!r}, "
            f"target_match={self.target_match!r}, "
            f"reconcile_machine_id={self.reconcile_machine_id})"
        )


@dataclass(frozen=True)
class WebhookReceipt:
    event_id: str
    result: EnqueueResult
    reconcile_machine_id: int


def verify_and_enqueue(
    headers: Mapping[str, str],
    raw_body: bytes,
    webhook_secret: str | bytes,
    enqueue: DurableEnqueue,
    *,
    clock: Callable[[], float] = time.time,
) -> WebhookReceipt:
    """Verify exact signed bytes and return success only after durable handoff."""
    if not isinstance(raw_body, bytes):
        raise WebhookError("invalid_body_type")
    if not raw_body:
        raise WebhookError("empty_body")
    if len(raw_body) > MAX_BODY_BYTES:
        raise WebhookError("body_too_large")
    normalized = _headers(headers)
    content_type = normalized.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise WebhookError("invalid_content_type")
    timestamp_text = normalized.get("x-vast-timestamp", "")
    if not timestamp_text.isascii() or not timestamp_text.isdigit() or len(timestamp_text) > 12:
        raise WebhookError("invalid_timestamp")
    signed_timestamp = int(timestamp_text)
    try:
        now = float(clock())
    except (TypeError, ValueError, OverflowError):
        raise WebhookError("invalid_clock") from None
    if not math.isfinite(now):
        raise WebhookError("invalid_clock")
    if abs(now - signed_timestamp) > MAX_REPLAY_AGE_SECONDS:
        raise WebhookError("stale_timestamp")
    signature = normalized.get("x-vast-signature-256", "")
    if not _SIGNATURE.fullmatch(signature):
        raise WebhookError("invalid_signature_format")
    secret = _secret_bytes(webhook_secret)
    signed = timestamp_text.encode("ascii") + b"." + raw_body
    expected = "sha256=" + hmac.new(secret, signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise WebhookError("signature_mismatch")

    payload = _payload(raw_body)
    event_id = _required_text(payload, "event_id", MAX_EVENT_ID_CHARS)
    if not _EVENT_ID.fullmatch(event_id):
        raise WebhookError("invalid_event_id")
    header_event_id = normalized.get("x-vast-event-id", "")
    if not hmac.compare_digest(header_event_id, event_id):
        raise WebhookError("event_id_mismatch")
    attempt_text = normalized.get("x-vast-delivery-attempt", "")
    if not attempt_text.isascii() or not attempt_text.isdigit() or len(attempt_text) > 9:
        raise WebhookError("invalid_delivery_attempt")
    delivery_attempt = int(attempt_text)
    if delivery_attempt < 1:
        raise WebhookError("invalid_delivery_attempt")
    user_id = payload.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
        raise WebhookError("invalid_user_id")
    notif_type = _required_text(payload, "notif_type", MAX_TYPE_CHARS)
    subject = _required_text(payload, "subject", MAX_SUBJECT_CHARS)
    message = _required_text(payload, "message", MAX_MESSAGE_CHARS)
    event_timestamp_value = payload.get("timestamp")
    if (
        isinstance(event_timestamp_value, bool)
        or not isinstance(event_timestamp_value, (int, float))
        or not math.isfinite(float(event_timestamp_value))
        or event_timestamp_value <= 0
    ):
        raise WebhookError("invalid_event_timestamp")
    machine_id, target_match = _machine_identity(payload.get("machine_id"))
    event = WebhookEvent(
        event_id=event_id,
        delivery_attempt=delivery_attempt,
        signed_timestamp=signed_timestamp,
        user_id=user_id,
        notif_type=notif_type,
        subject=subject,
        message=message,
        event_timestamp=float(event_timestamp_value),
        machine_id=machine_id,
        target_match=target_match,
        # Webhook content can accelerate observation but never select a target
        # or authorize an action.  Missing, unknown, and wrong IDs all request
        # fixed-target reconciliation of 17049.
        reconcile_machine_id=TARGET_MACHINE_ID,
        raw_body=raw_body,
    )
    try:
        result = enqueue(event)
    except Exception:
        raise WebhookError("durable_enqueue_failed") from None
    if result not in {EnqueueResult.ACCEPTED, EnqueueResult.DUPLICATE}:
        raise WebhookError("durable_enqueue_failed")
    return WebhookReceipt(event_id, result, TARGET_MACHINE_ID)


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(headers, Mapping) or len(headers) > 64:
        raise WebhookError("invalid_headers")
    result: dict[str, str] = {}
    for raw_name, value in headers.items():
        if (
            not isinstance(raw_name, str)
            or not isinstance(value, str)
            or len(raw_name) > 128
            or len(value) > 8192
            or "\r" in value
            or "\n" in value
        ):
            raise WebhookError("invalid_headers")
        name = raw_name.lower()
        if name in result:
            raise WebhookError("ambiguous_headers")
        result[name] = value
    return result


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        try:
            result = secret.encode("utf-8")
        except UnicodeEncodeError:
            raise WebhookError("invalid_secret") from None
    elif isinstance(secret, bytes):
        result = secret
    else:
        raise WebhookError("invalid_secret")
    if not result or len(result) > 4096:
        raise WebhookError("invalid_secret")
    return result


def _payload(raw_body: bytes) -> dict[str, object]:
    try:
        result = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise WebhookError("malformed_json") from None
    if not isinstance(result, dict) or len(result) > 64:
        raise WebhookError("malformed_payload")
    return result


def _required_text(payload: dict[str, object], key: str, limit: int) -> str:
    value = payload.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or any(ord(character) < 32 and character not in "\t\n" for character in value)
    ):
        raise WebhookError(f"invalid_{key}")
    return value


def _machine_identity(value: object) -> tuple[int | None, bool | None]:
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int) and value >= 0:
        return value, value == TARGET_MACHINE_ID
    if isinstance(value, str) and value.isascii() and value.isdigit():
        machine_id = int(value)
        return machine_id, machine_id == TARGET_MACHINE_ID
    return None, None


class SQLiteWebhookQueue:
    """A dedicated durable ingress queue, separate from incident-state schema.

    Event insertion and its fixed-target reconciliation signal are one row and one
    transaction.  A duplicate remains successful while an unconsumed signal survives a
    process crash.  Consumers acknowledge only after scheduling reconciliation.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS vast_webhook_events (
                     event_id TEXT PRIMARY KEY,
                     signed_timestamp INTEGER NOT NULL,
                     delivery_attempt INTEGER NOT NULL,
                     machine_id INTEGER,
                     target_match INTEGER,
                     reconcile_machine_id INTEGER NOT NULL CHECK(reconcile_machine_id=17049),
                     raw_body BLOB NOT NULL,
                     accepted_utc INTEGER NOT NULL,
                     reconciled_utc INTEGER
                   )"""
            )
            self._db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            self.close()
            raise

    def __call__(self, event: WebhookEvent) -> EnqueueResult:
        if event.reconcile_machine_id != TARGET_MACHINE_ID:
            raise ValueError("wrong reconciliation target")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute(
                    """INSERT INTO vast_webhook_events(
                         event_id,signed_timestamp,delivery_attempt,machine_id,target_match,
                         reconcile_machine_id,raw_body,accepted_utc)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        event.event_id,
                        event.signed_timestamp,
                        event.delivery_attempt,
                        event.machine_id,
                        None if event.target_match is None else int(event.target_match),
                        TARGET_MACHINE_ID,
                        event.raw_body,
                        int(time.time()),
                    ),
                )
                self._db.commit()
                return EnqueueResult.ACCEPTED
            except sqlite3.IntegrityError:
                self._db.rollback()
                row = self._db.execute(
                    "SELECT reconcile_machine_id FROM vast_webhook_events WHERE event_id=?",
                    (event.event_id,),
                ).fetchone()
                if row is None or int(row["reconcile_machine_id"]) != TARGET_MACHINE_ID:
                    raise
                return EnqueueResult.DUPLICATE
            except Exception:
                self._db.rollback()
                raise

    def pending_reconcile_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._db.execute(
                """SELECT event_id FROM vast_webhook_events
                   WHERE reconcile_machine_id=? AND reconciled_utc IS NULL
                   ORDER BY accepted_utc,event_id LIMIT ?""",
                (TARGET_MACHINE_ID, limit),
            ).fetchall()
        return tuple(str(row["event_id"]) for row in rows)

    def mark_reconciled(self, event_id: str, machine_id: int = TARGET_MACHINE_ID) -> None:
        if machine_id != TARGET_MACHINE_ID or not _EVENT_ID.fullmatch(event_id):
            raise ValueError("invalid reconciliation acknowledgement")
        with self._lock:
            self._db.execute(
                """UPDATE vast_webhook_events SET reconciled_utc=?
                   WHERE event_id=? AND reconcile_machine_id=? AND reconciled_utc IS NULL""",
                (int(time.time()), event_id, TARGET_MACHINE_ID),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


class _BoundedThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[http.server.BaseHTTPRequestHandler],
        *,
        max_connections: int,
    ):
        self._slots = threading.BoundedSemaphore(max_connections)
        self._deadline_lock = threading.Lock()
        self._connection_deadlines: dict[int, float] = {}
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        super().__init__(address, handler)

    def get_request(self) -> tuple[socket.socket, object]:
        request, client_address = super().get_request()
        deadline = time.monotonic() + self.request_deadline  # type: ignore[attr-defined]
        request.settimeout(self.request_deadline)  # type: ignore[attr-defined]
        with self._deadline_lock:
            self._connection_deadlines[id(request)] = deadline
        return request, client_address

    def connection_deadline(self, request: object) -> float:
        with self._deadline_lock:
            return self._connection_deadlines[id(request)]

    def _forget_request(self, request: object) -> None:
        with self._deadline_lock:
            self._connection_deadlines.pop(id(request), None)

    def process_request(self, request: object, client_address: object) -> None:
        if not self._slots.acquire(blocking=False):
            self._forget_request(request)
            self.shutdown_request(request)  # type: ignore[arg-type]
            return
        try:
            super().process_request(request, client_address)  # type: ignore[arg-type]
        except BaseException:
            self._forget_request(request)
            self._slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)  # type: ignore[arg-type]
        finally:
            self._forget_request(request)
            self._slots.release()


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "terracompute-webhook"
    sys_version = ""

    def setup(self) -> None:
        # BaseHTTPRequestHandler parses the request line and headers before do_POST.
        # Arm an absolute accept-to-body deadline first so byte trickles cannot renew
        # an inactivity timeout forever.
        deadline = self.server.connection_deadline(self.request)  # type: ignore[attr-defined]
        remaining = max(0.001, deadline - time.monotonic())
        self.request.settimeout(remaining)
        self._deadline_timer = threading.Timer(remaining, self._expire_connection)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()
        super().setup()

    def finish(self) -> None:
        timer = getattr(self, "_deadline_timer", None)
        if timer is not None:
            timer.cancel()
        super().finish()

    def _expire_connection(self) -> None:
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self.close_connection = True
        if self.path != WEBHOOK_ROUTE:
            self._reply(404)
            return
        raw_headers = list(self.headers.raw_items())
        names = [name.lower() for name, _value in raw_headers]
        if len(names) != len(set(names)):
            self._reply(400)
            return
        if "transfer-encoding" in names or "content-length" not in names:
            self._reply(400)
            return
        length_text = self.headers.get("Content-Length", "")
        if not length_text.isascii() or not length_text.isdigit() or len(length_text) > 9:
            self._reply(400)
            return
        length = int(length_text)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(413 if length > MAX_BODY_BYTES else 400)
            return
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._reply(408)
            return
        if len(body) != length:
            self._reply(400)
            return
        headers = {name: value for name, value in raw_headers}
        try:
            receipt = verify_and_enqueue(
                headers,
                body,
                self.server.webhook_secret,  # type: ignore[attr-defined]
                self.server.webhook_enqueue,  # type: ignore[attr-defined]
            )
        except WebhookError:
            self._reply(401)
            return
        self._reply(202 if receipt.result is EnqueueResult.ACCEPTED else 200)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.close_connection = True
        self._reply(405)

    def _reply(self, status: int) -> None:
        body = b"{}\n"
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        # Request text is external evidence and can contain tenant data.
        return


def webhook_server(
    host: str,
    port: int,
    secret: str | bytes,
    enqueue: DurableEnqueue,
    *,
    max_connections: int = MAX_CONNECTIONS,
    request_deadline: float = REQUEST_DEADLINE_SECONDS,
) -> http.server.HTTPServer:
    """Construct an exact-route loopback receiver without starting it."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("webhook listener must be a loopback IP address") from None
    if not address.is_loopback:
        raise ValueError("webhook listener must be loopback")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("webhook port is invalid")
    if not 1 <= max_connections <= MAX_CONNECTIONS:
        raise ValueError("webhook connection limit is invalid")
    if not 1 <= request_deadline <= 30:
        raise ValueError("webhook request deadline is invalid")
    server = _BoundedThreadingHTTPServer(
        (host, port), _WebhookHandler, max_connections=max_connections
    )
    server.webhook_secret = secret  # type: ignore[attr-defined]
    server.webhook_enqueue = enqueue  # type: ignore[attr-defined]
    server.request_deadline = float(request_deadline)  # type: ignore[attr-defined]
    return server
