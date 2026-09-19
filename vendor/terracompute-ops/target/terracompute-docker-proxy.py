#!/usr/bin/env python3
"""A docker socket that refuses to touch a customer's container, and passes the rest.

The agent needs docker. Its own monitoring lives there -- what image an exporter runs,
what environment it was given, why it reopens a GPU -- and the answer to a recurring
fault is usually in a field nobody thought to expose in advance. Handing it the real
socket hands it every tenant's container as well, because `docker exec` and `docker cp`
reach through dockerd into filesystems the session's mounts have walled off.

So: not a list of what the agent may do. One rule about whose containers it may not
touch, and everything else goes through untouched -- every subcommand, every field,
every container that does not exist yet, including the monitoring stack that replaces
the current one.

**Names are not enough.** A container can be addressed by id as easily as by name, so a
denial that only reads names is bypassed by `docker ps -q` followed by the id. Every
reference is resolved to a name through docker itself before it is judged.

**Vast assigns the name, not the tenant.** Rentals are `C.<digits>`, which is what Vast's
own host tooling filters on. That matters more than it looks: labels and image names come
from the tenant's own image and could be forged to look like ours, but the tenant does
not choose their container's name. A boundary has to rest on something the other side
cannot write.

The match is anchored. Docker's own `--filter name=C.` is an unanchored substring and
would match `notC.mine`; this does not.
"""

from __future__ import annotations

import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
from typing import Iterable

