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

**What it is and is not.** An observation gets the read-only socket, and there this is
a boundary: nothing but reads pass, and no read reaches a tenant's container. The
writable socket serves approved management sessions. There it refuses every way found
to reach a tenant's container or step around the proxy (the socket or docker's storage
bound into a new container), but a privileged container or a broad host mount still
reaches the machine as a whole; that stays a person's call when approving the plan.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import socket
import socketserver
import stat
import sys
import threading
import time
from typing import Iterable
from urllib.parse import parse_qs, quote, unquote_to_bytes

DOCKER_SOCKET = "/var/run/docker.sock"
LISTEN_SOCKET = "/run/terracompute-docker-proxy/docker.sock"
# What an observation gets: the same proxy, refusing anything that would change a
# container. The observe profile promises a machine nothing it runs can alter.
READ_ONLY_SOCKET = "/run/terracompute-docker-proxy/docker-ro.sock"
# Vast names every rental this way. Anchored, and digits only: `C.51217040` is a
# customer, `C.thing` and `myC.51217040` are not rentals and are not treated as such.
TENANT_NAME = re.compile(r"^/?C\.[0-9]+$")
# Where a container is named in the docker API. Everything else is forwarded without
# being understood, which is the point: this must not become a list of known verbs.
CONTAINER_PATH = re.compile(r"^/(?:v[0-9.]+/)?containers/([^/?]+)")
# Endpoints that name a container somewhere other than the path.
COMMIT_PATH = re.compile(r"^/(?:v[0-9.]+/)?commit$")
CREATE_PATH = re.compile(r"^/(?:v[0-9.]+/)?containers/create$")
PRUNE_PATH = re.compile(r"^/(?:v[0-9.]+/)?containers/prune$")
RENAME_PATH = re.compile(r"^/(?:v[0-9.]+/)?containers/(.+)/rename$")
SWARM_OR_PLUGIN_PATH = re.compile(
    r"^/(?:v[0-9.]+/)?(?:services|swarm|nodes|tasks|secrets|configs|plugins)(?:/|$)"
)
NETWORK_ATTACH_PATH = re.compile(r"^/(?:v[0-9.]+/)?networks/[^/]+/(?:connect|disconnect)$")
VOLUME_CREATE_PATH = re.compile(r"^/(?:v[0-9.]+/)?volumes/create$")
EXEC_PATH = re.compile(r"^/(?:v[0-9.]+/)?exec/([^/]+)(?:/|$)")
# Host paths a container must not be given. The socket is dockerd itself -- a
# container holding it never talks to this proxy again -- and the storage roots hold
# every tenant's filesystem. A bind of any ancestor exposes them just the same.
PROTECTED_HOST_PATHS = (
    "/var/run/docker.sock", "/run/docker.sock", "/var/run/docker", "/run/docker",
    "/var/lib/docker", "/var/lib/containerd", "/run/containerd", "/var/run/containerd",
)
HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
MAX_HEADER_BYTES = 64 * 1024
# A create request is judged on its body; one larger than this is refused unread.
MAX_CREATE_BODY_BYTES = 1024 * 1024


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _reply_body(reply: bytes) -> bytes:
    """The body of an HTTP reply, with chunked transfer framing removed."""
    head, _, body = reply.partition(b"\r\n\r\n")
    if b"transfer-encoding: chunked" not in head.lower():
        return body
    decoded = b""
    while True:
        size_line, crlf, body = body.partition(b"\r\n")
        digits = size_line.split(b";", 1)[0]
        if not crlf or not re.fullmatch(rb"[0-9A-Fa-f]{1,16}", digits):
            raise RuntimeError("malformed chunked reply")
        size = int(digits, 16)
        if size == 0:
            # Trailers, if any, end with an empty line; without it the reply is cut.
            if not (body.startswith(b"\r\n") or b"\r\n\r\n" in body):
                raise RuntimeError("truncated chunked reply")
            return decoded
        # A truncated chunk, or one not followed by CRLF, is not a complete reply;
        # reading it as one could make a partial listing look authoritative.
        if len(body) < size + 2 or body[size:size + 2] != b"\r\n":
            raise RuntimeError("truncated chunked reply")
        decoded, body = decoded + body[:size], body[size + 2:]


