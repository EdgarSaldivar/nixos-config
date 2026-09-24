from __future__ import annotations

import hashlib
import http.server
import threading
import time
import unittest

from terracompute_ops.http_client import (
    HttpClientError,
    HttpRequest,
    HttpResponse,
    RestrictedHttpClient,
    StdlibTransport,
)


class FakeTransport:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.requests: list[tuple[HttpRequest, float, int, str | None]] = []

    def request(
        self,
        request: HttpRequest,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        cert_sha256: str | None = None,
    ) -> HttpResponse:
        self.requests.append(
            (request, timeout_seconds, max_response_bytes, cert_sha256)
        )
        return self.response


class FakeSocket:
    def __init__(self, der: bytes, events: list[str]):
        self.der = der
        self.events = events

    def getpeercert(self, *, binary_form: bool) -> bytes:
        self.events.append("certificate")
        assert binary_form
        return self.der


class FakeNativeResponse:
    status = 200
    closed = False

    def close(self):
        self.closed = True

    def getheader(self, _name: str) -> None:
        return None

    def getheaders(self) -> list[tuple[str, str]]:
        return [("Content-Type", "application/json")]

    def read(self, _limit: int) -> bytes:
        return b"{}"


class FakeConnection:
    def __init__(self, der: bytes, events: list[str]):
        self.sock = FakeSocket(der, events)
        self.events = events
        self.sent_headers: dict[str, str] | None = None

    def connect(self) -> None:
        self.events.append("connect")

    def request(
        self, method: str, target: str, *, body: bytes | None, headers: dict[str, str]
    ) -> None:
        self.events.append("request")
        self.sent_headers = headers

    def getresponse(self) -> FakeNativeResponse:
        return FakeNativeResponse()

    def close(self) -> None:
        self.events.append("close")


class AdvancingClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class SlowReadResponse(FakeNativeResponse):
    def __init__(self, clock: AdvancingClock):
        self.clock = clock

    def read(self, _limit: int) -> bytes:
        # Models a peer that keeps the inactivity timeout alive while the
        # aggregate wall-clock deadline expires during response reading.
        self.clock.value += 2
        return b"{}"


class SlowReadConnection(FakeConnection):
    def __init__(self, der: bytes, events: list[str], clock: AdvancingClock):
        super().__init__(der, events)
        self.clock = clock

    def getresponse(self) -> SlowReadResponse:
        return SlowReadResponse(self.clock)