DOCKER_SOCKET = "/var/run/docker.sock"
LISTEN_SOCKET = "/run/terracompute-docker-proxy/docker.sock"
# Vast names every rental this way. Anchored, and digits only: `C.51217040` is a
# customer, `C.thing` and `myC.51217040` are not rentals and are not treated as such.
TENANT_NAME = re.compile(r"^/?C\.[0-9]+$")
# Where a container is named in the docker API. Everything else is forwarded without
# being understood, which is the point: this must not become a list of known verbs.
CONTAINER_PATH = re.compile(r"^/(?:v[0-9.]+/)?containers/([^/?]+)")
MAX_HEADER_BYTES = 64 * 1024
NAME_CACHE_SECONDS = 10.0


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Names:
    """What every container is called, by id and by name, refreshed from docker."""

    def __init__(self, docker_socket: str = DOCKER_SOCKET):
        self.docker_socket = docker_socket
        self._by_reference: dict[str, str] = {}
        self._seen: set[str] = set()
        self._read_at = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> dict[str, str]:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10)
        connection.connect(self.docker_socket)
        try:
            connection.sendall(
                b"GET /containers/json?all=1 HTTP/1.1\r\nHost: docker\r\n"
                b"Connection: close\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            connection.close()
        body = b"".join(chunks).split(b"\r\n\r\n", 1)[-1]
        # The reply may be chunked; the container list is the only JSON array in it.
        start, end = body.find(b"["), body.rfind(b"]")
        if start < 0 or end <= start:
            return {}
        listed = json.loads(body[start : end + 1])
        mapping: dict[str, str] = {}
        for item in listed:
            names = [str(name) for name in (item.get("Names") or [])]
            identifier = str(item.get("Id") or "")
            primary = names[0] if names else identifier
            for name in names:
                mapping[name.lstrip("/")] = primary
            if identifier:
                mapping[identifier] = primary
                mapping[identifier[:12]] = primary
        return mapping

    def resolve(self, reference: str) -> tuple[str, bool]:
        """The container's name, and whether we had to guess because docker did not say.

        A reference docker does not know is not a customer's -- it does not exist -- so
        it is allowed through and docker answers for itself.
        """
        with self._lock:
            stale = time.monotonic() - self._read_at > NAME_CACHE_SECONDS
            if reference not in self._by_reference or stale:
                try:
                    self._by_reference = self._fetch()
                    self._read_at = time.monotonic()
                except Exception as error:  # A broken lookup must not open the door.
                    _say(f'{{"proxy":"name-lookup-failed","error":"{type(error).__name__}"}}')
                    return reference, True
            name = self._by_reference.get(reference, reference)
            self._note_all()
            return name, False

    def _note_all(self) -> None:
        """Say when a container APPEARS that is not a rental. Nothing is blocked by it.

        The rule above rests on Vast naming rentals `C.<digits>`. If that ever changes
        the denial stops matching and nothing else would say so, so something has to.

        The first reading is the baseline, taken whole. Deciding that per name means
        everything after the first one counts as new, and it reports all seven of our
        own containers on startup -- true, useless, and exactly the noise a real signal
        gets lost in.
        """
        names = {name.lstrip("/") for name in self._by_reference.values()}
        baseline = not self._seen
        appeared = names - self._seen
        self._seen |= names
        if baseline:
            return
        for name in sorted(appeared):
            if not TENANT_NAME.match(name):
                _say(f'{{"proxy":"new-container","name":"{name[:64]}"}}')


def denied(reason: str) -> bytes:
    body = json.dumps({"message": reason}).encode()
    return (
        b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )


def judge(request_line: bytes, names: Names) -> str | None:
    """The reason to refuse this request, or None to pass it through untouched."""
    try:
        _method, path, _version = request_line.decode("latin-1").split(" ", 2)
    except ValueError:
        return None  # Not something we understand; docker can reject it itself.
    match = CONTAINER_PATH.match(path)
    if match is None:
        return None
    reference = match.group(1)
    name, unknown = names.resolve(reference)
    if unknown:
        return "the proxy could not check whose container this is"
    if TENANT_NAME.match(name):
        return (
            f"{name.lstrip('/')} is a customer's container. This agent does not read or "
            "change tenant containers. If the evidence genuinely requires it, stop and "
            "ask a person."
        )
    return None


def _headers_without_keepalive(raw: bytes) -> bytes:
    """Forward one request per connection, so a second cannot ride in unjudged."""
    lines = [
        line for line in raw.split(b"\r\n")
        if not line.lower().startswith(b"connection:")
    ]
    while lines and lines[-1] == b"":
        lines.pop()
    return b"\r\n".join(lines) + b"\r\nConnection: close\r\n\r\n"


class Handler(socketserver.BaseRequestHandler):
    names: Names

    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head and len(head) < MAX_HEADER_BYTES:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        request_line = head.split(b"\r\n", 1)[0]
        reason = judge(request_line, self.names)
        if reason is not None:
            _say(f'{{"proxy":"denied","request":"{request_line.decode("latin-1")[:120]}"}}')
            self.request.sendall(denied(reason))
            return
        headers, _, rest = head.partition(b"\r\n\r\n")
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(DOCKER_SOCKET)
            upstream.sendall(_headers_without_keepalive(headers) + rest)
            self._splice(upstream)
        except Exception as error:
            _say(f'{{"proxy":"upstream-failed","error":"{type(error).__name__}"}}')
        finally:
            upstream.close()

    def _splice(self, upstream: socket.socket) -> None:
        def pump(source: socket.socket, sink: socket.socket) -> None:
            try:
                while True:
                    chunk = source.recv(65536)
                    if not chunk:
                        break
                    sink.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    sink.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        forward = threading.Thread(target=pump, args=(self.request, upstream), daemon=True)
        forward.start()
        pump(upstream, self.request)
        forward.join(timeout=5)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(listen: str = LISTEN_SOCKET, docker_socket: str = DOCKER_SOCKET) -> None:
    Handler.names = Names(docker_socket)
    if os.path.exists(listen):
        os.unlink(listen)
    server = Server(listen, Handler)
    # Root talks to it, and so does anything in a session, which runs as root too.
    os.chmod(listen, 0o600)
    _say(f'{{"proxy":"listening","socket":"{listen}"}}')
    server.serve_forever()


def main(argv: Iterable[str] = ()) -> int:
    arguments = list(argv)
    serve(*(arguments[:2] or [LISTEN_SOCKET]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
