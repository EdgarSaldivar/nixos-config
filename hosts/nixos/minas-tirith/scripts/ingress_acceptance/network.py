"""Network probes and observation collection for ingress acceptance."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import socket
import ssl
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .certificate import decode_certificate_identity
from .models import (
    Baseline,
    Observations,
    RedirectObservation,
    ResolutionObservation,
    StatusObservation,
)


def parse_target(value: str) -> Tuple[str, int]:
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0 or closing + 1 >= len(value) or value[closing + 1] != ":":
            raise argparse.ArgumentTypeError("target must be HOST:PORT")
        host, port_text = value[1:closing], value[closing + 2 :]
    else:
        if ":" not in value:
            raise argparse.ArgumentTypeError("target must be HOST:PORT")
        host, port_text = value.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target port must be an integer") from exc
    if not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("target must contain a host and port 1..65535")
    return host, port


def connection_target(
    fixed_target: Optional[Tuple[str, int]], hostname: str, default_port: int
) -> Tuple[str, int]:
    return fixed_target if fixed_target is not None else (hostname, default_port)


def resolve_hostname(hostname: str, port: int) -> ResolutionObservation:
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return ResolutionObservation(error=f"{type(exc).__name__}: {exc}")
    addresses = tuple(dict.fromkeys(answer[4][0] for answer in answers))
    return ResolutionObservation(addresses=addresses)


def _request_bytes(hostname: str) -> bytes:
    ascii_hostname = hostname.encode("idna").decode("ascii")
    return (
        f"GET / HTTP/1.1\r\nHost: {ascii_hostname}\r\n"
        "User-Agent: ingress-acceptance/1\r\nAccept: */*\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")


def probe_https_get(
    target: Tuple[str, int], hostname: str, context: ssl.SSLContext, timeout: float
) -> Tuple[StatusObservation, Optional[bytes]]:
    raw_socket: Optional[socket.socket] = None
    tls_socket: Optional[ssl.SSLSocket] = None
    certificate: Optional[bytes] = None
    try:
        raw_socket = socket.create_connection(target, timeout=timeout)
        raw_socket.settimeout(timeout)
        tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
        tls_socket.settimeout(timeout)
        certificate = tls_socket.getpeercert(binary_form=True)
        tls_socket.sendall(_request_bytes(hostname))
        response = http.client.HTTPResponse(tls_socket, method="GET")
        response.begin()
        status = response.status
        location = response.getheader("Location")
        response.close()
        return StatusObservation(status=status, location=location), certificate
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError) as exc:
        return StatusObservation(error=f"{type(exc).__name__}: {exc}"), certificate
    finally:
        if tls_socket is not None:
            tls_socket.close()
        elif raw_socket is not None:
            raw_socket.close()


def probe_tls_certificate(
    target: Tuple[str, int], hostname: str, context: ssl.SSLContext, timeout: float
) -> Tuple[Optional[bytes], Optional[str]]:
    raw_socket: Optional[socket.socket] = None
    tls_socket: Optional[ssl.SSLSocket] = None
    try:
        raw_socket = socket.create_connection(target, timeout=timeout)
        raw_socket.settimeout(timeout)
        tls_socket = context.wrap_socket(raw_socket, server_hostname=hostname)
        tls_socket.settimeout(timeout)
        return tls_socket.getpeercert(binary_form=True), None
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if tls_socket is not None:
            tls_socket.close()
        elif raw_socket is not None:
            raw_socket.close()


def probe_http_redirect(
    target: Tuple[str, int], hostname: str, timeout: float
) -> RedirectObservation:
    plain_socket: Optional[socket.socket] = None
    try:
        plain_socket = socket.create_connection(target, timeout=timeout)
        plain_socket.settimeout(timeout)
        plain_socket.sendall(_request_bytes(hostname))
        response = http.client.HTTPResponse(plain_socket, method="GET")
        response.begin()
        result = RedirectObservation(
            status=response.status, location=response.getheader("Location")
        )
        response.close()
        return result
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        return RedirectObservation(error=f"{type(exc).__name__}: {exc}")
    finally:
        if plain_socket is not None:
            plain_socket.close()


def read_access_log(path: Path, hostnames: Iterable[str]) -> Tuple[Dict[str, str], List[str]]:
    wanted = {hostname.lower().rstrip(".") for hostname in hostnames}
    routers: Dict[str, str] = {}
    errors: List[str] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"access log line {line_number}: invalid JSON: {exc.msg}")
                    continue
                if not isinstance(entry, dict):
                    errors.append(f"access log line {line_number}: JSON value is not an object")
                    continue
                request_host = entry.get("RequestHost")
                if not isinstance(request_host, str):
                    continue
                normalized_host = request_host.rsplit(":", 1)[0].lower().rstrip(".")
                if normalized_host not in wanted:
                    continue
                router_name = entry.get("RouterName")
                routers[normalized_host] = (
                    router_name if isinstance(router_name, str) else "<missing RouterName>"
                )
    except OSError as exc:
        errors.append(f"cannot read access log {path}: {exc}")
    return routers, errors


def collect_observations(
    baseline: Baseline,
    target: Optional[Tuple[str, int]],
    http_target: Optional[Tuple[str, int]],
    access_log: Optional[Path],
    insecure: bool,
    timeout: float,
    public_dns: bool = False,
) -> Observations:
    observations = Observations()
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    certificate_der: Dict[str, bytes] = {}

    if public_dns:
        # Keep this separate from socket.create_connection so even an expected
        # 000ERR route must retain public DNS. Otherwise deleting dungeon's DNS
        # record would look exactly like its intentionally dead backend.
        for hostname in baseline.probe_hostnames():
            observations.resolutions[hostname] = resolve_hostname(hostname, 443)

    # One loop and no retries guarantees exactly one HTTPS GET per status hostname.
    for hostname in baseline.statuses:
        status, certificate = probe_https_get(
            connection_target(target, hostname, 443), hostname, context, timeout
        )
        observations.statuses[hostname] = status
        if certificate:
            certificate_der[hostname] = certificate

    # Status hosts are never retried: in particular, an expected 000ERR may consume
    # one timeout during its sole GET, but it must never consume a second timeout.
    for hostname in baseline.tls_only_hostnames():
        certificate, error = probe_tls_certificate(
            connection_target(target, hostname, 443), hostname, context, timeout
        )
        if certificate:
            certificate_der[hostname] = certificate
        elif error:
            observations.collection_errors.append(f"certificate probe {hostname}: {error}")

    for hostname, certificate in certificate_der.items():
        observations.fingerprints[hostname] = hashlib.sha256(certificate).hexdigest()
    for hostname in baseline.certificates:
        certificate = certificate_der.get(hostname)
        if certificate is None:
            continue
        try:
            observations.certificates[hostname] = decode_certificate_identity(certificate)
        except (ValueError, UnicodeError) as exc:
            observations.collection_errors.append(
                f"cannot decode certificate for {hostname}: {exc}"
            )

    first_hostname = next(iter(baseline.statuses))
    observations.redirect = probe_http_redirect(
        connection_target(http_target, first_hostname, 80), first_hostname, timeout
    )

    if access_log is not None:
        routers, errors = read_access_log(access_log, baseline.statuses)
        observations.routers.update(routers)
        observations.collection_errors.extend(errors)
    return observations