class HttpClientTests(unittest.TestCase):
    def test_rejected_detached_response_is_closed(self):
        response = FakeNativeResponse()
        response.status = 503
        connection = FakeConnection(b"fixture", [])
        connection.getresponse = lambda: response
        transport = StdlibTransport(connection_factory=lambda *args, **kwargs: connection)
        with self.assertRaises(HttpClientError):
            transport.request(HttpRequest("GET", "http://localhost/", {}),
                              timeout_seconds=1, max_response_bytes=128)
        self.assertTrue(response.closed)

    def test_connection_close_trickle_is_interrupted_at_wall_deadline(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "40")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            start = time.monotonic()
            with self.assertRaisesRegex(HttpClientError, "deadline_exceeded"):
                StdlibTransport().request(
                    HttpRequest("GET", f"http://127.0.0.1:{server.server_port}/", {}),
                    timeout_seconds=0.2, max_response_bytes=100,
                )
            self.assertLess(time.monotonic() - start, 0.8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_exact_routes_prevent_cross_origin_or_arbitrary_path(self) -> None:
        transport = FakeTransport(HttpResponse(200, {}, b"{}"))
        client = RestrictedHttpClient(
            "https://example.test",
            {"/fixed": frozenset({"GET"})},
            transport=transport,
        )
        with self.assertRaisesRegex(HttpClientError, "route_not_allowed"):
            client.request("GET", "https://evil.test/fixed")
        with self.assertRaisesRegex(HttpClientError, "route_not_allowed"):
            client.request("POST", "/fixed")
        self.assertEqual(transport.requests, [])
        client.request("GET", "/fixed")
        request, timeout, limit, pin = transport.requests[0]
        self.assertEqual(request.url, "https://example.test/fixed")
        self.assertEqual(timeout, 10)
        self.assertEqual(limit, 512 * 1024)
        self.assertIsNone(pin)

    def test_write_methods_are_available_only_on_exact_allowed_routes(self) -> None:
        transport = FakeTransport(HttpResponse(200, {}, b"{}"))
        client = RestrictedHttpClient(
            "https://example.test",
            {
                "/delete-only": frozenset({"DELETE"}),
                "/put-only": frozenset({"PUT"}),
            },
            transport=transport,
        )
        client.request("DELETE", "/delete-only")
        client.request("PUT", "/put-only", body=b"{}")
        with self.assertRaisesRegex(HttpClientError, "route_not_allowed"):
            client.request("PUT", "/delete-only")
        with self.assertRaisesRegex(HttpClientError, "route_not_allowed"):
            client.request("DELETE", "/put-only")
        self.assertEqual(
            [(item[0].method, item[0].url) for item in transport.requests],
            [
                ("DELETE", "https://example.test/delete-only"),
                ("PUT", "https://example.test/put-only"),
            ],
        )

    def test_redirect_and_oversize_are_rejected_by_outer_boundary(self) -> None:
        redirect = RestrictedHttpClient(
            "https://example.test",
            {"/fixed": frozenset({"GET"})},
            transport=FakeTransport(HttpResponse(302, {"Location": "https://evil.test"}, b"")),
        )
        with self.assertRaisesRegex(HttpClientError, "redirect_rejected"):
            redirect.request("GET", "/fixed")

        oversized = RestrictedHttpClient(
            "https://example.test",
            {"/fixed": frozenset({"GET"})},
            transport=FakeTransport(HttpResponse(200, {}, b"x" * 1025)),
            max_response_bytes=1024,
        )
        with self.assertRaisesRegex(HttpClientError, "response_too_large"):
            oversized.request("GET", "/fixed")

    def test_transport_exception_details_are_sanitized(self) -> None:
        class FailingTransport:
            def request(self, *_args: object, **_kwargs: object) -> HttpResponse:
                raise HttpClientError("synthetic-secret-in-error")

        client = RestrictedHttpClient(
            "https://example.test",
            {"/fixed": frozenset({"GET"})},
            transport=FailingTransport(),
        )
        with self.assertRaisesRegex(HttpClientError, "request_failed") as raised:
            client.request("GET", "/fixed")
        self.assertNotIn("synthetic-secret", repr(raised.exception))

    def test_request_repr_never_contains_header_values_or_body(self) -> None:
        request = HttpRequest(
            "GET",
            "https://example.test/fixed",
            {"Authorization": "Bearer synthetic-secret"},
            b"synthetic-body-secret",
        )
        rendered = repr(request)
        self.assertIn("Authorization", rendered)
        self.assertNotIn("synthetic-secret", rendered)
        self.assertNotIn("synthetic-body-secret", rendered)
        response = HttpResponse(
            200, {"Set-Cookie": "synthetic-response-secret"}, b"secret body"
        )
        self.assertNotIn("synthetic-response-secret", repr(response))
        self.assertNotIn("secret body", repr(response))

    def test_certificate_pin_mismatch_occurs_before_authorization_is_sent(self) -> None:
        events: list[str] = []
        connection = FakeConnection(b"presented-der", events)
        transport = StdlibTransport(connection_factory=lambda *_args, **_kwargs: connection)
        request = HttpRequest(
            "GET",
            "https://10.0.15.237/redfish/v1/",
            {"Authorization": "Basic synthetic"},
        )
        with self.assertRaisesRegex(HttpClientError, "certificate_pin_mismatch"):
            transport.request(
                request,
                timeout_seconds=1,
                max_response_bytes=1024,
                cert_sha256="0" * 64,
            )
        self.assertEqual(events, ["connect", "certificate", "close"])
        self.assertIsNone(connection.sent_headers)

    def test_matching_pin_is_checked_before_each_request(self) -> None:
        events: list[str] = []
        der = b"synthetic-certificate-der"
        connections: list[FakeConnection] = []

        def factory(*_args: object, **_kwargs: object) -> FakeConnection:
            connection = FakeConnection(der, events)
            connections.append(connection)
            return connection

        transport = StdlibTransport(connection_factory=factory)
        request = HttpRequest(
            "GET",
            "https://10.0.15.237/redfish/v1/",
            {"Authorization": "Basic synthetic"},
        )
        pin = hashlib.sha256(der).hexdigest()
        for _ in range(2):
            transport.request(
                request,
                timeout_seconds=1,
                max_response_bytes=1024,
                cert_sha256=pin,
            )
        self.assertEqual(len(connections), 2)
        self.assertEqual(
            events,
            [
                "connect",
                "certificate",
                "request",
                "close",
                "connect",
                "certificate",
                "request",
                "close",
            ],
        )

    def test_slow_trickle_read_cannot_outlive_absolute_deadline(self) -> None:
        clock = AdvancingClock()
        events: list[str] = []
        der = b"synthetic-certificate-der"
        connection = SlowReadConnection(der, events, clock)
        transport = StdlibTransport(
            connection_factory=lambda *_args, **_kwargs: connection,
            monotonic=clock,
        )
        request = HttpRequest(
            "GET",
            "https://10.0.15.237/redfish/v1/",
            {"Authorization": "Basic synthetic"},
        )
        with self.assertRaisesRegex(HttpClientError, "deadline_exceeded"):
            transport.request(
                request,
                timeout_seconds=1,
                max_response_bytes=1024,
                cert_sha256=hashlib.sha256(der).hexdigest(),
            )
        self.assertEqual(events[:3], ["connect", "certificate", "request"])


if __name__ == "__main__":
    unittest.main()