class Names:
    """What every container is called, by id and by name, refreshed from docker."""

    def __init__(self, docker_socket: str = DOCKER_SOCKET):
        self.docker_socket = docker_socket
        self._listing: list[tuple[str, tuple[str, ...]]] = []
        self._seen: set[str] = set()
        self._read_at = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> list[tuple[str, tuple[str, ...]]]:
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
        reply = b"".join(chunks)
        status = reply.split(b"\r\n", 1)[0].split(b" ")
        if len(status) < 2 or status[1] != b"200":
            # An error page is not a listing: "no containers" would let every
            # reference through as one that does not exist.
            raise RuntimeError("container listing failed")
        body = _reply_body(reply)
        start, end = body.find(b"["), body.rfind(b"]")
        if start < 0 or end <= start:
            raise RuntimeError("container listing unreadable")
        listed = json.loads(body[start : end + 1])
        if not isinstance(listed, list):
            raise RuntimeError("container listing unreadable")
        return [
            (str(item.get("Id") or ""),
             tuple(str(name).lstrip("/") for name in (item.get("Names") or [])))
            for item in listed
        ]

    def _candidates(self, reference: str) -> list[str]:
        """Every container docker could mean by this reference, by primary name.

        Docker resolves a reference as a full id, an exact name, or ANY unambiguous id
        prefix -- not only the 12-character short id. Judging only fixed spellings let
        an 11- or 14-character prefix of a customer's id straight through. Every
        container the reference could select is returned, so a reference that is
        ambiguous between ours and a customer's is judged as the customer's.
        """
        wanted = reference.lstrip("/")
        found: list[str] = []
        for identifier, names in self._listing:
            if wanted in names or (wanted and identifier.startswith(wanted)):
                # Every name, not the first: the listing does not put the container's
                # own name first, and a link alias listed ahead of `C.<digits>` made a
                # customer's container look like ours.
                found.extend(names or (identifier,))
        return found

    def exec_owner(self, exec_id: str) -> tuple[str | None, bool]:
        """The container an exec instance belongs to, and whether docker could not say.

        An exec id is created against a container (that request is judged), but the id
        is then used on its own: judging /exec/<id>/start by path alone let a known
        tenant exec through.
        """
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10)
        try:
            connection.connect(self.docker_socket)
            connection.sendall(
                # Percent-encoded: the id came from a request, and written raw a CRLF in
                # it would start a second, unjudged request on this connection.
                b"GET /exec/" + quote(exec_id, safe="").encode("ascii") + b"/json HTTP/1.1\r\n"
                b"Host: docker\r\nConnection: close\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception as error:
            _say(f'{{"proxy":"exec-lookup-failed","error":"{type(error).__name__}"}}')
            return None, True
        finally:
            connection.close()
        reply = b"".join(chunks)
        status = reply.split(b"\r\n", 1)[0].split(b" ")
        if len(status) > 1 and status[1] == b"404":
            return None, False  # No such exec: docker answers for itself.
        body = _reply_body(reply)
        start, end = body.find(b"{"), body.rfind(b"}")
        try:
            owner = json.loads(body[start : end + 1]).get("ContainerID") if start >= 0 else None
        except ValueError:
            owner = None
        if not owner:
            return None, True
        return str(owner), False

    def volume_device(self, name: str) -> tuple[str | None, bool]:
        """The host device an existing volume is backed by, and whether docker could not say."""
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(10)
        try:
            connection.connect(self.docker_socket)
            connection.sendall(
                b"GET /volumes/" + quote(name, safe="").encode("ascii") + b" HTTP/1.1\r\n"
                b"Host: docker\r\nConnection: close\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception as error:
            _say(f'{{"proxy":"volume-lookup-failed","error":"{type(error).__name__}"}}')
            return None, True
        finally:
            connection.close()
        reply = b"".join(chunks)
        status = reply.split(b"\r\n", 1)[0].split(b" ")
        if len(status) > 1 and status[1] == b"404":
            return None, False  # Created fresh by docker, backed by nothing of ours.
        if len(status) < 2 or status[1] != b"200":
            return None, True
        body = _reply_body(reply)
        start, end = body.find(b"{"), body.rfind(b"}")
        try:
            options = json.loads(body[start : end + 1]).get("Options") or {}
        except (ValueError, AttributeError):
            return None, True
        device = str(options.get("device") or "") if isinstance(options, dict) else ""
        return (device or None), False

    def resolve(self, reference: str) -> tuple[list[str], bool]:
        """The containers this reference could select, and whether docker could not say.

        A reference docker does not know is not a customer's -- it does not exist -- so
        it is allowed through and docker answers for itself.
        """
        with self._lock:
            # Read fresh for every judgment. A cached listing let a reference be judged
            # by what it used to name, and the listing is one small request.
            try:
                self._listing = self._fetch()
                self._read_at = time.monotonic()
            except Exception as error:  # A broken lookup must not open the door.
                _say(f'{{"proxy":"name-lookup-failed","error":"{type(error).__name__}"}}')
                return [], True
            found = self._candidates(reference)
            self._note_all()
            return found, False

    def _note_all(self) -> None:
        """Say when a container APPEARS that is not a rental. Nothing is blocked by it.

        The rule above rests on Vast naming rentals `C.<digits>`. If that ever changes
        the denial stops matching and nothing else would say so, so something has to.

        The first reading is the baseline, taken whole. Deciding that per name means
        everything after the first one counts as new, and it reports all seven of our
        own containers on startup -- true, useless, and exactly the noise a real signal
        gets lost in.
        """
        names = {names[0] if names else ident for ident, names in self._listing}
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


# What a docker client may do without changing anything. The docker API is REST-shaped,
# so this is the read/write line itself rather than a list of verbs: inspect, logs, ps,
# stats, events, diff, top and every future read are GET; start, stop, restart, kill,
# exec, commit, prune and rm are POST or DELETE.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def canonical_target(target: str) -> tuple[str, str] | None:
    """The path dockerd will route on, and the query; None if it cannot be known.

    dockerd matches the percent-DECODED, CLEANED path (duplicate slashes collapsed, `.`
    and `..` resolved). Judging the raw text let `/containers/C%2E51217040/json`,
    `//containers/C.51217040/logs` and `/v1.43/./containers/...` past a rule that
    dockerd then routed to the customer's container. Anything that is not an ordinary
    origin-form target -- an absolute URL, `*`, undecodable bytes, control characters --
    cannot be judged the way dockerd will read it, so it is refused rather than guessed.
    """
    if not target.startswith("/"):
        return None
    path, _, query = target.partition("?")
    path = path.partition("#")[0]
    try:
        decoded = unquote_to_bytes(path).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        return None
    cleaned = posixpath.normpath(re.sub(r"/+", "/", decoded))
    return ("/" if cleaned in ("", ".") else cleaned), query


def _tenant_reason(references: Iterable[str], names: Names) -> str | None:
    for reference in references:
        if TENANT_NAME.match(reference):
            # By its spelling alone, whatever the listing says: a rental that does not
            # exist yet may exist by the time docker acts on the request.
            return _tenant_text(reference)
        found, unknown = names.resolve(reference)
        if unknown:
            return "the proxy could not check whose container this is"
        tenant = next((name for name in found if TENANT_NAME.match(name)), None)
        if tenant is not None:
            return _tenant_text(tenant)
    return None


def _tenant_text(name: str) -> str:
    return (
        f"{name.lstrip('/')} is a customer's container. This agent does not read or "
        "change tenant containers. If the evidence genuinely requires it, stop and ask "
        "a person."
    )


class _Ambiguous(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object keyed by lower-cased name, refusing any key Go would merge.

    dockerd decodes into Go structs: field names match case-insensitively and a
    repeated key is merged into the same field. Python keeps the last duplicate and
    matches case exactly, so `hostconfig` or a second `HostConfig` hid what docker
    would act on. A body with two keys that differ only by case is refused.
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if not key.isascii():
            # Go folds some non-ASCII letters onto ASCII ones (`ſ` to `s`, the Kelvin
            # sign to `k`), so `HoſtConfig` is HostConfig to docker. No docker field
            # has a non-ASCII name; refusing them is simpler than folding like Go.
            raise _Ambiguous(key)
        folded = key.lower()
        if folded in result:
            raise _Ambiguous(key)
        result[folded] = value
    return result


def _strict_json(body: bytes) -> dict[str, object] | None:
    try:
        parsed = json.loads(body or b"{}", object_pairs_hook=_strict_object)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# Where dockerd's view of the filesystem is reachable from here. The proxy's unit runs
# with a private /tmp and without /home, so its own view differs from dockerd's; the
# host root through pid 1 does not. If it cannot be read, host paths are refused: our
# own view can hide exactly the symlink dockerd will follow. ("" means "this process's
# view is dockerd's", which only a test sets.)
HOST_ROOT = "/proc/1/root"


def _host_view_root() -> str | None:
    if HOST_ROOT == "":
        return ""
    return HOST_ROOT if os.access(HOST_ROOT, os.R_OK | os.X_OK) else None


def host_resolve(path: str) -> str | None:
    """`path` resolved the way dockerd would see it; None if that cannot be established.

    Walks component by component through the host root, following symlinks with an
    absolute target back to that root. A component that does not exist ends the walk
    (nothing below it can be a link); any other failure is None, and None is refused.
    """
    root = _host_view_root()
    if root is None:
        return None
    queue = [part for part in path.split("/") if part and part != "."]
    resolved: list[str] = []
    followed = 0
    while queue:
        part = queue.pop(0)
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = "/" + "/".join(resolved + [part])
        try:
            mode = os.lstat(root + candidate).st_mode
        except FileNotFoundError:
            resolved.append(part)
            resolved.extend(p for p in queue if p not in ("", "."))
            break
        except OSError:
            return None
        if stat.S_ISLNK(mode):
            followed += 1
            if followed > 40:
                return None
            try:
                target = os.readlink(root + candidate)
            except OSError:
                return None
            if target.startswith("/"):
                resolved = []
            queue = [p for p in target.split("/") if p and p != "."] + queue
        else:
            resolved.append(part)
    return "/" + "/".join(resolved)


def _host_path_protected(source: str) -> bool:
    """Whether dockerd would mount something that exposes itself or customer data.

    Judged on both the spelling and the object it resolves to in dockerd's view: a
    symlink such as `/tmp/docker-data -> /var/lib/docker`, or
    `/proc/1/root/var/lib/docker`, names protected storage without spelling it.
    """
    if not source.startswith("/"):
        return True  # Callers pass only host paths; a relative one cannot be judged.
    spelled = posixpath.normpath(re.sub(r"/+", "/", source))
    if re.match(r"^/proc/[^/]+/(?:root|cwd)(?:/|$)", spelled):
        # A process's root or cwd is a doorway to its whole filesystem.
        return True
    # Resolved from BOTH spellings. dockerd's bind parser cleans the source lexically
    # before the kernel resolves it; a local-volume device goes to mount(2) as written,
    # where `..` after a symlink is the link target's parent. Either may be the one used.
    resolved_each = [host_resolve(spelled), host_resolve(re.sub(r"/+", "/", source))]
    if None in resolved_each:
        return True
    protected_paths = set(PROTECTED_HOST_PATHS)
    # Both sides resolved: `/var/run` is itself a symlink to `/run` on most hosts.
    protected_paths |= {host_resolve(path) or path for path in PROTECTED_HOST_PATHS}
    for path in (spelled, *resolved_each):
        for protected in protected_paths:
            if path == "/" or protected == path or protected.startswith(path + "/") \
                    or path.startswith(protected + "/"):
                return True
    return False


def create_references(body: bytes) -> tuple[list[str], list[str], list[str]] | None:
    """What a create request would attach to: other containers, and host paths.

    `--volumes-from`, `--network/--pid/--ipc container:<ref>` and `--link` make the new
    container reach into another one without its name ever appearing in the path;
    binds and mounts give it host paths. None if the body cannot be read the way
    docker will read it.
    """
    spec = _strict_json(body)
    if spec is None:
        return None
    host = spec.get("hostconfig") or {}
    if not isinstance(host, dict):
        return None
    references: list[str] = []
    for entry in host.get("volumesfrom") or []:
        references.append(str(entry).split(":", 1)[0])
    for mode in ("networkmode", "pidmode", "ipcmode", "utsmode", "cgroupnsmode"):
        value = str(host.get(mode) or "")
        if value.startswith("container:"):
            references.append(value[len("container:"):])
    for link in host.get("links") or []:
        references.append(str(link).split(":", 1)[0])
    sources: list[str] = []
    volumes: list[str] = []
    for bind in host.get("binds") or []:
        source = str(bind).split(":", 1)[0]
        (sources if source.startswith("/") else volumes).append(source)
    for mount in host.get("mounts") or []:
        if not isinstance(mount, dict):
            return None
        kind = str(mount.get("type") or "bind").lower()
        if kind == "bind":
            sources.append(str(mount.get("source") or ""))
        elif kind == "volume":
            # A local volume can be a bind in disguise (type=none, o=bind,
            # device=<host path>), either inline here or already configured on an
            # existing volume that this mount only names.
            options = ((mount.get("volumeoptions") or {}).get("driverconfig") or {}).get("options") \
                if isinstance(mount.get("volumeoptions") or {}, dict) else None
            if isinstance(options, dict):
                if options.get("device"):
                    sources.append(str(options.get("device")))
            elif options is not None:
                return None
            if mount.get("source"):
                volumes.append(str(mount.get("source")))
    return [reference for reference in references if reference], sources, volumes


def _protected_reason(sources: list[str]) -> str | None:
    exposed = [source for source in sources if _host_path_protected(source)]
    if exposed:
        return (
            f"a container given {exposed[0]} could reach dockerd or every customer's "
            "filesystem around this proxy. Mount something narrower."
        )
    return None


def judge(request_line: bytes, names: Names, *, read_only: bool = False) -> str | None:
    """The reason to refuse this request, or None to pass it through untouched.

    The body of a create or network request is judged separately (`judge_body`), because only
    the handler can read it.
    """
    try:
        method, target, _version = request_line.decode("latin-1").split(" ", 2)
    except ValueError:
        return None  # Not something we understand; docker can reject it itself.
    if read_only and method.upper() not in READ_METHODS:
        # The observe profile mounts every filesystem read-only and tells the model
        # that nothing it runs can alter the machine. A docker mutation never touches
        # those mounts -- it is a socket to a daemon outside the sandbox -- so for the
        # few hours between this proxy going live and this check, `docker restart` on
        # any container of ours was reachable from the path documented as incapable of
        # change. Measured, not reasoned about: `docker start dcgm-exporter` ran from
        # an observation on 2026-09-19 and returned success.
        return (
            "this is a read-only session, so it cannot change a container. Look all you "
            "like. If the evidence says something must be restarted or replaced, say so "
            "in your finding and let a person decide."
        )
    canonical = canonical_target(target)
    if canonical is None:
        return "the proxy cannot tell which container this request names, so it refuses it"
    path, query = canonical
    # `container:<ref>` in any parameter joins that container's namespaces -- the legacy
    # builder's networkmode, for one -- whatever the endpoint.
    joined = [value[len("container:"):] for values in parse_qs(query).values()
              for value in values if value.lower().startswith("container:")]
    if joined:
        reason = _tenant_reason(joined, names)
        if reason is not None:
            return reason
    if method.upper() not in READ_METHODS and SWARM_OR_PLUGIN_PATH.match(path):
        # A swarm service or task starts containers with whatever mounts it asks for,
        # and a plugin runs with the privileges it requests, both without the create
        # request this proxy judges.
        return ("swarm services and plugins can start containers around this proxy, "
                "so changing them is refused. Ask a person.")
    if PRUNE_PATH.match(path):
        # Removes every stopped container, customers' included.
        return ("pruning containers would remove customers' stopped containers too. "
                "Remove our own containers by name instead.")
    if COMMIT_PATH.match(path):
        return _tenant_reason(parse_qs(query).get("container", []), names)
    if CREATE_PATH.match(path) or RENAME_PATH.match(path):
        # Giving one of ours a rental's name would put it on the customers' side of
        # this very rule, and put a fake rental on the machine.
        for name in parse_qs(query).get("name", []):
            if TENANT_NAME.match(name.lstrip("/")):
                return "a container of ours may not take a rental's name"
    if RENAME_PATH.match(path):
        return _tenant_reason([RENAME_PATH.match(path).group(1)], names)
    if CREATE_PATH.match(path) or NETWORK_ATTACH_PATH.match(path) or VOLUME_CREATE_PATH.match(path):
        return None  # Judged on its body.
    exec_match = EXEC_PATH.match(path)
    if exec_match is not None:
        owner, unknown = names.exec_owner(exec_match.group(1))
        if unknown:
            return "the proxy could not check whose container this exec belongs to"
        return _tenant_reason([owner], names) if owner else None
    match = CONTAINER_PATH.match(path)
    if match is None:
        return None
    # dockerd routes /containers/{name:.*}/<action>: a name may contain `/` (a legacy
    # link such as `ours/db`), so the reference is not just the next segment. Every
    # leading run of segments is a reference docker could mean; judge them all.
    rest = path[match.start(1):].split("/")
    return _tenant_reason(["/".join(rest[:n]) for n in range(1, len(rest) + 1)], names)


def body_judged(request_line: bytes) -> bool:
    """Whether this request is judged on its body, which only the handler can read."""
    try:
        method, target, _version = request_line.decode("latin-1").split(" ", 2)
    except ValueError:
        return False
    canonical = canonical_target(target)
    if method.upper() != "POST" or canonical is None:
        return False
    path = canonical[0]
    return bool(CREATE_PATH.match(path) or NETWORK_ATTACH_PATH.match(path)
                or VOLUME_CREATE_PATH.match(path))


def judge_body(request_line: bytes, body: bytes | None, names: Names) -> str | None:
    if body is None:
        return "the proxy could not read this request whole, so it refuses it"
    target = request_line.decode("latin-1").split(" ", 2)[1]
    path = (canonical_target(target) or ("", ""))[0]
    if NETWORK_ATTACH_PATH.match(path):
        spec = _strict_json(body)
        if spec is None:
            return "the proxy could not read this request, so it refuses it"
        container = str(spec.get("container") or "")
        return _tenant_reason([container], names) if container else None
    if VOLUME_CREATE_PATH.match(path):
        spec = _strict_json(body)
        if spec is None:
            return "the proxy could not read this request, so it refuses it"
        options = spec.get("driveropts") or {}
        if not isinstance(options, dict):
            return "the proxy could not read this request, so it refuses it"
        device = str(options.get("device") or "")
        return _protected_reason([device]) if device else None
    found = create_references(body)
    if found is None:
        return "the proxy could not read this create request, so it refuses it"
    references, sources, volumes = found
    for volume in volumes:
        device, unknown = names.volume_device(volume)
        if unknown:
            return "the proxy could not check what this volume is backed by"
        if device:
            sources.append(device)
    return _tenant_reason(references, names) or _protected_reason(sources)


class FramingError(ValueError):
    pass


def parse_head(block: bytes) -> tuple[bytes, list[tuple[bytes, bytes]]]:
    """The request line and headers, read exactly as Go's HTTP server reads them.

    Go also accepts a bare LF as a line end. Splitting only on CRLF let a bare-LF
    request end its headers early -- the proxy judged one request while dockerd read
    a second, unjudged one after it (the `Connection: close` inserted by the proxy
    landing in the second), and a `Content-Length` could be hidden from the body
    check. Any bare CR or LF, folded header, malformed name, or conflicting framing
    header is refused: the proxy only forwards what it and dockerd read the same way.
    """
    if re.search(rb"(?<!\r)\n|\r(?!\n)", block):
        raise FramingError("bare CR or LF")
    lines = block.split(b"\r\n")
    request_line, header_lines = lines[0], lines[1:]
    parts = request_line.split(b" ")
    if len(parts) != 3 or parts[2] not in (b"HTTP/1.0", b"HTTP/1.1") \
            or not HEADER_NAME.match(parts[0]):
        raise FramingError("request line")
    headers: list[tuple[bytes, bytes]] = []
    for line in header_lines:
        if line[:1] in (b" ", b"\t"):
            raise FramingError("folded header")
        name, colon, value = line.partition(b":")
        if not colon or not HEADER_NAME.match(name):
            raise FramingError("malformed header")
        if re.search(rb"[^\t\x20-\x7e]", value):
            # Go's parsers treat some non-ASCII bytes as whitespace (a UTF-8 no-break
            # space in Content-Type, for one); byte-level comparison here does not.
            # docker's client sends only ASCII headers, so nothing else is accepted.
            raise FramingError("non-ASCII header value")
        headers.append((name.lower(), value.strip(b" \t")))
    lengths = [value for name, value in headers if name == b"content-length"]
    encodings = [value for name, value in headers if name == b"transfer-encoding"]
    if len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings):
        raise FramingError("conflicting framing headers")
    if lengths and not lengths[0].isdigit():
        raise FramingError("content-length")
    return request_line, headers


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
    read_only: bool = False

    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head and len(head) < MAX_HEADER_BYTES:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        block = head.partition(b"\r\n\r\n")[0]
        try:
            request_line, headers = parse_head(block)
        except FramingError as error:
            _say(f'{{"proxy":"denied-framing","why":"{error}"}}')
            self.request.sendall(denied(
                "the proxy could not frame this request the way docker would, so it "
                "refuses it"))
            return
        if b"\r\n\r\n" not in head:
            self.request.sendall(denied("request headers too large"))
            return
        if any(name == b"content-type" and b"x-www-form-urlencoded" in value.lower()
               for name, value in headers):
            # dockerd reads parameters with ParseForm, which merges a urlencoded body
            # into the query: `name=C.42` in the body renames one of ours onto a rental
            # name while the query this proxy judges says nothing. docker's own client
            # never sends a form body, so none is let through.
            self.request.sendall(denied(
                "form-encoded bodies are not accepted; pass parameters in the query"))
            return
        reason = judge(request_line, self.names, read_only=self.read_only)
        if reason is None and body_judged(request_line):
            head, body = self._whole_body(head, headers)
            reason = judge_body(request_line, body, self.names)
        if reason is not None:
            _say(f'{{"proxy":"denied","request":"{request_line.decode("latin-1")[:120]}"}}')
            self.request.sendall(denied(reason))
            return
        headers, _, rest = head.partition(b"\r\n\r\n")
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # The daemon that was consulted to judge the request is the one it goes to.
            upstream.connect(self.names.docker_socket)
            upstream.sendall(_headers_without_keepalive(headers) + rest)
            self._splice(upstream)
        except Exception as error:
            _say(f'{{"proxy":"upstream-failed","error":"{type(error).__name__}"}}')
        finally:
            upstream.close()

    def _whole_body(
        self, head: bytes, parsed: list[tuple[bytes, bytes]],
    ) -> tuple[bytes, bytes | None]:
        """Read a Content-Length body whole; None when it cannot be judged whole."""
        headers, _, rest = head.partition(b"\r\n\r\n")
        if any(name == b"transfer-encoding" for name, _value in parsed):
            return head, None
        length = next((int(value) for name, value in parsed if name == b"content-length"), None)
        if length is None:
            return head, b""
        if length > MAX_CREATE_BODY_BYTES:
            return head, None
        while len(rest) < length:
            chunk = self.request.recv(65536)
            if not chunk:
                return head, None
            rest += chunk
        return headers + b"\r\n\r\n" + rest, rest[:length]

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


class ReadOnlyHandler(Handler):
    """The same proxy, refusing anything that would change a container."""

    read_only = True


def _listener(path: str, handler: type[Handler]) -> Server:
    if os.path.exists(path):
        os.unlink(path)
    server = Server(path, handler)
    # Root talks to it, and so does anything in a session, which runs as root too.
    os.chmod(path, 0o600)
    _say(f'{{"proxy":"listening","socket":"{path}","read_only":{str(handler.read_only).lower()}}}')
    return server


def serve(
    listen: str = LISTEN_SOCKET,
    docker_socket: str = DOCKER_SOCKET,
    read_only_listen: str = READ_ONLY_SOCKET,
) -> None:
    """Two sockets over one dockerd, because the two profiles promise different things.

    An observation is told the machine cannot be changed by anything it runs, and that
    has to be true of docker as well -- a mutation there never touches the read-only
    mounts, so nothing else was enforcing it. A management session has a person's
    approval behind it and gets the full socket.

    Whose containers is the same rule on both; what may be done to them is not.
    """
    Handler.names = Names(docker_socket)
    writable = _listener(listen, Handler)
    readable = _listener(read_only_listen, ReadOnlyHandler)
    threading.Thread(target=readable.serve_forever, daemon=True).start()
    writable.serve_forever()


def main(argv: Iterable[str] = ()) -> int:
    arguments = list(argv)
    serve(*(arguments[:3] or [LISTEN_SOCKET]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
