"""Bounded Telegram transport and authenticated operator-input normalization.

Outbound delivery is at-least-once: a connection failure after Telegram accepted a
``sendMessage`` request is ambiguous, so callers must not claim exactly-once delivery.
The durable outbox remains owned by :mod:`terracompute_ops.state`; the compatible
``drain_outbox(store, token, chat_id, sender)`` entry point intentionally persists only a
fixed secret-free failure category.

Inbound polling is disabled by default and uses an injected :class:`UpdateBackend`.
Deploy exactly one enabled consumer for a bot. Its backend should use its own schema or
namespaced connection and must not change SQLite's global ``user_version``. Accepted
authenticated envelopes are stored before their update cursor advances. These envelopes
are requests for a later deterministic policy broker; nothing in this module executes or
authorizes an action.
"""

from __future__ import annotations

import http.client
import inspect
import json
import re
import socket
import time
import sqlite3
import ssl
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .state import MACHINE_ID, StateStore


TELEGRAM_HOST = "api.telegram.org"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_MESSAGE_BYTES = 16 * 1024
MAX_RETRY_AFTER_SECONDS = 3600
MAX_LONG_POLL_SECONDS = 50
MAX_IDENTIFIER = (1 << 63) - 1
EXPECTED_BOT_USERNAME = "TerraComputeBot"


class TelegramError(OSError):
    """Base class whose messages never contain request URLs or credentials."""


class TelegramTransportError(TelegramError):
    def __init__(self, category: str = "telegram-transport-failed"):
        self.category = category
        super().__init__(category)


class TelegramDeliveryUncertain(TelegramTransportError):
    """A send may have been accepted even though no complete response was received."""

    def __init__(self) -> None:
        super().__init__("telegram-delivery-uncertain")


class TelegramAPIError(TelegramError):
    def __init__(self, error_code: int | None = None):
        self.error_code = error_code
        super().__init__("telegram-api-rejected-request")


class TelegramRateLimited(TelegramAPIError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, min(MAX_RETRY_AFTER_SECONDS, retry_after))
        super().__init__(429)
        self.args = ("telegram-rate-limited",)


