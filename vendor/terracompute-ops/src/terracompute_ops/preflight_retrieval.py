"""Fetch and publish the fixed Pelargir backup preflight attestation.

The network-facing fetch runs without privilege and can only populate its
private spool.  The root publisher has no network or credentials; it copies the
download into its root-owned directory, validates that exact copy with the
backup entrypoint contract, and only then renames it over the publication path.
"""

from __future__ import annotations

import argparse
import os
import re
import resource
import signal
import stat
import subprocess
import sys
from pathlib import Path

from .runtime_entrypoints import (
    MAX_CONFIG_BYTES,
    RuntimeConfigError,
    load_backup_config,
    load_backup_preflight,
)


SFTP_TARGET = "terracompute-backup@pelargir"
SFTP_PREFLIGHT_PATH = "/terracompute-preflight.json"
FETCH_TIMEOUT_SECONDS = 30
_SAFE_ABSOLUTE_PATH = re.compile(r"^/[A-Za-z0-9._+/@%=-]+$")


class PreflightRetrievalError(RuntimeError):
    """A fixed-code preflight retrieval or publication failure."""


def _absolute_path(path: Path, label: str) -> Path:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or _SAFE_ABSOLUTE_PATH.fullmatch(str(path)) is None
    ):
        raise PreflightRetrievalError(f"{label}-path-invalid")
    return path


def _private_regular_file(path: Path) -> bool:
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        and status.st_uid in {0, os.geteuid()}
        and stat.S_IMODE(status.st_mode) & 0o077 == 0
        and status.st_nlink == 1
        and status.st_size > 0
    )


def _exact_executable(path: Path) -> bool:
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        and bool(stat.S_IMODE(status.st_mode) & 0o111)
    )


def _limit_fetch_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_CONFIG_BYTES, MAX_CONFIG_BYTES))


def _directory_fd(
    path: Path,
    *,
    label: str,
    owner: int | None,
    remove_names: tuple[str, ...] = (),
) -> int:
    """Open one real directory, optionally clearing fixed names before checks.

    Clearing through the opened directory descriptor makes an old publication
    unusable even when the following ownership/mode check rejects the parent.
    The failed publisher dependency still prevents backup from starting.
    """

    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.name in ("", ".", "..")
    ):
        raise PreflightRetrievalError(f"{label}-directory-invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PreflightRetrievalError(f"{label}-directory-unavailable") from error
    try:
        for name in remove_names:
            try:
                os.unlink(name, dir_fd=descriptor)
            except FileNotFoundError:
                pass
            except OSError as error:
                raise PreflightRetrievalError(f"{label}-cleanup-failed") from error
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or (owner is not None and status.st_uid != owner)
            or status.st_mode & 0o022
        ):
            raise PreflightRetrievalError(f"{label}-directory-unsafe")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_input(directory_fd: int, name: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        status = os.fstat(descriptor)
    except OSError as error:
        raise PreflightRetrievalError("preflight-input-unavailable") from error
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_size <= 0
        or status.st_size > MAX_CONFIG_BYTES
        or status.st_mode & 0o037
    ):
        os.close(descriptor)
        raise PreflightRetrievalError("preflight-input-unsafe")
    return descriptor, status


def _create_private_file(directory_fd: int, name: str, mode: int) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(name, flags, mode, dir_fd=directory_fd)
    except OSError as error:
        raise PreflightRetrievalError("preflight-output-unsafe") from error


def _fsync_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise PreflightRetrievalError("preflight-directory-sync-failed") from error


