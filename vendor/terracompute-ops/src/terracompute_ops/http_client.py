"""Small bounded HTTP primitives for fixed-origin observation clients.

Integration contract: callers construct :class:`RestrictedHttpClient` with an
immutable origin and route/method allowlist.  The transport is injectable for
offline tests, but production transports never follow redirects, cap response
bytes, and reduce failures to secret-free reason codes.  ``cert_sha256`` asks
the stdlib transport to pin the peer certificate DER *before* it writes any
HTTP headers; this is the mechanism used by the Redfish adapter.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol


MAX_RESPONSE_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_CONCURRENCY = 4
_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}\Z")
_PIN = re.compile(r"[0-9a-fA-F]{64}\Z")
_TRANSPORT_REASONS = frozenset(
    {
        "certificate_pin_mismatch",
        "deadline_exceeded",
        "http_status",
        "malformed_content_length",
        "pin_requires_https",
        "redirect_rejected",
        "request_failed",
        "response_too_large",
        "tls_peer_unavailable",
    }
)


class HttpClientError(RuntimeError):
    """A bounded, secret-free failure suitable for persisted evidence."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status

    def __repr__(self) -> str:
        return f"HttpClientError(reason={self.reason!r}, status={self.status!r})"


@dataclass(frozen=True, repr=False)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None

    def __repr__(self) -> str:
        # Header values and bodies can contain credentials or tenant evidence.
        names = tuple(sorted(self.headers))
        return f"HttpRequest(method={self.method!r}, url={self.url!r}, header_names={names!r})"


@dataclass(frozen=True, repr=False)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def __repr__(self) -> str:
        names = tuple(sorted(self.headers)) if isinstance(self.headers, Mapping) else ()
        size = len(self.body) if isinstance(self.body, bytes) else None
        return f"HttpResponse(status={self.status!r}, header_names={names!r}, body_bytes={size!r})"


class HttpTransport(Protocol):
    """Offline-injectable transport contract used by all clients in this unit."""

    def request(
        self,
        request: HttpRequest,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        cert_sha256: str | None = None,
    ) -> HttpResponse: ...