class TelegramResponseError(TelegramError):
    def __init__(self, category: str = "telegram-invalid-response"):
        self.category = category
        super().__init__(category)


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class TelegramTransport(Protocol):
    def request(
        self,
        path: str,
        body: bytes,
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HTTPResponse: ...


def _connect_over_ipv4(
    address: tuple[str, int],
    timeout: float | None = None,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Reach Telegram over IPv4 only, whatever DNS offers first.

    Measured on imladris on 2026-09-18: the host carries a global-scope IPv6 address
    from Tailscale and has NO IPv6 default route, while ``api.telegram.org`` resolves
    to an AAAA record first. ``socket.create_connection`` walks the addresses in the
    order it is given, so every request tried a v6 address that goes nowhere and spent
    the whole request timeout there before falling back. IPv4 connects in 0.135s.

    The symptom was not a dead bot -- outbound sends mostly worked -- but a getUpdates
    long poll that failed and recovered every few minutes. That poll is how a button
    press comes back, so approvals arrived late or never, and the ones that did land
    were refused as "outside the proposal lifetime". A person pressing a button and
    nothing happening was a routing table, three layers down.

    Pinning the family here changes this client and nothing else on the host. It is
    deliberately not a system-wide gai.conf edit or a route added for one bot.
    """
    host, port = address
    failure: OSError | None = None
    for family, kind, proto, _canonical, sockaddr in socket.getaddrinfo(
        host, port, socket.AF_INET, socket.SOCK_STREAM
    ):
        candidate = socket.socket(family, kind, proto)
        try:
            if timeout is not None:
                candidate.settimeout(timeout)
            if source_address:
                candidate.bind(source_address)
            candidate.connect(sockaddr)
            return candidate
        except OSError as error:
            candidate.close()
            failure = error
    raise failure or OSError(f"no IPv4 address for {host}")


class StdlibTelegramTransport:
    """Direct stdlib HTTPS transport pinned to Telegram's Bot API origin.

    ``http.client`` does not consult ambient HTTP(S) proxy variables and does not follow
    redirects. The host is a constant rather than caller input. Response bodies and
    declared content lengths are bounded before JSON parsing.

    Connections are pinned to IPv4; see :func:`_connect_over_ipv4` for why.
    """

    def __init__(self, ssl_context: ssl.SSLContext | None = None):
        self._ssl_context = ssl_context or ssl.create_default_context()

    def __repr__(self) -> str:
        return "StdlibTelegramTransport(host='api.telegram.org')"

    def request(
        self,
        path: str,
        body: bytes,
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HTTPResponse:
        if not path.startswith("/bot") or "://" in path or "\r" in path or "\n" in path:
            raise TelegramTransportError("telegram-invalid-request-path")
        connection = http.client.HTTPSConnection(
            TELEGRAM_HOST, 443, timeout=timeout, context=self._ssl_context
        )
        # Replacing the hook rather than overriding connect(): everything else that
        # connect() does -- the TLS wrap, and server_hostname for certificate
        # validation -- is left exactly as the stdlib wrote it.
        connection._create_connection = _connect_over_ipv4  # type: ignore[method-assign]
        deadline = time.monotonic() + timeout
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

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.connect()
            connected_socket = connection.sock
            if expired.is_set() or time.monotonic() >= deadline:
                raise TelegramTransportError("telegram-request-timeout")
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    if int(length) > max_response_bytes:
                        raise TelegramResponseError("telegram-response-too-large")
                except ValueError as error:
                    raise TelegramResponseError("telegram-invalid-content-length") from error
            data = response.read(max_response_bytes + 1)
            if expired.is_set() or time.monotonic() >= deadline:
                raise TelegramTransportError("telegram-request-timeout")
            if len(data) > max_response_bytes:
                raise TelegramResponseError("telegram-response-too-large")
            return HTTPResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.getheaders()},
                body=data,
            )
        finally:
            timer.cancel()
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
            connection.close()


class NotificationKind(str, Enum):
    INCIDENT = "incident"
    REMINDER = "reminder"
    ACKNOWLEDGEMENT = "acknowledgement"
    APPROVAL_REQUEST = "approval_request"
    GENERAL = "general"


@dataclass(frozen=True)
class NotificationMetadata:
    """Local correlation metadata; it is not sent as arbitrary Bot API fields."""

    kind: NotificationKind = NotificationKind.GENERAL
    incident_id: str | None = None
    severity: str | None = None
    reminder_number: int | None = None
    acknowledgement_id: str | None = None


@dataclass(frozen=True)
class SendReceipt:
    message_id: int
    metadata: NotificationMetadata
    # Telegram acceptance is not proof of device display, sound, or human receipt.
    delivery_semantics: str = "telegram-accepted"


class TelegramClient:
    """Small semantic Bot API client with an injected transport for offline tests."""

    def __init__(
        self,
        token: str,
        *,
        transport: TelegramTransport | None = None,
        timeout: float = 15,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,256}", token
        ):
            raise ValueError("Telegram credential is malformed")
        if timeout <= 0 or timeout > 120:
            raise ValueError("Telegram timeout is outside its bounded range")
        if not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("Telegram response bound is outside its allowed range")
        self._token = token
        self._transport = transport or StdlibTelegramTransport()
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    def __repr__(self) -> str:
        return (
            f"TelegramClient(token=<redacted>, timeout={self._timeout!r}, "
            f"max_response_bytes={self._max_response_bytes!r})"
        )

    def clear_buttons(self, chat_id: int | str, message_id: int) -> None:
        """Take the buttons off a request that has been answered or has lapsed.

        A spent button is worse than no button. It is single-use and bound to one
        proposal, so a second press can only fail -- and after a session where every
        press failed for a different reason, an affordance that does nothing is the
        last thing anybody needs. The message stays, so the record of what was asked
        and answered is untouched; only the invitation goes.

        Best effort by design. The answer has already been recorded by the time this
        runs, so failing to tidy up must never undo it -- and Telegram rejects an edit
        that changes nothing, which is exactly what a second call does.
        """
        try:
            self._call("editMessageReplyMarkup", {
                "chat_id": normalize_id(chat_id, "chat_id"),
                "message_id": normalize_id(message_id, "message_id", positive=True),
                "reply_markup": {"inline_keyboard": []},
            }, mutation=True)
        except Exception:
            pass

    def send_message(
        self,
        chat_id: int | str,
        message: str,
        *,
        silent: bool = False,
        metadata: NotificationMetadata | None = None,
        approve_callback: tuple[str, str] | None = None,
        deny_callback: tuple[str, str] | None = None,
    ) -> SendReceipt:
        normalized_chat = normalize_id(chat_id, "chat_id")
        if (
            not isinstance(message, str)
            or not message
            or len(message.encode("utf-8")) > MAX_MESSAGE_BYTES
        ):
            raise ValueError("Telegram message is empty or unexpectedly large")
        payload = {
            "chat_id": normalized_chat,
            "text": message,
            "disable_web_page_preview": True,
            "disable_notification": bool(silent),
        }
        buttons = []
        for callback, grammar, what in (
            (approve_callback, _CALLBACK_APPROVE, "approval"),
            (deny_callback, _CALLBACK_DENY, "denial"),
        ):
            if callback is None:
                continue
            label, data = callback
            # Only the parser's own grammar, within Telegram's 64-byte callback limit.
            if (
                not isinstance(label, str)
                or not 1 <= len(label) <= 64
                or not isinstance(data, str)
                or len(data.encode("utf-8")) > 64
                or not grammar.fullmatch(data)
            ):
                raise ValueError(f"{what} button is invalid")
            buttons.append({"text": label, "callback_data": data})
        if deny_callback is not None and approve_callback is None:
            raise ValueError("a denial button needs its approval button")
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        result = self._call("sendMessage", payload, mutation=True)
        if not isinstance(result, dict):
            raise TelegramResponseError()
        try:
            message_id = normalize_id(
                result.get("message_id"), "message_id", positive=True
            )
        except (TypeError, ValueError):
            raise TelegramResponseError("telegram-message-id-missing") from None
        return SendReceipt(message_id, metadata or NotificationMetadata())

    def get_updates(
        self, *, offset: int, poll_timeout: int = 25, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if not 0 <= poll_timeout <= MAX_LONG_POLL_SECONDS:
            raise ValueError("Telegram long-poll timeout is outside its bounded range")
        if not 1 <= limit <= 100:
            raise ValueError("Telegram update limit is outside its bounded range")
        normalized_offset = normalize_id(
            offset, "offset", positive=True, allow_zero=True
        )
        result = self._call(
            "getUpdates",
            {
                "offset": normalized_offset,
                "timeout": poll_timeout,
                "limit": limit,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=max(self._timeout, poll_timeout + 5),
        )
        if not isinstance(result, list) or len(result) > limit:
            raise TelegramResponseError()
        if not all(isinstance(item, dict) for item in result):
            raise TelegramResponseError()
        return tuple(result)

    def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        result = self._call(
            "getChatMember",
            {
                "chat_id": normalize_id(chat_id, "chat_id"),
                "user_id": normalize_id(user_id, "user_id", positive=True),
            },
        )
        if not isinstance(result, dict):
            raise TelegramResponseError()
        return result

    def _call(
        self,
        method: str,
        payload: Mapping[str, Any],
        *,
        timeout: float | None = None,
        mutation: bool = False,
    ) -> Any:
        body = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        try:
            response = self._transport.request(
                f"/bot{self._token}/{method}",
                body,
                timeout=timeout or self._timeout,
                max_response_bytes=self._max_response_bytes,
            )
        except TelegramError:
            raise
        except Exception:
            if mutation:
                # The original exception may render the token-bearing request path.
                raise TelegramDeliveryUncertain() from None
            raise TelegramTransportError() from None
        if 300 <= response.status < 400:
            raise TelegramResponseError("telegram-redirect-rejected")
        if len(response.body) > self._max_response_bytes:
            raise TelegramResponseError("telegram-response-too-large")
        try:
            document = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TelegramResponseError() from error
        if not isinstance(document, dict):
            raise TelegramResponseError()
        if response.status == 429 or document.get("error_code") == 429:
            parameters = document.get("parameters")
            retry = parameters.get("retry_after") if isinstance(parameters, dict) else 1
            if isinstance(retry, bool) or not isinstance(retry, int):
                retry = 1
            raise TelegramRateLimited(retry)
        if response.status != 200:
            code = document.get("error_code")
            raise TelegramAPIError(
                code if isinstance(code, int) and not isinstance(code, bool) else None
            )
        if document.get("ok") is not True or "result" not in document:
            code = document.get("error_code")
            raise TelegramAPIError(
                code if isinstance(code, int) and not isinstance(code, bool) else None
            )
        return document["result"]


def read_credential(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value or len(value) > 4096:
        raise ValueError("credential file is empty or unexpectedly large")
    return value


def send_message(
    token: str,
    chat_id: str,
    message: str,
    timeout: int = 15,
    *,
    silent: bool = False,
    transport: TelegramTransport | None = None,
    metadata: NotificationMetadata | None = None,
) -> SendReceipt:
    """Compatibility sender plus optional silent/correlation attributes."""

    return TelegramClient(token, transport=transport, timeout=timeout).send_message(
        chat_id, message, silent=silent, metadata=metadata
    )


def drain_outbox(
    store: StateStore,
    token: str,
    chat_id: str,
    sender: Callable[[str, str, str], Any] = send_message,
) -> tuple[int, int]:
    """Attempt due messages using at-least-once delivery semantics.

    A transport timeout can be ambiguous; leaving the record pending may later duplicate a
    message. It must never be represented as exactly-once or as definitely unsent.
    """

    sent = 0
    failed = 0
    for item in store.due_notifications():
        try:
            sender(token, chat_id, item["message"])
        except Exception:
            store.mark_failed(item["id"], item["attempts"], "delivery-failed")
            failed += 1
        else:
            store.mark_sent(item["id"])
            sent += 1
    return sent, failed


@dataclass(frozen=True)
class NotificationDrainResult:
    sent: int
    failed: int
    retry_after: int | None = None


def _notification_digest(items: list[Mapping[str, Any]]) -> tuple[str, str, bool]:
    """Collapse one due batch into a single bounded operator message."""
    def field(item: Mapping[str, Any], name: str, default: Any) -> Any:
        try:
            return item[name]
        except (KeyError, IndexError):
            return default

    if len(items) == 1:
        item = items[0]
        return str(item["message"]), str(item["severity"]), bool(item["silent"])

    ranks = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    severities: dict[str, int] = {}
    event_types: dict[str, int] = {}
    for item in items:
        severity = str(item["severity"]).lower()
        event_type = str(field(item, "event_type", "incident")).lower()
        severities[severity] = severities.get(severity, 0) + 1
        event_types[event_type] = event_types.get(event_type, 0) + 1
    highest = max(severities, key=lambda value: ranks.get(value, 1))
    lines = [
        f"Terracompute alert digest: {len(items)} updates for machine {MACHINE_ID}",
        "Severity: " + ", ".join(
            f"{name}={severities[name]}"
            for name in sorted(severities, key=lambda value: -ranks.get(value, 1))
        ),
        "Events: " + ", ".join(
            f"{name}={count}" for name, count in sorted(event_types.items())
        ),
        "",
    ]
    shown = min(8, len(items))
    for item in items[:shown]:
        summary = " ".join(str(item["message"]).split())[:240]
        lines.append(
            f"- {str(item['severity']).lower()} "
            f"{str(field(item, 'event_type', 'incident')).lower()} "
            f"{str(field(item, 'incident_id', 'unknown'))[:12]}: {summary}"
        )
    if len(items) > shown:
        lines.append(f"- {len(items) - shown} more updates retained in controller state")
    message = "\n".join(lines).encode("utf-8")[:3500].decode("utf-8", "ignore")
    return message, highest, all(bool(item["silent"]) for item in items)


def drain_outbox_semantic(
    store: StateStore,
    client: TelegramClient,
    chat_id: int | str,
    *,
    limit: int = 20,
) -> NotificationDrainResult:
    """Drain one bounded batch with semantic success and explicit metadata.

    Delivery remains at-least-once.  In particular, an ambiguous transport result is
    retained for retry and may duplicate a message accepted by Telegram.
    """

    sent = failed = 0
    retry_after: int | None = None
    items = store.due_notifications(limit=limit)
    if not items:
        return NotificationDrainResult(0, 0, None)
    message, severity, silent = _notification_digest(items)
    try:
        client.send_message(
            chat_id,
            message,
            silent=silent,
            metadata=NotificationMetadata(
                kind=NotificationKind.INCIDENT,
                severity=severity,
            ),
        )
    except TelegramRateLimited as error:
        for item in items:
            _mark_failed(store, item, "telegram-rate-limited", error.retry_after)
        failed = len(items)
        retry_after = error.retry_after
    except Exception:
        for item in items:
            _mark_failed(store, item, "delivery-failed", None)
        failed = len(items)
    else:
        for item in items:
            store.mark_sent(int(item["id"]))
        sent = len(items)
    return NotificationDrainResult(sent, failed, retry_after)


def _mark_failed(
    store: StateStore,
    item: Mapping[str, Any],
    category: str,
    retry_after: int | None,
) -> None:
    method = store.mark_failed
    parameters = inspect.signature(method).parameters
    if retry_after is not None and "retry_after_seconds" in parameters:
        method(
            int(item["id"]),
            int(item["attempts"]),
            category,
            retry_after_seconds=retry_after,
        )
    else:
        method(int(item["id"]), int(item["attempts"]), category)


class InputKind(str, Enum):
    UNKNOWN_QUESTION = "unknown_question"
    ACKNOWLEDGEMENT = "acknowledgement"
    APPROVAL_COMMAND = "approval_command"
    # A refusal carries no authority to act; it only tells the service to stop asking.
    DENIAL_COMMAND = "denial_command"
    # An operator instruction: it steers what the service does, and never approves.
    INSTRUCTION = "instruction"
    # A question for the controller to answer. It changes nothing.
    QUESTION = "question"


@dataclass(frozen=True)
class AuthenticatedInput:
    """Authenticated input for later policy evaluation, never an execution request."""

    update_id: int
    group_id: int
    sender_id: int
    message_id: int | None
    callback_id: str | None
    kind: InputKind
    subject_id: str | None
    nonce: str | None
    text: str


class UpdateBackend(Protocol):
    """Durable, namespaced cursor/input interface.

    ``store_accepted`` must be idempotent by ``update_id``. The consumer calls it and
    requires success before ``advance_cursor``. A transactional backend may make both
    operations part of one durable transaction, but must preserve that logical ordering.
    """

    def load_cursor(self, namespace: str) -> int: ...

    def store_accepted(self, namespace: str, envelope: AuthenticatedInput) -> None: ...

    def advance_cursor(self, namespace: str, next_offset: int) -> None: ...


class InputRejected(ValueError):
    """A permanent unauthenticated/unsupported update; safe to advance past."""


class AuthenticationUnavailable(TelegramError):
    """Membership could not be established; retain the update for a later retry."""


_ACK = re.compile(
    r"^/ack(?:@([A-Za-z0-9_]+))?\s+([A-Za-z0-9._:-]{1,128})\s*$", re.I
)
_APPROVE = re.compile(
    r"^/approve(?:@([A-Za-z0-9_]+))?\s+([A-Za-z0-9._:-]{1,128})\s+([A-Za-z0-9_-]{8,256})\s*$",
    re.I,
)
_CALLBACK_ACK = re.compile(r"^ack:([A-Za-z0-9._-]{1,128})$")
_CALLBACK_APPROVE = re.compile(
    r"^approve:([A-Za-z0-9._-]{1,128}):([A-Za-z0-9_-]{8,256})$"
)
_CALLBACK_DENY = re.compile(r"^deny:([A-Za-z0-9._-]{1,128}):([A-Za-z0-9_-]{8,256})$")
# Instructions steer the service. The argument is validated by whoever acts on it.
INSTRUCTIONS = ("pause", "resume", "hold", "release", "status", "now", "why", "again")
MAX_QUESTION_CHARS = 256
_ASK = re.compile(
    r"^/ask(?:@([A-Za-z0-9_]+))?\s+([\x20-\x7e]{1,%d})\s*$" % MAX_QUESTION_CHARS
)
_INSTRUCTION = re.compile(
    r"^/(" + "|".join(INSTRUCTIONS) + r")(?:@([A-Za-z0-9_]+))?(?:\s+([A-Za-z0-9._:-]{1,64}))?\s*$",
    re.I,
)
_DENY = re.compile(
    r"^/deny(?:@([A-Za-z0-9_]+))?\s+([A-Za-z0-9._:-]{1,128})\s+([A-Za-z0-9_-]{8,256})\s*$",
    re.I,
)


class SQLiteUpdateBackend:
    """Durable Telegram cursor and authenticated operator inbox.

    The database is deliberately separate from incident state.  Approval-shaped inputs
    are typed and stored but this backend has no execution or approval-creation method.
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
                """CREATE TABLE IF NOT EXISTS telegram_cursor (
                     namespace TEXT PRIMARY KEY,
                     next_offset INTEGER NOT NULL CHECK(next_offset>=0)
                   )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_operator_inbox (
                     namespace TEXT NOT NULL,
                     update_id INTEGER NOT NULL,
                     group_id INTEGER NOT NULL,
                     sender_id INTEGER NOT NULL,
                     message_id INTEGER,
                     callback_id TEXT,
                     kind TEXT NOT NULL,
                     subject_id TEXT,
                     nonce TEXT,
                     text TEXT NOT NULL,
                     stored_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                     handled_utc TEXT,
                     PRIMARY KEY(namespace,update_id)
                   )"""
            )
            self._db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            self.close()
            raise

    def load_cursor(self, namespace: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT next_offset FROM telegram_cursor WHERE namespace=?", (namespace,)
            ).fetchone()
        return 0 if row is None else int(row["next_offset"])

    def store_accepted(self, namespace: str, envelope: AuthenticatedInput) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    """INSERT OR IGNORE INTO telegram_operator_inbox(
                         namespace,update_id,group_id,sender_id,message_id,callback_id,
                         kind,subject_id,nonce,text)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        namespace,
                        envelope.update_id,
                        envelope.group_id,
                        envelope.sender_id,
                        envelope.message_id,
                        envelope.callback_id,
                        envelope.kind.value,
                        envelope.subject_id,
                        envelope.nonce,
                        envelope.text,
                    ),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    def advance_cursor(self, namespace: str, next_offset: int) -> None:
        next_offset = normalize_id(
            next_offset, "next_offset", positive=True, allow_zero=True
        )
        with self._lock:
            self._db.execute(
                """INSERT INTO telegram_cursor(namespace,next_offset) VALUES(?,?)
                   ON CONFLICT(namespace) DO UPDATE SET
                     next_offset=MAX(next_offset,excluded.next_offset)""",
                (namespace, next_offset),
            )
            self._db.commit()

    def mark_handled(self, namespace: str, update_id: int) -> None:
        with self._lock:
            self._db.execute(
                """UPDATE telegram_operator_inbox SET handled_utc=CURRENT_TIMESTAMP
                   WHERE namespace=? AND update_id=? AND handled_utc IS NULL""",
                (namespace, update_id),
            )
            self._db.commit()

    def get_input(self, namespace: str, update_id: int) -> AuthenticatedInput | None:
        """Return one stored authenticated input, handled or not."""
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM telegram_operator_inbox WHERE namespace=? AND update_id=?""",
                (namespace, int(update_id)),
            ).fetchone()
        return None if row is None else _input_from_row(row)

    def pending_inputs(
        self, namespace: str, *, limit: int = 100
    ) -> tuple[AuthenticatedInput, ...]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM telegram_operator_inbox
                   WHERE namespace=? AND handled_utc IS NULL
                   ORDER BY update_id LIMIT ?""",
                (namespace, limit),
            ).fetchall()
        return tuple(_input_from_row(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _input_from_row(row: sqlite3.Row) -> AuthenticatedInput:
    return AuthenticatedInput(
        update_id=int(row["update_id"]),
        group_id=int(row["group_id"]),
        sender_id=int(row["sender_id"]),
        message_id=None if row["message_id"] is None else int(row["message_id"]),
        callback_id=None if row["callback_id"] is None else str(row["callback_id"]),
        kind=InputKind(str(row["kind"])),
        subject_id=None if row["subject_id"] is None else str(row["subject_id"]),
        nonce=None if row["nonce"] is None else str(row["nonce"]),
        text=str(row["text"]),
    )


class TelegramUpdateConsumer:
    """Single-consumer durable long poller for a configured operations group.

    Construction is inert and ``enabled`` defaults to false so provisioning helpers cannot
    accidentally consume deployed updates. Current membership is queried for every input;
    accepted statuses are creator, administrator, member, and restricted only when
    ``is_member`` is explicitly true. Bots, anonymous senders, departed members, wrong
    chats, and unconfigured migration messages are rejected.
    """

    def __init__(
        self,
        client: TelegramClient,
        backend: UpdateBackend,
        *,
        group_id: int | str,
        namespace: str = "terracompute-telegram-v1",
        enabled: bool = False,
        migrations: Mapping[int, int] | None = None,
        bot_username: str = EXPECTED_BOT_USERNAME,
    ):
        if not namespace or len(namespace) > 128:
            raise ValueError("cursor namespace must be non-empty and bounded")
        self.client = client
        self.backend = backend
        self.group_id = normalize_id(group_id, "group_id")
        self.namespace = namespace
        self.enabled = enabled
        self.bot_username = _normalize_bot_username(bot_username)
        self.migrations = dict(migrations or {})
        for old, new in self.migrations.items():
            normalize_id(old, "migration source")
            normalize_id(new, "migration target")
            if new != self.group_id:
                raise ValueError("configured migration must target the configured group")

    def poll_once(self, *, poll_timeout: int = 25) -> tuple[AuthenticatedInput, ...]:
        if not self.enabled:
            return ()
        offset = normalize_id(
            self.backend.load_cursor(self.namespace),
            "cursor",
            positive=True,
            allow_zero=True,
        )
        updates = self.client.get_updates(offset=offset, poll_timeout=poll_timeout)
        accepted: list[AuthenticatedInput] = []
        ordered = sorted(updates, key=lambda item: _update_id(item))
        for update in ordered:
            update_id = _update_id(update)
            if update_id < offset:
                continue
            try:
                envelope = self._authenticate(update, update_id)
            except (InputRejected, TypeError, ValueError):
                self.backend.advance_cursor(self.namespace, update_id + 1)
                offset = update_id + 1
                continue
            # Persist the typed input before the durable cursor makes it invisible to
            # future getUpdates calls. On storage failure neither step is hidden/retried.
            self.backend.store_accepted(self.namespace, envelope)
            self.backend.advance_cursor(self.namespace, update_id + 1)
            offset = update_id + 1
            accepted.append(envelope)
        return tuple(accepted)

    def _authenticate(
        self, update: Mapping[str, Any], update_id: int
    ) -> AuthenticatedInput:
        callback = update.get("callback_query")
        if callback is not None:
            if not isinstance(callback, dict):
                raise InputRejected("invalid callback")
            message = callback.get("message")
            sender = callback.get("from")
            callback_id = normalize_callback_id(callback.get("id"))
            raw_text = callback.get("data")
            if not isinstance(raw_text, str) or len(raw_text.encode("utf-8")) > 512:
                raise InputRejected("invalid callback data")
        else:
            message = update.get("message")
            if not isinstance(message, dict):
                raise InputRejected("unsupported update")
            sender = message.get("from")
            callback_id = None
            raw_text = message.get("text")
            if (
                not isinstance(raw_text, str)
                or len(raw_text.encode("utf-8")) > MAX_MESSAGE_BYTES
            ):
                raise InputRejected("unsupported message")
        if not isinstance(message, dict) or not isinstance(sender, dict):
            raise InputRejected("anonymous input")
        if "sender_chat" in message or sender.get("is_bot") is not False:
            raise InputRejected("bot or anonymous input")
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") not in {"group", "supergroup"}:
            raise InputRejected("wrong chat type")
        raw_group = normalize_id(chat.get("id"), "chat_id")
        migration_to = message.get("migrate_to_chat_id")
        migration_from = message.get("migrate_from_chat_id")
        if migration_to is not None or migration_from is not None:
            if migration_to is None:
                raise InputRejected("unconfigured migration")
            target = normalize_id(migration_to, "migration target")
            if self.migrations.get(raw_group) != target or target != self.group_id:
                raise InputRejected("unconfigured migration")
            # Telegram migration service messages are not operator input.
            raise InputRejected("migration service message")
        if raw_group == self.group_id:
            group_id = raw_group
        elif self.migrations.get(raw_group) == self.group_id:
            group_id = self.group_id
        else:
            raise InputRejected("wrong group")
        sender_id = normalize_id(sender.get("id"), "sender_id", positive=True)
        message_id = normalize_id(
            message.get("message_id"), "message_id", positive=True
        )
        try:
            membership = self.client.get_chat_member(self.group_id, sender_id)
        except TelegramError as error:
            raise AuthenticationUnavailable(
                "telegram-membership-unavailable"
            ) from error
        if not current_human_member(membership, sender_id):
            raise InputRejected("sender is not a current human member")
        kind, subject, nonce = parse_operator_input(
            raw_text,
            callback=callback is not None,
            expected_bot_username=self.bot_username,
        )
        return AuthenticatedInput(
            update_id=update_id,
            group_id=group_id,
            sender_id=sender_id,
            message_id=message_id,
            callback_id=callback_id,
            kind=kind,
            subject_id=subject,
            nonce=nonce,
            text=raw_text[:4096],
        )


def _question_text(text: str, expected_bot_username: str) -> str | None:
    """The question inside a message, bounded and printable."""
    without_mention = re.sub(
        rf"@{re.escape(_normalize_bot_username(expected_bot_username))}\b", " ", text, flags=re.I
    )
    cleaned = " ".join(without_mention.split())
    if not cleaned or not re.fullmatch(r"[\x20-\x7e]+", cleaned):
        return None
    return cleaned[:MAX_QUESTION_CHARS]


def parse_operator_input(
    text: str,
    *,
    callback: bool = False,
    expected_bot_username: str = EXPECTED_BOT_USERNAME,
) -> tuple[InputKind, str | None, str | None]:
    """Classify input without granting authority or invoking any action."""

    if callback:
        ack = _CALLBACK_ACK.fullmatch(text)
        if ack:
            return InputKind.ACKNOWLEDGEMENT, ack.group(1), None
        denial = _CALLBACK_DENY.fullmatch(text)
        if denial:
            return InputKind.DENIAL_COMMAND, denial.group(1), denial.group(2)
        approval = _CALLBACK_APPROVE.fullmatch(text)
    else:
        ack = _ACK.fullmatch(text)
        if ack and _mention_matches(ack.group(1), expected_bot_username):
            return InputKind.ACKNOWLEDGEMENT, ack.group(2), None
        denial = _DENY.fullmatch(text)
        if denial and _mention_matches(denial.group(1), expected_bot_username):
            return InputKind.DENIAL_COMMAND, denial.group(2), denial.group(3)
        instruction = _INSTRUCTION.fullmatch(text)
        if instruction and _mention_matches(instruction.group(2), expected_bot_username):
            return InputKind.INSTRUCTION, instruction.group(1).lower(), instruction.group(3)
        question = _ASK.fullmatch(text)
        if question and _mention_matches(question.group(1), expected_bot_username):
            return InputKind.QUESTION, None, question.group(2).strip()
        approval = _APPROVE.fullmatch(text)
    if callback and approval:
        return InputKind.APPROVAL_COMMAND, approval.group(1), approval.group(2)
    if approval and _mention_matches(approval.group(1), expected_bot_username):
        return InputKind.APPROVAL_COMMAND, approval.group(2), approval.group(3)
    # Anything else said in the group is said to it. Requiring a reply or an @mention
    # made this a command line: in a conversation nobody addresses every line, and a
    # message that went unanswered because it lacked a mention looks like a service
    # that is ignoring you. This is last, so a command keeps its own meaning.
    if not callback and not _aimed_at_another_bot(text, expected_bot_username):
        asked = _question_text(text, expected_bot_username)
        if asked is not None:
            return InputKind.QUESTION, None, asked
    return InputKind.UNKNOWN_QUESTION, None, None


_ADDRESSED_COMMAND = re.compile(r"^/[A-Za-z0-9_]+@([A-Za-z0-9_]+)\b")


def _aimed_at_another_bot(text: str, expected_bot_username: str | None) -> bool:
    """A command sent to a different bot is not this one's conversation to join."""
    match = _ADDRESSED_COMMAND.match(text.strip())
    return bool(match) and not _mention_matches(match.group(1), expected_bot_username)