def fetch_preflight(
    *,
    config_path: Path,
    incoming_path: Path,
    sftp_executable: Path,
    identity_path: Path,
    known_hosts_path: Path,
) -> None:
    """Fetch the one fixed remote file into a private single-link spool file."""

    if not incoming_path.is_absolute() or incoming_path.name in ("", ".", ".."):
        raise PreflightRetrievalError("incoming-path-invalid")
    _absolute_path(incoming_path, "incoming")
    _absolute_path(config_path, "config")
    _absolute_path(sftp_executable, "sftp-executable")
    _absolute_path(identity_path, "ssh-identity")
    _absolute_path(known_hosts_path, "ssh-known-hosts")
    if not _exact_executable(sftp_executable):
        raise PreflightRetrievalError("sftp-executable-invalid")
    if not _private_regular_file(identity_path) or not _private_regular_file(
        known_hosts_path
    ):
        raise PreflightRetrievalError("preflight-credential-invalid")
    incoming_name = incoming_path.name
    temporary_name = f".{incoming_name}.fetching"
    directory_fd = _directory_fd(
        incoming_path.parent,
        label="incoming",
        owner=os.geteuid(),
        remove_names=(incoming_name, temporary_name),
    )
    output_fd: int | None = None
    try:
        output_fd = _create_private_file(directory_fd, temporary_name, 0o640)
        command = (
            str(sftp_executable),
            "-q",
            "-F",
            "/dev/null",
            "-b",
            "-",
            "-oBatchMode=yes",
            "-oIdentitiesOnly=yes",
            "-oIdentityAgent=none",
            "-oPasswordAuthentication=no",
            "-oKbdInteractiveAuthentication=no",
            "-oStrictHostKeyChecking=yes",
            f"-oUserKnownHostsFile={known_hosts_path}",
            "-oGlobalKnownHostsFile=/dev/null",
            "-oConnectTimeout=10",
            "-oConnectionAttempts=1",
            "-oClearAllForwardings=yes",
            "-oForwardAgent=no",
            "-oPermitLocalCommand=no",
            "-oRequestTTY=no",
            "-i",
            str(identity_path),
            "--",
            SFTP_TARGET,
        )
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"LC_ALL": "C"},
                close_fds=True,
                preexec_fn=_limit_fetch_output,
                start_new_session=True,
            )
            try:
                process.communicate(
                    f"get {SFTP_PREFLIGHT_PATH} {incoming_path.parent / temporary_name}\n".encode(
                        "ascii"
                    ),
                    timeout=FETCH_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise PreflightRetrievalError("preflight-fetch-timeout") from None
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise PreflightRetrievalError("preflight-fetch-unavailable") from error
        if process.returncode != 0:
            raise PreflightRetrievalError("preflight-fetch-failed")
        os.fsync(output_fd)
        status = os.fstat(output_fd)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_size <= 0
            or status.st_size > MAX_CONFIG_BYTES
        ):
            raise PreflightRetrievalError("preflight-fetch-invalid")
        os.close(output_fd)
        output_fd = None

        config = load_backup_config(config_path)
        temporary_path = incoming_path.parent / temporary_name
        load_backup_preflight(temporary_path, config)
        os.replace(
            temporary_name,
            incoming_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        _fsync_directory(directory_fd)
    except BaseException:
        for name in (temporary_name, incoming_name):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if output_fd is not None:
            os.close(output_fd)
        os.close(directory_fd)


def publish_preflight(
    *,
    config_path: Path,
    incoming_path: Path,
    publication_path: Path,
    publication_owner: int | None = None,
) -> None:
    """Revalidate a root-controlled copy and atomically publish the attestation."""

    _absolute_path(config_path, "config")
    _absolute_path(incoming_path, "incoming")
    _absolute_path(publication_path, "publication")
    if incoming_path.name in ("", ".", "..") or publication_path.name in ("", ".", ".."):
        raise PreflightRetrievalError("preflight-path-invalid")
    publication_name = publication_path.name
    temporary_name = f".{publication_name}.publishing"
    publication_fd = _directory_fd(
        publication_path.parent,
        label="publication",
        owner=os.geteuid() if publication_owner is None else publication_owner,
        remove_names=(publication_name, temporary_name),
    )
    incoming_fd: int | None = None
    output_fd: int | None = None
    try:
        incoming_directory_fd = _directory_fd(
            incoming_path.parent,
            label="incoming",
            owner=None,
        )
        try:
            incoming_fd, input_status = _open_private_input(
                incoming_directory_fd, incoming_path.name
            )
            directory_status = os.fstat(incoming_directory_fd)
            if (
                input_status.st_uid != directory_status.st_uid
                or input_status.st_gid != directory_status.st_gid
            ):
                raise PreflightRetrievalError("preflight-input-owner-invalid")
        finally:
            os.close(incoming_directory_fd)

        output_fd = _create_private_file(publication_fd, temporary_name, 0o640)
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(incoming_fd, min(65536, remaining))
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                if written <= 0:
                    raise PreflightRetrievalError("preflight-copy-failed")
                view = view[written:]
            remaining -= len(chunk)
        if remaining == 0 and os.read(incoming_fd, 1):
            raise PreflightRetrievalError("preflight-size-limit")
        os.fchmod(output_fd, 0o640)
        os.fsync(output_fd)
        os.close(output_fd)
        output_fd = None

        config = load_backup_config(config_path)
        temporary_path = publication_path.parent / temporary_name
        load_backup_preflight(temporary_path, config)

        try:
            os.stat(publication_name, dir_fd=publication_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise PreflightRetrievalError("preflight-destination-raced")
        os.replace(
            temporary_name,
            publication_name,
            src_dir_fd=publication_fd,
            dst_dir_fd=publication_fd,
        )
        _fsync_directory(publication_fd)
    except BaseException:
        for name in (temporary_name, publication_name):
            try:
                os.unlink(name, dir_fd=publication_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if incoming_fd is not None:
            os.close(incoming_fd)
        if output_fd is not None:
            os.close(output_fd)
        os.close(publication_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terracompute-backup-preflight", allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", allow_abbrev=False)
    fetch.add_argument("--config", required=True, type=Path)
    fetch.add_argument("--incoming", required=True, type=Path)
    fetch.add_argument("--sftp-executable", required=True, type=Path)
    fetch.add_argument("--ssh-identity-file", required=True, type=Path)
    fetch.add_argument("--ssh-known-hosts-file", required=True, type=Path)
    publish = commands.add_parser("publish", allow_abbrev=False)
    publish.add_argument("--config", required=True, type=Path)
    publish.add_argument("--incoming", required=True, type=Path)
    publish.add_argument("--publication", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "fetch":
            fetch_preflight(
                config_path=args.config,
                incoming_path=args.incoming,
                sftp_executable=args.sftp_executable,
                identity_path=args.ssh_identity_file,
                known_hosts_path=args.ssh_known_hosts_file,
            )
        else:
            if os.geteuid() != 0:
                raise PreflightRetrievalError("preflight-publisher-not-root")
            publish_preflight(
                config_path=args.config,
                incoming_path=args.incoming,
                publication_path=args.publication,
                publication_owner=0,
            )
        return 0
    except (PreflightRetrievalError, RuntimeConfigError, OSError, ValueError):
        print("preflight-retrieval-failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
