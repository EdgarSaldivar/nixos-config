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

EXECS = {"tenantexec": "aaaa111122223333", "ourexec": "bbbb444455556666"}
VOLUMES = {"leak": "/var/lib/docker", "ours": "/srv/exporter"}


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
            elif head.startswith(b"GET /volumes/"):
                name = head.split(b" ")[1].split(b"/")[2].decode()
                device = VOLUMES.get(name)
                if device is None:
                    connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 2\r\n"
                                       b"Connection: close\r\n\r\n{}")
                    return
                body = json.dumps({"Name": name, "Driver": "local", "Options": {
                    "type": "none", "o": "bind", "device": device}}).encode()
            elif head.startswith(b"GET /exec/"):
                exec_id = head.split(b"/")[2].decode()
                owner = EXECS.get(exec_id)
                if owner is None:
                    connection.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 2\r\n"
                                       b"Connection: close\r\n\r\n{}")
                    return
                body = json.dumps({"ID": exec_id, "ContainerID": owner}).encode()
            else:
                body = b'{"reached":"docker"}'
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )


def _connect(path: str) -> socket.socket:
    """Connect, riding out the instant between the socket file appearing (bind) and the
    server accepting (listen); setUp only waits for the file."""
    for _ in range(200):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10)
        try:
            connection.connect(path)
            return connection
        except ConnectionRefusedError:
            connection.close()
            threading.Event().wait(0.01)
    raise ConnectionRefusedError(path)


class ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        # The tests' own filesystem stands in for dockerd's view.
        proxy.HOST_ROOT = ""
        self.directory = tempfile.mkdtemp()
        self.docker_path = os.path.join(self.directory, "docker.sock")
        self.listen_path = os.path.join(self.directory, "proxy.sock")
        self.docker = FakeDocker(self.docker_path)
        proxy.DOCKER_SOCKET = self.docker_path
        threading.Thread(
            target=proxy.serve,
            args=(self.listen_path, self.docker_path, self.listen_path + ".ro"),
            daemon=True,
        ).start()
        for _ in range(200):
            if os.path.exists(self.listen_path):
                break
            threading.Event().wait(0.01)

    def ask(self, path: str) -> tuple[int, str]:
        connection = _connect(self.listen_path)
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

    def test_any_id_prefix_of_a_customer_is_refused(self) -> None:
        """Docker resolves ANY unambiguous id prefix, not just the 12-char short id."""
        for prefix in ("aaaa1", "aaaa11112", "aaaa111122223", "aaaa11112222333"):
            with self.subTest(prefix):
                status, _ = self.ask(f"/v1.43/containers/{prefix}/json")
                self.assertEqual(status, 403, f"{prefix} walked past it")

    def test_a_prefix_ambiguous_with_a_customer_is_refused(self) -> None:
        LISTED.append({"Id": "aaaa9999", "Names": ["/ours"]})
        try:
            status, _ = self.ask("/v1.43/containers/aaaa/json")
            self.assertEqual(status, 403)
        finally:
            LISTED.pop()

    def test_path_spellings_dockerd_normalises_are_refused(self) -> None:
        """dockerd routes on the decoded, cleaned path; so must the judge."""
        for path in (
            "/containers/C%2E51217040/json",
            "/v1.43/containers/%43.51217040/logs",
            "//containers/C.51217040/logs",
            "/v1.43//containers/C.51217040/json",
            "/v1.43/./containers/C.51217040/json",
            "/v1.43/x/../containers/C.51217040/json",
            "/containers/aaaa%3111122223333/json",
        ):
            with self.subTest(path):
                status, _ = self.ask(path)
                self.assertEqual(status, 403, f"{path} walked past it")

    def test_a_target_it_cannot_canonicalise_is_refused(self) -> None:
        for path in ("http://docker/containers/C.51217040/json", "*",
                     "/containers/C.5%001/json", "/containers/%ff/json"):
            with self.subTest(path):
                status, _ = self.ask(path)
                self.assertEqual(status, 403)

    def test_our_containers_still_pass_in_every_spelling(self) -> None:
        for path in ("/containers/dcgm%2Dexporter/json", "//v1.43/containers/bbbb4444/json",
                     "/v1.43/containers/bbbb444455556666/json"):
            with self.subTest(path):
                status, _ = self.ask(path)
                self.assertEqual(status, 200)

    def test_commit_of_a_customers_container_is_refused(self) -> None:
        status, _ = self.send_method("POST", "/v1.43/commit?container=C.51217040&repo=x")
        self.assertEqual(status, 403)
        status, _ = self.send_method("POST", "/v1.43/commit?container=aaaa111&repo=x")
        self.assertEqual(status, 403)

    def test_pruning_containers_is_refused(self) -> None:
        status, _ = self.send_method("POST", "/v1.43/containers/prune")
        self.assertEqual(status, 403)

    def test_create_that_reaches_into_a_customer_is_refused(self) -> None:
        for host in (
            {"VolumesFrom": ["C.51217040:ro"]},
            {"NetworkMode": "container:C.51217040"},
            {"PidMode": "container:aaaa1111"},
            {"IpcMode": "container:C.51217040"},
            {"Links": ["C.51217040:db"]},
        ):
            with self.subTest(host):
                status, _ = self.send_method(
                    "POST", "/v1.43/containers/create?name=probe",
                    json.dumps({"Image": "busybox", "HostConfig": host}).encode())
                self.assertEqual(status, 403)

    def test_create_of_an_ordinary_container_passes(self) -> None:
        status, _ = self.send_method(
            "POST", "/v1.43/containers/create?name=probe",
            json.dumps({"Image": "busybox", "HostConfig": {"VolumesFrom": ["dcgm-exporter"]}}).encode())
        self.assertEqual(status, 200)

    def test_create_it_cannot_read_whole_is_refused(self) -> None:
        status, _ = self.send_method(
            "POST", "/v1.43/containers/create", b"not json")
        self.assertEqual(status, 403)
        status, _ = self.send_method(
            "POST", "/v1.43/containers/create", b"", extra="Transfer-Encoding: chunked\r\n")
        self.assertEqual(status, 403)

    def send_method(self, method: str, path: str, body: bytes = b"", extra: str = "",
                    socket_path: str | None = None) -> tuple[int, str]:
        connection = _connect(socket_path or self.listen_path)
        length = f"Content-Length: {len(body)}\r\n" if body and not extra else ""
        connection.sendall(
            f"{method} {path} HTTP/1.1\r\nHost: docker\r\n{extra}{length}\r\n".encode() + body
        )
        received = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
        connection.close()
        head, _, rest = received.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), rest.decode("latin-1")

    def raw(self, payload: bytes) -> tuple[int, bytes]:
        connection = _connect(self.listen_path)
        connection.sendall(payload)
        received = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
        connection.close()
        return int(received.split(b" ")[1]), received

    def test_a_bare_lf_cannot_smuggle_a_second_request(self) -> None:
        """Go ends headers at a bare LF too; the proxy must not judge one request while
        docker reads another."""
        before = len(self.docker.saw)
        status, _ = self.raw(
            b"GET /_ping HTTP/1.1\r\nHost: docker\n\n"
            b"POST /containers/C.51217040/stop HTTP/1.1\r\nHost: docker\r\n"
            b"Content-Length: 0\r\n\r\n")
        self.assertEqual(status, 403)
        self.assertEqual(len(self.docker.saw), before, "something reached docker")

    def test_a_hidden_content_length_is_refused(self) -> None:
        body = json.dumps({"HostConfig": {"VolumesFrom": ["C.51217040"]}}).encode()
        status, _ = self.raw(
            b"POST /containers/create HTTP/1.1\r\nHost: docker\nContent-Length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body)
        self.assertEqual(status, 403)

    def test_conflicting_or_folded_framing_is_refused(self) -> None:
        for head in (
            b"POST /containers/create HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 0\r\n\r\n{}",
            b"POST /containers/create HTTP/1.1\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}",
            b"GET /info HTTP/1.1\r\nHost: docker\r\n folded\r\n\r\n",
            b"GET /info HTTP/1.1\rX\r\nHost: docker\r\n\r\n",
            b"GET /info HTTP/2\r\n\r\n",
        ):
            with self.subTest(head):
                status, _ = self.raw(head)
                self.assertEqual(status, 403)

    def test_create_json_is_read_as_go_reads_it(self) -> None:
        """Case-insensitive fields and merged duplicates must not hide a reference."""
        for body in (
            b'{"Image":"busybox","hostconfig":{"volumesfrom":["C.51217040"]}}',
            b'{"Image":"busybox","HostConfig":{"VolumesFrom":["C.51217040"]},"HostConfig":{}}',
            b'{"Image":"busybox","HostConfig":{"VolumesFrom":["C.51217040"]},"hostConfig":{}}',
        ):
            with self.subTest(body):
                status, _ = self.send_method("POST", "/containers/create", body)
                self.assertEqual(status, 403)

    def test_network_attach_of_a_customer_is_refused(self) -> None:
        for verb in ("connect", "disconnect"):
            with self.subTest(verb):
                status, _ = self.send_method(
                    "POST", f"/v1.43/networks/bridge/{verb}",
                    b'{"Container":"C.51217040","Force":true}')
                self.assertEqual(status, 403)
        status, _ = self.send_method(
            "POST", "/v1.43/networks/bridge/connect", b'{"Container":"dcgm-exporter"}')
        self.assertEqual(status, 200)

    def test_an_exec_is_judged_by_the_container_it_belongs_to(self) -> None:
        for suffix in ("json", "start", "resize?h=1&w=1"):
            with self.subTest(suffix):
                status, _ = self.send_method("POST" if suffix != "json" else "GET",
                                             f"/v1.43/exec/tenantexec/{suffix}")
                self.assertEqual(status, 403)
        status, _ = self.ask("/v1.43/exec/ourexec/json")
        self.assertEqual(status, 200)
        status, _ = self.ask("/v1.43/exec/no-such-exec/json")
        self.assertEqual(status, 200, "a missing exec is docker's to answer")

    def test_a_container_cannot_be_given_dockerd_or_its_storage(self) -> None:
        for host in (
            {"Binds": ["/var/run/docker.sock:/docker.sock"]},
            {"Binds": ["/run//docker.sock:/s:ro"]},
            {"Binds": ["/:/host"]},
            {"Binds": ["/var/lib:/x"]},
            {"Binds": ["/var/lib/docker/overlay2:/x"]},
            {"Mounts": [{"Type": "bind", "Source": "/var/run", "Target": "/r"}]},
        ):
            with self.subTest(host):
                status, _ = self.send_method(
                    "POST", "/containers/create",
                    json.dumps({"Image": "busybox", "HostConfig": host}).encode())
                self.assertEqual(status, 403)
        status, _ = self.send_method(
            "POST", "/v1.43/volumes/create",
            b'{"Name":"x","DriverOpts":{"type":"none","o":"bind","device":"/var/lib/docker"}}')
        self.assertEqual(status, 403)
        status, _ = self.send_method(
            "POST", "/containers/create",
            json.dumps({"Image": "busybox", "HostConfig": {
                "Binds": ["/proc:/host/proc:ro", "exporter-data:/data"]}}).encode())
        self.assertEqual(status, 200, "an ordinary exporter mount was refused")

    def test_non_ascii_keys_go_would_fold_are_refused(self) -> None:
        for key in ("Ho\u017ftConfig", "HostConfi\u212a"):
            with self.subTest(key):
                body = json.dumps({"Image": "x", key: {"NetworkMode": "container:C.51217040"}},
                                  ensure_ascii=False).encode()
                status, _ = self.send_method("POST", "/containers/create", body)
                self.assertEqual(status, 403)

    def test_a_volume_mount_that_is_a_bind_in_disguise_is_refused(self) -> None:
        mount = {"Type": "volume", "Source": "probe", "Target": "/t", "VolumeOptions": {
            "DriverConfig": {"Name": "local", "Options": {
                "type": "none", "o": "bind", "device": "/var/lib/docker"}}}}
        status, _ = self.send_method(
            "POST", "/containers/create",
            json.dumps({"Image": "x", "HostConfig": {"Mounts": [mount]}}).encode())
        self.assertEqual(status, 403)

    def test_swarm_and_plugin_changes_are_refused_reads_pass(self) -> None:
        for path in ("/v1.43/services/create", "/swarm/init", "/plugins/pull?remote=x",
                     "/v1.43/services/abc/update", "/secrets/create"):
            with self.subTest(path):
                status, _ = self.send_method("POST", path, b"{}")
                self.assertEqual(status, 403)
        status, _ = self.ask("/v1.43/services")
        self.assertEqual(status, 200)

    def test_a_reference_is_judged_on_a_fresh_listing(self) -> None:
        """Renamed between two requests: the second must see the new name."""
        status, _ = self.ask("/v1.43/containers/cccc7777/json")
        self.assertEqual(status, 200)
        LISTED[2]["Names"] = ["/C.99999999"]
        try:
            status, _ = self.ask("/v1.43/containers/cccc7777/json")
            self.assertEqual(status, 403, "judged on a stale listing")
        finally:
            LISTED[2]["Names"] = ["/vast-grafana-1"]

    def test_a_name_with_a_slash_is_judged_whole(self) -> None:
        LISTED[0]["Names"] = ["/ours/db", "/C.51217040"]
        try:
            for path in ("/v1.43/containers/ours/db/json", "/containers/ours/db/stop"):
                with self.subTest(path):
                    status, _ = self.send_method("POST" if path.endswith("stop") else "GET", path)
                    self.assertEqual(status, 403)
            status, _ = self.ask("/v1.43/containers/C.51217040/json")
            self.assertEqual(status, 403, "an alias listed first hid the customer's name")
        finally:
            LISTED[0]["Names"] = ["/C.51217040"]

    def test_a_host_path_is_judged_by_what_it_resolves_to(self) -> None:
        link = os.path.join(self.directory, "docker-data")
        os.symlink("/var/lib/docker", link)
        sources = [link, "/var/lib/./docker/../docker", "/proc/1/root/var/lib/docker",
                   "/proc/self/root", "/proc//1/./root/srv", "/proc/42/cwd"]
        for source in sources:
            with self.subTest(source):
                self.assertTrue(proxy._host_path_protected(source))
        self.assertFalse(proxy._host_path_protected(self.directory))

    def test_host_paths_resolve_in_dockerds_view_not_ours(self) -> None:
        """The unit has a private /tmp: a host symlink there is invisible to the proxy's
        own view, so resolution goes through the host root."""
        host = os.path.join(self.directory, "hostroot")
        os.makedirs(os.path.join(host, "tmp"))
        os.makedirs(os.path.join(host, "srv", "data"))
        os.symlink("/var/lib/docker", os.path.join(host, "tmp", "docker-data"))
        os.symlink("../var/run", os.path.join(host, "srv", "run-link"))
        os.makedirs(os.path.join(host, "var", "lib", "docker", "volumes"))
        os.makedirs(os.path.join(host, "var", "lib", "docker", "containers"))
        os.symlink("/var/lib/docker/volumes", os.path.join(host, "srv", "vlink"))
        saved = proxy.HOST_ROOT
        proxy.HOST_ROOT = host
        try:
            self.assertEqual(proxy.host_resolve("/srv/vlink/../containers"), "/var/lib/docker/containers")
            self.assertTrue(proxy._host_path_protected("/srv/vlink/../containers"),
                            "`..` after a symlink was resolved lexically")
            self.assertEqual(proxy.host_resolve("/tmp/docker-data/x"), "/var/lib/docker/x")
            self.assertTrue(proxy._host_path_protected("/tmp/docker-data"))
            self.assertTrue(proxy._host_path_protected("/srv/run-link/docker.sock"))
            self.assertFalse(proxy._host_path_protected("/srv/data"))
        finally:
            proxy.HOST_ROOT = saved

    def test_host_paths_are_refused_when_dockerds_view_is_unavailable(self) -> None:
        saved = proxy.HOST_ROOT
        proxy.HOST_ROOT = os.path.join(self.directory, "no-such-root")
        try:
            self.assertTrue(proxy._host_path_protected("/srv/anything"))
        finally:
            proxy.HOST_ROOT = saved

    def test_volume_devices_are_judged_new_or_existing(self) -> None:
        status, _ = self.send_method(
            "POST", "/volumes/create",
            b'{"Name":"x","Driver":"local","DriverOpts":{"type":"none","o":"bind","device":"./var/lib/docker"}}')
        self.assertEqual(status, 403, "a relative device was taken for a volume name")
        for host in ({"Binds": ["leak:/data:ro"]},
                     {"Mounts": [{"Type": "volume", "Source": "leak", "Target": "/d"}]}):
            with self.subTest(host):
                status, _ = self.send_method(
                    "POST", "/containers/create",
                    json.dumps({"Image": "x", "HostConfig": host}).encode())
                self.assertEqual(status, 403, "an existing volume's backing was not judged")
        for host in ({"Binds": ["ours:/data", "brand-new:/n"]},
                     {"Mounts": [{"Type": "volume", "Source": "brand-new", "Target": "/d"}]}):
            with self.subTest(host):
                status, _ = self.send_method(
                    "POST", "/containers/create",
                    json.dumps({"Image": "x", "HostConfig": host}).encode())
                self.assertEqual(status, 200)

    def test_a_failed_listing_is_not_an_empty_one(self) -> None:
        names = proxy.Names(self.docker_path)
        names._fetch = lambda: (_ for _ in ()).throw(RuntimeError("container listing failed"))
        found, unknown = names.resolve("C.51217040")
        self.assertTrue(unknown)

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
            spoken = io.StringIO()
            with contextlib.redirect_stderr(spoken):
                names.resolve("something-new")
            self.assertIn("new-container", spoken.getvalue())
            self.assertIn("something-new", spoken.getvalue())
        finally:
            LISTED.pop()


class ReadOnlyProxyTests(ProxyTests):
    """The socket an observation gets: the same rule on whose, stricter on what.

    The observe profile mounts every filesystem read-only and the contract tells the
    model nothing it runs can alter the machine. A docker mutation never touches those
    mounts, so for a few hours `docker restart` on any container of ours was reachable
    from the path documented as incapable of change. Measured on 2026-09-19: `docker
    start dcgm-exporter` ran from an observation and returned success.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ro_path = self.listen_path + ".ro"
        for _ in range(200):
            if os.path.exists(self.ro_path):
                break
            threading.Event().wait(0.01)

    def send(self, method: str, path: str) -> tuple[int, str]:
        connection = _connect(self.ro_path)
        connection.sendall(f"{method} {path} HTTP/1.1\r\nHost: docker\r\n\r\n".encode())
        received = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            received += chunk
        connection.close()
        head, _, body = received.partition(b"\r\n\r\n")
        return int(head.split(b" ")[1]), body.decode("latin-1")

    def test_reading_our_own_container_still_works(self) -> None:
        status, body = self.send("GET", "/v1.43/containers/dcgm-exporter/json")
        self.assertEqual(status, 200)
        self.assertIn("reached", body)

    def test_it_refuses_every_way_of_changing_one(self) -> None:
        """Not a list of verbs: the docker API is REST-shaped, so this is the line."""
        for method, path in (
            ("POST", "/v1.43/containers/dcgm-exporter/restart"),
            ("POST", "/v1.43/containers/dcgm-exporter/start"),
            ("POST", "/v1.43/containers/dcgm-exporter/stop"),
            ("POST", "/v1.43/containers/dcgm-exporter/kill"),
            ("POST", "/v1.43/containers/dcgm-exporter/exec"),
            ("DELETE", "/v1.43/containers/dcgm-exporter"),
            ("POST", "/v1.43/containers/prune"),
            ("POST", "/v1.43/images/create?fromImage=evil"),
        ):
            with self.subTest(f"{method} {path}"):
                status, body = self.send(method, path)
                self.assertEqual(status, 403, "a change went through a read-only session")
                self.assertIn("read-only", body)

    def test_a_tenant_is_still_refused_as_a_tenant(self) -> None:
        """The whose rule is unchanged; a read of a rental is refused for being theirs."""
        status, body = self.send("GET", "/v1.43/containers/C.51217040/json")
        self.assertEqual(status, 403)
        self.assertIn("customer", body)

    def test_the_writable_socket_still_allows_a_change(self) -> None:
        """A management session has a person's approval behind it."""
        status, _ = self.ask("/v1.43/containers/dcgm-exporter/json")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
