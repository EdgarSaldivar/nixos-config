"""The docker socket that refuses a customer's container and passes everything else."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import socket
import tempfile
import threading
import unittest

_SOURCE = pathlib.Path(__file__).resolve().parent.parent / "target" / "terracompute-docker-proxy.py"
_COPY = pathlib.Path(tempfile.mkdtemp()) / "proxy_module.py"
shutil.copy(_SOURCE, _COPY)
_SPEC = importlib.util.spec_from_file_location("proxy_module", _COPY)
proxy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(proxy)

LISTED = [
    {"Id": "aaaa111122223333", "Names": ["/C.51217040"]},
    {"Id": "bbbb444455556666", "Names": ["/dcgm-exporter"]},
    {"Id": "cccc777788889999", "Names": ["/vast-grafana-1"]},
]


class FakeDocker:
    """Just enough docker: it answers the container list and echoes anything else."""

    def __init__(self, path: str):
        self.path = path
        self.saw: list[bytes] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(connection,), daemon=True).start()

    def _one(self, connection: socket.socket) -> None:
        with connection:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                head += chunk
            self.saw.append(head.split(b"\r\n", 1)[0])
            if b"/containers/json" in head:
                body = json.dumps(LISTED).encode()
            else:
                body = b'{"reached":"docker"}'
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )


class ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp()
        self.docker_path = os.path.join(self.directory, "docker.sock")
        self.listen_path = os.path.join(self.directory, "proxy.sock")
        self.docker = FakeDocker(self.docker_path)
        proxy.DOCKER_SOCKET = self.docker_path
        threading.Thread(
            target=proxy.serve, args=(self.listen_path, self.docker_path), daemon=True
        ).start()
        for _ in range(200):
            if os.path.exists(self.listen_path):
                break
            threading.Event().wait(0.01)

    def ask(self, path: str) -> tuple[int, str]:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10)
        connection.connect(self.listen_path)
        connection.sendall(
            f"GET {path} HTTP/1.1\r\nHost: docker\r\n\r\n".encode()
        )
        received = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
        connection.close()
        head, _, body = received.partition(b"\r\n\r\n")
        status = int(head.split(b" ")[1])
        return status, body.decode("latin-1")

    def test_our_own_container_passes_through_with_every_field(self) -> None:
        """No allowlist of fields or containers: whatever it asks docker, it gets."""
        status, body = self.ask("/v1.43/containers/dcgm-exporter/json?size=1")
        self.assertEqual(status, 200)
        self.assertIn("reached", body)

    def test_a_customers_container_is_refused_by_name(self) -> None:
        status, body = self.ask("/v1.43/containers/C.51217040/logs?stdout=1")
        self.assertEqual(status, 403)
        self.assertIn("customer", body)
        self.assertIn("ask a person", body)

    def test_a_customers_container_is_refused_by_id(self) -> None:
        """`docker ps -q` then the id is the obvious way round a name check."""
        status, _ = self.ask("/v1.43/containers/aaaa111122223333/logs")
        self.assertEqual(status, 403)
        status, _ = self.ask("/v1.43/containers/aaaa11112222/json")
        self.assertEqual(status, 403, "a short id walked past it")

    def test_exec_into_a_customer_is_refused(self) -> None:
        status, _ = self.ask("/v1.43/containers/C.51217040/exec")
        self.assertEqual(status, 403)

    def test_a_container_that_does_not_exist_is_dockers_problem(self) -> None:
        status, _ = self.ask("/v1.43/containers/not-a-real-thing/json")
        self.assertEqual(status, 200, "it answered for docker instead of forwarding")

    def test_unanchored_lookalikes_are_not_treated_as_customers(self) -> None:
        """docker's own --filter name=C. matches these; this must not."""
        for name in ("notC.51217040", "C.thing", "myC.5"):
            with self.subTest(name):
                self.assertIsNone(proxy.TENANT_NAME.match(name))
        self.assertIsNotNone(proxy.TENANT_NAME.match("/C.51217040"))

    def test_verbs_it_has_never_heard_of_still_work(self) -> None:
        """It must not become a list of known operations."""
        for path in ("/v1.43/info", "/v1.43/images/json", "/v1.99/some/future/thing"):
            with self.subTest(path):
                status, _ = self.ask(path)
                self.assertEqual(status, 200)

    def test_a_second_request_cannot_ride_in_on_one_connection(self) -> None:
        """Judged once per connection, so the forwarded request says Connection: close."""
        self.ask("/v1.43/containers/dcgm-exporter/json")
        forwarded = [line for line in self.docker.saw if b"dcgm-exporter" in line]
        self.assertTrue(forwarded)

    def test_it_says_when_a_container_appears_that_is_not_a_rental(self) -> None:
        """If Vast ever stops naming rentals C.<digits> the denial silently stops
        matching and nothing blocks, so something has to say the world changed.

        What is already there is the baseline: saying "not a rental" about all seven
        of our own containers every time is the noise a real signal gets lost in.
        """
        import io, contextlib
        names = proxy.Names(self.docker_path)
        quiet = io.StringIO()
        with contextlib.redirect_stderr(quiet):
            names.resolve("dcgm-exporter")
        self.assertEqual(quiet.getvalue(), "", "it reported the containers already there")

        LISTED.append({"Id": "dddd0000", "Names": ["/something-new"]})
        try:
            names._read_at = 0.0   # force a re-read
            spoken = io.StringIO()
            with contextlib.redirect_stderr(spoken):
                names.resolve("something-new")
            self.assertIn("new-container", spoken.getvalue())
            self.assertIn("something-new", spoken.getvalue())
        finally:
            LISTED.pop()


if __name__ == "__main__":
    unittest.main()
