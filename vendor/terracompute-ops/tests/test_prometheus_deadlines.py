from __future__ import annotations

import http.server
import json
import threading
import time
import unittest
import urllib.parse
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from terracompute_ops.prometheus import PrometheusClient, PrometheusError


def _metric_body() -> bytes:
    now = time.time()
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"__name__": "up"}, "value": [now, str(now)]}
                ],
            },
        }
    ).encode()


class _Response:
    status = 200

    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


@contextmanager
def _trickle_server(body: bytes) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for byte in body:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(0.03)
            except OSError:
                pass

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class PrometheusDeadlineTests(unittest.TestCase):
    def test_metric_connection_close_trickle_is_cancelled(self) -> None:
        with _trickle_server(_metric_body()) as endpoint:
            client = PrometheusClient(
                endpoint,
                timeout_seconds=0.2,
                aggregate_timeout_seconds=1,
            )
            started = time.monotonic()
            with self.assertRaisesRegex(PrometheusError, "request_failed") as caught:
                client.fetch("17049", "vastai-exporter", "dcgm-exporter")
            elapsed = time.monotonic() - started

        self.assertEqual(caught.exception.query_id, "vast")
        self.assertLess(elapsed, 0.8)

    def test_alert_connection_close_trickle_is_cancelled(self) -> None:
        body = b'{"status":"success","data":{"alerts":[]}}'
        with _trickle_server(body) as endpoint:
            client = PrometheusClient(endpoint, timeout_seconds=0.2)
            started = time.monotonic()
            with self.assertRaisesRegex(PrometheusError, "request_failed") as caught:
                client.fetch_alerts()
            elapsed = time.monotonic() - started

        self.assertEqual(caught.exception.query_id, "alerts")
        self.assertLess(elapsed, 0.8)

    def test_metric_queries_share_one_aggregate_deadline(self) -> None:
        class MonotonicClock:
            value = 0.0

            def __call__(self) -> float:
                return self.value

            def advance(self, seconds: float) -> None:
                self.value += seconds

        monotonic = MonotonicClock()

        class DelayedOpener:
            def __init__(self) -> None:
                self.timeouts: list[float] = []

            def open(self, request: object, timeout: float) -> _Response:
                del request
                self.timeouts.append(timeout)
                monotonic.advance(0.05)
                return _Response(_metric_body())

        opener = DelayedOpener()
        client = PrometheusClient(
            "http://prometheus.example:9090",
            timeout_seconds=1,
            aggregate_timeout_seconds=0.13,
            opener=opener,
        )
        with patch("terracompute_ops.prometheus.time.monotonic", monotonic):
            with self.assertRaisesRegex(PrometheusError, "request_failed"):
                client.fetch("17049", "vastai-exporter", "dcgm-exporter")

        self.assertEqual(len(opener.timeouts), 3)
        self.assertTrue(
            all(
                later < earlier
                for earlier, later in zip(opener.timeouts, opener.timeouts[1:])
            )
        )
        self.assertGreaterEqual(monotonic.value, 0.13)

    def test_alert_fetch_has_an_independent_deadline_after_metric_failure(self) -> None:
        class SplitOpener:
            def open(self, request: object, timeout: float) -> _Response:
                del timeout
                if urllib.parse.urlsplit(request.full_url).path == "/api/v1/query":
                    raise OSError("synthetic query failure")
                return _Response(b'{"status":"success","data":{"alerts":[]}}')

        client = PrometheusClient(
            "http://prometheus.example:9090", opener=SplitOpener()
        )
        with self.assertRaises(PrometheusError):
            client.fetch("17049", "vastai-exporter", "dcgm-exporter")

        self.assertTrue(client.fetch_alerts().healthy)


if __name__ == "__main__":
    unittest.main()