class StdlibTransport:
    """No-proxy/no-redirect stdlib transport, with optional pre-auth pinning."""

    def __init__(
        self,
        *,
        connection_factory: object | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._connection_factory = connection_factory
        self._monotonic = monotonic

    def _read_bounded(
        self,
        response: object,
        limit: int,
        deadline: float,
        expired: threading.Event,
    ) -> bytes:
        raw_length = response.getheader("Content-Length")
        if raw_length is not None:
            try:
                if int(raw_length) > limit:
                    raise HttpClientError("response_too_large")
            except ValueError:
                raise HttpClientError("malformed_content_length") from None
        try:
            body = response.read(limit + 1)
            if not isinstance(body, bytes):
                raise HttpClientError("request_failed")
            if self._monotonic() >= deadline or expired.is_set():
                raise HttpClientError("deadline_exceeded")
            if len(body) > limit:
                raise HttpClientError("response_too_large")
            return body
        except HttpClientError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            if expired.is_set() or self._monotonic() >= deadline:
                raise HttpClientError("deadline_exceeded") from None
            raise HttpClientError("request_failed") from None

    def request(
        self,
        request: HttpRequest,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        cert_sha256: str | None = None,
    ) -> HttpResponse:
        deadline = self._monotonic() + timeout_seconds
        return self._request_direct(
            request, deadline, max_response_bytes, cert_sha256
        )

    def _request_direct(
        self, request: HttpRequest, deadline: float,
        max_response_bytes: int, cert_sha256: str | None,
    ) -> HttpResponse:
        parsed = urllib.parse.urlsplit(request.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HttpClientError("request_failed")
        if cert_sha256 is not None and parsed.scheme != "https":
            raise HttpClientError("pin_requires_https")
        connection = None
        response = None
        connected_socket = None
        watchdog = None
        expired = threading.Event()
        try:
            factory = self._connection_factory
            kwargs: dict[str, object] = {
                "timeout": max(0.001, deadline - self._monotonic())
            }
            if parsed.scheme == "https":
                if cert_sha256 is None:
                    context = ssl.create_default_context()
                else:
                    # A pinned private BMC commonly has a self-signed
                    # certificate.  Its DER digest is the trust anchor.
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                kwargs["context"] = context
                factory = factory or http.client.HTTPSConnection
            else:
                factory = factory or http.client.HTTPConnection
            connection = factory(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                **kwargs,
            )

            def expire() -> None:
                expired.set()
                # HTTPConnection detaches its socket on Connection: close,
                # while HTTPResponse still owns a readable file wrapper.
                sock = connected_socket or getattr(connection, "sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except (AttributeError, OSError):
                        pass
                try:
                    connection.close()
                except OSError:
                    pass

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise HttpClientError("deadline_exceeded")
            watchdog = threading.Timer(remaining, expire)
            watchdog.daemon = True
            watchdog.start()
            # Explicit connect is essential on the pin path: inspect the TLS
            # peer before HTTPConnection.request() writes Authorization.
            connection.connect()
            if self._monotonic() >= deadline or expired.is_set():
                raise HttpClientError("deadline_exceeded")
            sock = connection.sock
            connected_socket = sock
            if sock is None:
                raise HttpClientError("tls_peer_unavailable")
            if cert_sha256 is not None:
                peer_der = sock.getpeercert(binary_form=True)
                actual = hashlib.sha256(peer_der).hexdigest()
                if not _constant_time_hex_equal(actual, cert_sha256):
                    raise HttpClientError("certificate_pin_mismatch")
            if self._monotonic() >= deadline or expired.is_set():
                raise HttpClientError("deadline_exceeded")
            set_timeout = getattr(sock, "settimeout", None)
            if set_timeout is not None:
                set_timeout(max(0.001, deadline - self._monotonic()))
            target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
            connection.request(
                request.method, target, body=request.body, headers=dict(request.headers)
            )
            response = connection.getresponse()
            if self._monotonic() >= deadline or expired.is_set():
                raise HttpClientError("deadline_exceeded")
            status = int(response.status)
            if 300 <= status < 400:
                raise HttpClientError("redirect_rejected", status)
            if not 200 <= status < 300:
                raise HttpClientError("http_status", status)
            body = self._read_bounded(response, max_response_bytes, deadline, expired)
            return HttpResponse(status, dict(response.getheaders()), body)
        except HttpClientError as error:
            if error.reason in _TRANSPORT_REASONS:
                raise HttpClientError(error.reason, error.status) from None
            raise HttpClientError("request_failed") from None
        except (OSError, ValueError, http.client.HTTPException, ssl.SSLError):
            if expired.is_set() or self._monotonic() >= deadline:
                raise HttpClientError("deadline_exceeded") from None
            raise HttpClientError("request_failed") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            close_response = getattr(response, "close", None)
            if close_response is not None:
                try:
                    close_response()
                except OSError:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass


def _constant_time_hex_equal(actual: str, expected: str) -> bool:
    """Compare certificate digests without reflecting either value in errors."""
    import hmac

    return hmac.compare_digest(actual.lower(), expected.lower())


class RestrictedHttpClient:
    """Issue bounded requests only to constructor-approved exact routes.

    This class owns no application state.  Higher-level schedulers may inject a
    transport and call it from their own bounded worker pools.  A per-instance
    semaphore independently caps accidental concurrent use.
    """

    def __init__(
        self,
        origin: str,
        allowed_routes: Mapping[str, frozenset[str]],
        *,
        transport: HttpTransport | None = None,
        timeout_seconds: float = 10,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_concurrency: int = 1,
        cert_sha256: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        parsed = urllib.parse.urlsplit(origin)
        try:
            port = parsed.port
        except ValueError:
            raise HttpClientError("invalid_origin") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise HttpClientError("invalid_origin")
        if not 0.1 <= timeout_seconds <= 15:
            raise HttpClientError("invalid_timeout")
        if not 1024 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise HttpClientError("invalid_response_limit")
        if not 1 <= max_concurrency <= MAX_CONCURRENCY:
            raise HttpClientError("invalid_concurrency")
        if cert_sha256 is not None and not _PIN.fullmatch(cert_sha256):
            raise HttpClientError("invalid_certificate_pin")
        clean_routes: dict[str, frozenset[str]] = {}
        for path, methods in allowed_routes.items():
            if (
                not _valid_path(path)
                or not methods
                or not methods <= {"DELETE", "GET", "POST", "PUT"}
            ):
                raise HttpClientError("invalid_route_allowlist")
            clean_routes[path] = frozenset(methods)
        hostname = parsed.hostname
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            authority += f":{port}"
        self._origin = f"https://{authority}"
        self._routes = clean_routes
        self._transport = transport or StdlibTransport()
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._cert_sha256 = cert_sha256
        self._monotonic = monotonic
        self._slots = threading.BoundedSemaphore(max_concurrency)

    def __repr__(self) -> str:
        return (
            f"RestrictedHttpClient(origin={self._origin!r}, "
            f"routes={tuple(sorted(self._routes))!r})"
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        deadline: float | None = None,
    ) -> HttpResponse:
        method = method.upper()
        if path not in self._routes or method not in self._routes[path]:
            raise HttpClientError("route_not_allowed")
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            raise HttpClientError("request_too_large")
        clean_headers: dict[str, str] = {}
        for name, value in (headers or {}).items():
            if (
                not isinstance(name, str)
                or not _HEADER_NAME.fullmatch(name)
                or not isinstance(value, str)
                or len(value) > 8192
                or "\r" in value
                or "\n" in value
            ):
                raise HttpClientError("invalid_header")
            clean_headers[name] = value
        wait_deadline = self._monotonic() + self._timeout_seconds
        if deadline is not None:
            wait_deadline = min(wait_deadline, deadline)
        remaining = wait_deadline - self._monotonic()
        if remaining <= 0:
            raise HttpClientError("deadline_exceeded")
        acquired = self._slots.acquire(timeout=remaining)
        if not acquired:
            if self._monotonic() >= wait_deadline:
                raise HttpClientError("deadline_exceeded")
            raise HttpClientError("concurrency_limit")
        try:
            request_deadline = self._monotonic() + self._timeout_seconds
            if deadline is not None:
                request_deadline = min(request_deadline, deadline)
            remaining = request_deadline - self._monotonic()
            if remaining <= 0:
                raise HttpClientError("deadline_exceeded")
            response = self._transport.request(
                HttpRequest(method, self._origin + path, clean_headers, body),
                timeout_seconds=(self._timeout_seconds if deadline is None else remaining),
                max_response_bytes=self._max_response_bytes,
                cert_sha256=self._cert_sha256,
            )
        except HttpClientError as error:
            if error.reason in _TRANSPORT_REASONS:
                raise HttpClientError(error.reason, error.status) from None
            raise HttpClientError("request_failed") from None
        except Exception:
            # Injectable transports are untrusted with respect to error text.
            raise HttpClientError("request_failed") from None
        finally:
            self._slots.release()
        if self._monotonic() >= request_deadline:
            raise HttpClientError("deadline_exceeded")
        if (
            not isinstance(response, HttpResponse)
            or isinstance(response.status, bool)
            or not isinstance(response.status, int)
            or not 100 <= response.status <= 599
            or not isinstance(response.headers, Mapping)
            or not isinstance(response.body, bytes)
        ):
            raise HttpClientError("malformed_transport_response")
        if 300 <= response.status < 400:
            raise HttpClientError("redirect_rejected", response.status)
        if not 200 <= response.status < 300:
            raise HttpClientError("http_status", response.status)
        if len(response.body) > self._max_response_bytes:
            raise HttpClientError("response_too_large")
        length = _header(response.headers, "content-length")
        if length is not None:
            try:
                parsed_length = int(length)
                if parsed_length < 0:
                    raise HttpClientError("malformed_content_length")
                if parsed_length > self._max_response_bytes:
                    raise HttpClientError("response_too_large")
            except ValueError:
                raise HttpClientError("malformed_content_length") from None
        return response

    def request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        payload: object | None = None,
        deadline: float | None = None,
    ) -> object:
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            try:
                body = json.dumps(
                    payload, separators=(",", ":"), sort_keys=True, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise HttpClientError("invalid_json_request") from None
            request_headers["Content-Type"] = "application/json"
        request_headers.setdefault("Accept", "application/json")
        response = self.request(
            method, path, headers=request_headers, body=body, deadline=deadline
        )
        content_type = _header(response.headers, "content-type")
        if content_type is not None and content_type.split(";", 1)[0].strip().lower() not in {
            "application/json",
            "application/odata+json",
        }:
            raise HttpClientError("unexpected_content_type")
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise HttpClientError("malformed_json_response") from None


def _valid_path(path: object) -> bool:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 512:
        return False
    parsed = urllib.parse.urlsplit(path)
    return (
        not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
        and parsed.path == path
        and ".." not in path.split("/")
    )


def _header(headers: Mapping[str, str], wanted: str) -> str | None:
    for name, value in headers.items():
        if name.lower() == wanted:
            return value
    return None