def _normalize_bot_username(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("bot username must be text")
    normalized = value.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", normalized):
        raise ValueError("bot username is invalid")
    return normalized


def _mention_matches(mention: str | None, expected_bot_username: str) -> bool:
    if mention is None:
        return True
    return mention.casefold() == _normalize_bot_username(expected_bot_username).casefold()


def current_human_member(member: Mapping[str, Any], expected_user_id: int) -> bool:
    user = member.get("user")
    if not isinstance(user, dict):
        return False
    try:
        user_id = normalize_id(user.get("id"), "member user", positive=True)
    except (TypeError, ValueError):
        return False
    if user_id != expected_user_id or user.get("is_bot") is not False:
        return False
    status = member.get("status")
    if status in {"creator", "administrator", "member"}:
        return True
    return status == "restricted" and member.get("is_member") is True


def normalize_id(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    allow_zero: bool = False,
) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    if isinstance(value, str):
        if not re.fullmatch(r"-?[0-9]{1,20}", value):
            raise ValueError(f"{field} must be an integer")
        value = int(value)
    if not isinstance(value, int) or abs(value) > MAX_IDENTIFIER:
        raise ValueError(f"{field} must be a bounded integer")
    if positive and (value < 0 or (value == 0 and not allow_zero)):
        raise ValueError(f"{field} must be positive")
    return value


def normalize_callback_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
    ):
        raise InputRejected("callback id is invalid")
    return value


def _update_id(update: Mapping[str, Any]) -> int:
    return normalize_id(
        update.get("update_id"), "update_id", positive=True, allow_zero=True
    )


# Concise integration name; provisioning code should still leave it disabled.
UpdateConsumer = TelegramUpdateConsumer
