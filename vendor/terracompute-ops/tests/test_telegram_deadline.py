import http.client
import http.server
import threading
import time
import unittest
from unittest import mock

from terracompute_ops.telegram import StdlibTelegramTransport, TelegramError


class TelegramDeadlineTests(unittest.TestCase):
    def test_connection_close_response_trickle_cannot_delay_next_alert(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', '40')
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                        time.sleep(0.05)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def connection(_host, _port, *, timeout, context):
                return http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=timeout)

            with mock.patch('terracompute_ops.telegram.http.client.HTTPSConnection', side_effect=connection):
                start = time.monotonic()
                with self.assertRaises((TelegramError, OSError, http.client.HTTPException)):
                    StdlibTelegramTransport().request(
                        '/botfixture/sendMessage', b'{}', timeout=0.2, max_response_bytes=100
                    )
                self.assertLess(time.monotonic() - start, 0.8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
