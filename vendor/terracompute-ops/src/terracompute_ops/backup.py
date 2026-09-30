"""Consistent, locally verified backup snapshots for controller evidence.

This module intentionally has no restic, network, credential, retention, or
deletion integration.  It publishes a private local artifact and reports that
transport was not attempted; an independently commissioned service owns it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


MACHINE_ID = "17049"
GIB = 1024**3
LOCAL_SOFT_BUDGET_BYTES = 20 * GIB
WARN_RATIO = 0.75
ROUTINE_RESTRICT_RATIO = 0.90
MINIMUM_FREE_BYTES = 10 * GIB
DEFAULT_MAX_FILES = 2_048
# The state database holds 30 days of observations (about 3 GiB at the
# 2026-09 collection rate), so its single file dominates both bounds.
DEFAULT_MAX_TOTAL_BYTES = 8 * GIB
DEFAULT_MAX_FILE_BYTES = 6 * GIB
MAX_MANIFEST_BYTES = 1024 * 1024

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_EXCLUDED_COMPONENTS = frozenset(
    {
        ".aws",
        ".azure",
        ".codex",
        ".ssh",
        "auth",
        "auth.json",
        "credentials",
        "credential",
        "keychain",
        "secrets",
        "secret",
        "tenant",
        "tenants",
        "runtime",
    }
)
_EXCLUDED_SUFFIXES = (
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".keychain",
    ".keychain-db",
)


@dataclass(frozen=True)
class DiskAdmission:
    admitted: bool
    warning: bool
    reasons: tuple[str, ...]
    projected_usage_bytes: int
    projected_usage_ratio: float
    projected_free_bytes: int
    protected_history_preserved: bool = True


@dataclass(frozen=True)
class BackupResult:
    success: bool
    backup_id: str | None
    published_path: Path | None
    snapshot_sha256: str | None
    manifest_sha256: str | None
    file_count: int
    total_bytes: int
    verification: str
    transfer_status: str
    failure_code: str | None = None
    error: str | None = None
    admission: DiskAdmission | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    snapshot_sha256: str | None
    manifest_sha256: str | None
    file_count: int
    total_bytes: int
    error: str | None = None


class BackupError(ValueError):
    """A safe snapshot could not be created or validated."""


@dataclass(frozen=True)
class _FileIdentity:
    relative: Path
    directory: Path
    name: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True)
class _DirectoryIdentity:
    relative: Path
    device: int
    inode: int
    entries: tuple[str, ...]
    descriptor: int


@dataclass
class _ApprovedTree:
    root_device: int
    root_inode: int
    root_descriptor: int
    directories: dict[Path, _DirectoryIdentity]
    files: list[_FileIdentity]
    total_bytes: int

    def close(self) -> None:
        for directory in self.directories.values():
            os.close(directory.descriptor)
        os.close(self.root_descriptor)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise BackupError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def assess_disk_admission(
    used_bytes: int,
    free_bytes: int,
    planned_write_bytes: int = 0,
    *,
    capture_class: str = "routine",
    soft_budget_bytes: int = LOCAL_SOFT_BUDGET_BYTES,
    minimum_free_bytes: int = MINIMUM_FREE_BYTES,
) -> DiskAdmission:
    """Apply the 20 GiB/75%/90%/10 GiB collection policy without deleting data."""
    values = (used_bytes, free_bytes, planned_write_bytes, soft_budget_bytes, minimum_free_bytes)
    if any(not isinstance(value, int) or value < 0 for value in values):
        raise BackupError("disk byte counts must be non-negative integers")
    if soft_budget_bytes == 0:
        raise BackupError("soft budget must be greater than zero")
    if capture_class not in {"routine", "protected"}:
        raise BackupError("capture_class must be routine or protected")
    projected_usage = used_bytes + planned_write_bytes
    projected_free = max(0, free_bytes - planned_write_bytes)
    ratio = projected_usage / soft_budget_bytes
    reasons: list[str] = []
    warning = ratio >= WARN_RATIO
    if warning:
        reasons.append("local evidence usage is at or above the 75% warning threshold")
    admitted = True
    if projected_free < minimum_free_bytes:
        admitted = False
        reasons.append("the 10 GiB filesystem free-space reserve would be consumed")
    if capture_class == "routine" and ratio >= ROUTINE_RESTRICT_RATIO:
        admitted = False
        reasons.append("routine capture is restricted at 90% of the local soft budget")
    elif capture_class == "protected" and ratio >= ROUTINE_RESTRICT_RATIO:
        warning = True
        reasons.append("protected capture exceeds the routine threshold; expansion is required")
    return DiskAdmission(
        admitted,
        warning,
        tuple(reasons),
        projected_usage,
        ratio,
        projected_free,
    )


def _validate_target(machine_id: str) -> None:
    if str(machine_id) != MACHINE_ID:
        raise BackupError(f"backup target must be Vast machine {MACHINE_ID}")


def _safe_bundle_name(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        raise BackupError("bundle names must be single safe path components")
    if name in {".", ".."}:
        raise BackupError("bundle traversal is forbidden")
    return name


def _excluded(relative: Path) -> bool:
    lowered = tuple(part.lower() for part in relative.parts)
    return any(part in _EXCLUDED_COMPONENTS for part in lowered) or any(
        relative.name.lower().endswith(suffix) for suffix in _EXCLUDED_SUFFIXES
    ) or relative.name.lower().startswith(".env")


def _open_directory(path: Path | str, *, dir_fd: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags, dir_fd=dir_fd)


def _same_file(status: os.stat_result, identity: _FileIdentity) -> bool:
    return (
        stat.S_ISREG(status.st_mode)
        and status.st_dev == identity.device
        and status.st_ino == identity.inode
        and status.st_size == identity.size
        and status.st_mtime_ns == identity.mtime_ns
        and status.st_ctime_ns == identity.ctime_ns
        and not stat.S_IMODE(status.st_mode) & 0o222
    )


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _approved_files(
    evidence_root: Path,
    bundle_names: Iterable[str],
    *,
    max_files: int,
    max_total_bytes: int,
    max_file_bytes: int,
) -> _ApprovedTree:
    if not evidence_root.is_absolute():
        raise BackupError("evidence_root must be absolute")
    names = tuple(_safe_bundle_name(name) for name in bundle_names)
    if len(names) != len(set(names)):
        raise BackupError("approved bundle names must be unique")
    root_descriptor: int | None = None
    directories: dict[Path, _DirectoryIdentity] = {}
    files: list[_FileIdentity] = []
    total = 0
    try:
        root_descriptor = _open_directory(evidence_root)
        root_status = os.fstat(root_descriptor)

        def scan_directory(relative: Path, descriptor: int) -> None:
            nonlocal total
            status = os.fstat(descriptor)
            if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o222:
                raise BackupError(f"approved bundle directory is mutable: {relative}")
            try:
                entries = tuple(sorted(os.listdir(descriptor)))
            except OSError as exc:
                raise BackupError(
                    f"approved bundle directory cannot be scanned: {relative}"
                ) from exc
            directories[relative] = _DirectoryIdentity(
                relative, status.st_dev, status.st_ino, entries, descriptor
            )
            if len(directories) > max_files:
                raise BackupError("approved evidence exceeds directory-count limit")
            for name in entries:
                child = relative / name
                if _excluded(child):
                    raise BackupError(f"excluded path in approved bundle: {child}")
                child_status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(child_status.st_mode):
                    child_descriptor = _open_directory(name, dir_fd=descriptor)
                    opened_status = os.fstat(child_descriptor)
                    if (
                        opened_status.st_dev != child_status.st_dev
                        or opened_status.st_ino != child_status.st_ino
                    ):
                        os.close(child_descriptor)
                        raise BackupError(f"source changed during approved scan: {child}")
                    scan_directory(child, child_descriptor)
                    continue
                if not stat.S_ISREG(child_status.st_mode):
                    raise BackupError(f"non-regular file excluded: {child}")
                source_descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                try:
                    opened_status = os.fstat(source_descriptor)
                    if not stat.S_ISREG(opened_status.st_mode):
                        raise BackupError(f"non-regular file excluded: {child}")
                    if stat.S_IMODE(opened_status.st_mode) & 0o222:
                        raise BackupError(f"approved evidence file is mutable: {child}")
                    if opened_status.st_size > max_file_bytes:
                        raise BackupError(f"approved evidence file exceeds size limit: {child}")
                    digest = _hash_descriptor(source_descriptor)
                    completed_status = os.fstat(source_descriptor)
                    identity = _FileIdentity(
                        child,
                        relative,
                        name,
                        opened_status.st_dev,
                        opened_status.st_ino,
                        opened_status.st_size,
                        opened_status.st_mtime_ns,
                        opened_status.st_ctime_ns,
                        digest,
                    )
                    if not _same_file(completed_status, identity):
                        raise BackupError(f"source changed during approved scan: {child}")
                finally:
                    os.close(source_descriptor)
                files.append(identity)
                total += identity.size
                if len(files) > max_files:
                    raise BackupError("approved evidence exceeds file-count limit")
                if total > max_total_bytes:
                    raise BackupError("approved evidence exceeds total-size limit")

        for name in names:
            try:
                bundle_descriptor = _open_directory(name, dir_fd=root_descriptor)
            except OSError as exc:
                raise BackupError(
                    f"approved bundle is absent, a symlink, or not a real directory: {name}"
                ) from exc
            scan_directory(Path(name), bundle_descriptor)
        files.sort(key=lambda item: item.relative.as_posix())
        return _ApprovedTree(
            root_status.st_dev,
            root_status.st_ino,
            root_descriptor,
            directories,
            files,
            total,
        )
    except Exception:
        for directory in directories.values():
            os.close(directory.descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)
        raise


def _revalidate_approved_tree(evidence_root: Path, tree: _ApprovedTree) -> None:
    """Resolve the source namespace again without following any final symlink."""
    opened: dict[Path, int] = {}
    root_descriptor = _open_directory(evidence_root)
    try:
        root_status = os.fstat(root_descriptor)
        if (root_status.st_dev, root_status.st_ino) != (
            tree.root_device,
            tree.root_inode,
        ):
            raise BackupError("evidence root changed after approved scan")
        opened[Path()] = root_descriptor
        for relative, identity in sorted(
            tree.directories.items(), key=lambda item: (len(item[0].parts), item[0].as_posix())
        ):
            parent = relative.parent
            parent_descriptor = opened[parent]
            descriptor = _open_directory(relative.name, dir_fd=parent_descriptor)
            opened[relative] = descriptor
            status = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(status.st_mode)
                or stat.S_IMODE(status.st_mode) & 0o222
                or (status.st_dev, status.st_ino) != (identity.device, identity.inode)
                or tuple(sorted(os.listdir(descriptor))) != identity.entries
            ):
                raise BackupError(f"approved directory changed before copy: {relative}")
        for identity in tree.files:
            status = os.stat(
                identity.name,
                dir_fd=opened[identity.directory],
                follow_symlinks=False,
            )
            if not _same_file(status, identity):
                raise BackupError(f"approved file changed before copy: {identity.relative}")
    except OSError as exc:
        raise BackupError("approved source path changed before copy") from exc
    finally:
        for relative, descriptor in reversed(tuple(opened.items())):
            if relative != Path():
                os.close(descriptor)
        os.close(root_descriptor)


def _copy_approved_file(tree: _ApprovedTree, identity: _FileIdentity, destination: Path) -> None:
    parent_descriptor = tree.directories[identity.directory].descriptor
    try:
        source_descriptor = os.open(
            identity.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_descriptor
        )
    except OSError as exc:
        raise BackupError(f"approved file changed before copy: {identity.relative}") from exc
    destination_descriptor: int | None = None
    try:
        if not _same_file(os.fstat(source_descriptor), identity):
            raise BackupError(f"approved file changed before copy: {identity.relative}")
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
        )
        digest = hashlib.sha256()
        copied = 0
        while chunk := os.read(source_descriptor, 1024 * 1024):
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise BackupError(f"copy did not make progress: {identity.relative}")
                view = view[written:]
        if (
            copied != identity.size
            or digest.hexdigest() != identity.sha256
            or not _same_file(os.fstat(source_descriptor), identity)
        ):
            raise BackupError(f"approved file changed while copying: {identity.relative}")
        os.fchmod(destination_descriptor, 0o400)
        os.fsync(destination_descriptor)
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        os.close(source_descriptor)


def _fsync_directories(root: Path) -> None:
    """Flush every staged directory bottom-up after its final mode is set."""
    directories = [Path(directory) for directory, _dirs, _files in os.walk(root)]
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        os.chmod(directory, 0o500)
        descriptor = _open_directory(directory)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _discard_staging(path: Path) -> None:
    """Make only our unpublished temp tree writable, then remove it."""
    for directory, dirnames, filenames in os.walk(path, topdown=False):
        for filename in filenames:
            try:
                (Path(directory) / filename).chmod(0o600)
            except OSError:
                pass
        for dirname in dirnames:
            candidate = Path(directory) / dirname
            if not candidate.is_symlink():
                try:
                    candidate.chmod(0o700)
                except OSError:
                    pass
        try:
            Path(directory).chmod(0o700)
        except OSError:
            pass
    shutil.rmtree(path)


def _snapshot(
    connection: sqlite3.Connection, destination: Path, *, max_bytes: int
) -> None:
    if not isinstance(connection, sqlite3.Connection):
        raise BackupError("connection must be sqlite3.Connection")
    if connection.in_transaction and not connection.execute("PRAGMA query_only").fetchone()[0]:
        raise BackupError("caller connection has an open transaction")
    page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    if page_count * page_size > max_bytes:
        raise BackupError("SQLite source exceeds per-file size limit")
    progress_calls = 0

    def bounded_progress(_status: int, remaining: int, total: int) -> None:
        nonlocal progress_calls
        progress_calls += 1
        if total * page_size > max_bytes or progress_calls > page_count + 1_024:
            raise BackupError("SQLite backup exceeded its size/progress bound")

    database_name = str(connection.execute("PRAGMA database_list").fetchone()[2])
    if not database_name:
        target = sqlite3.connect(":memory:")
        try:
            connection.backup(target, pages=256, progress=bounded_progress, sleep=0.0)
            target.commit()
            content = target.serialize()
        finally:
            target.close()
        if len(content) > max_bytes:
            raise BackupError("SQLite snapshot exceeds per-file size limit")
        _write_private(destination, content)
        return

    if destination.exists() or destination.is_symlink():
        raise BackupError("SQLite snapshot destination already exists")
    target = sqlite3.connect(destination)
    try:
        connection.backup(target, pages=256, progress=bounded_progress, sleep=0.0)
        target.commit()
        # A live StateStore uses WAL. Normalize the standalone snapshot so an
        # isolated deserialize never depends on a sidecar WAL file.
        target.execute("PRAGMA journal_mode=DELETE")
        target.commit()
        check = target.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise BackupError("SQLite snapshot failed integrity_check")
    except Exception:
        target.close()
        destination.unlink(missing_ok=True)
        raise
    finally:
        try:
            target.close()
        except sqlite3.Error:
            pass
    if destination.stat().st_size > max_bytes:
        destination.unlink(missing_ok=True)
        raise BackupError("SQLite snapshot exceeds per-file size limit")
    os.chmod(destination, 0o400)
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sqlite_integrity(snapshot: Path) -> None:
    # Check the standalone file in place: read-only and immutable, so no journal
    # or WAL sidecar is read or created. Deserializing it into memory needed about
    # twice the database size, far beyond the backup unit's memory limit.
    uri = snapshot.absolute().as_uri() + "?mode=ro&immutable=1"
    try:
        restored = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise BackupError("SQLite snapshot is not restorable") from exc
    try:
        restored_check = restored.execute("PRAGMA integrity_check").fetchone()
        if restored_check is None or restored_check[0] != "ok":
            raise BackupError("isolated SQLite restore failed integrity_check")
    except sqlite3.DatabaseError as exc:
        raise BackupError("SQLite snapshot is not restorable") from exc
    finally:
        restored.close()


def _manifest_entries(root: Path, relative_files: Iterable[Path]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for relative in sorted(relative_files, key=lambda item: item.as_posix()):
        path = root / relative
        entries.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )
    return entries


def validate_backup(
    backup_path: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> ValidationResult:
    """Verify confinement, complete hashes, and an isolated SQLite restore."""
    try:
        path = Path(backup_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise BackupError("backup path must be an absolute real directory")
        root = path.absolute()
        manifest_path = root / "manifest.json"
        digest_path = root / "manifest.sha256"
        if manifest_path.is_symlink() or digest_path.is_symlink():
            raise BackupError("manifest symlinks are forbidden")
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise BackupError("manifest exceeds size limit")
        if digest_path.stat().st_size > 128:
            raise BackupError("manifest digest exceeds size limit")
        manifest_bytes = manifest_path.read_bytes()
        expected_manifest_hash = digest_path.read_text(encoding="ascii").strip()
        actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
            raise BackupError("manifest digest is malformed")
        if actual_manifest_hash != expected_manifest_hash:
            raise BackupError("manifest digest mismatch")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("manifest is not valid JSON") from exc
        if manifest.get("machine_id") != MACHINE_ID or manifest.get("format_version") != 1:
            raise BackupError("manifest target or format is invalid")
        approved = manifest.get("approved_bundles")
        if not isinstance(approved, list) or any(
            not isinstance(name, str) or _safe_bundle_name(name) != name for name in approved
        ):
            raise BackupError("manifest approved bundle list is invalid")
        if approved != sorted(set(approved)):
            raise BackupError("manifest approved bundle list is not canonical")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise BackupError("manifest has no files")
        if len(entries) > max_files:
            raise BackupError("manifest exceeds file-count limit")
        expected_paths: set[str] = set()
        total = 0
        snapshot_hash: str | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                raise BackupError("manifest file entry is invalid")
            relative_text = entry.get("path")
            if not isinstance(relative_text, str):
                raise BackupError("manifest path is invalid")
            relative = Path(relative_text)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise BackupError("manifest traversal is forbidden")
            if relative_text in expected_paths or _excluded(relative):
                raise BackupError("manifest contains duplicate or excluded path")
            expected_paths.add(relative_text)
            candidate = root
            for component in relative.parts:
                candidate = candidate / component
                if candidate.is_symlink():
                    raise BackupError("backup contains a symlink")
            if not candidate.is_file():
                raise BackupError("manifest file is missing")
            size = candidate.stat().st_size
            if size != entry.get("size"):
                raise BackupError(f"size mismatch: {relative_text}")
            total += size
            if total > max_total_bytes:
                raise BackupError("manifest exceeds total-size limit")
            digest = _sha256(candidate)
            if digest != entry.get("sha256"):
                raise BackupError(f"hash mismatch: {relative_text}")
            if relative_text == "state.sqlite3":
                snapshot_hash = digest
            elif (
                len(relative.parts) < 3
                or relative.parts[0] != "evidence"
                or relative.parts[1] not in approved
            ):
                raise BackupError("manifest evidence is not tied to an approved bundle")
        actual_paths: set[str] = set()
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            for dirname in dirnames:
                if (current / dirname).is_symlink():
                    raise BackupError("backup contains a directory symlink")
            for filename in filenames:
                relative_text = (current / filename).relative_to(root).as_posix()
                if relative_text not in {"manifest.json", "manifest.sha256"}:
                    actual_paths.add(relative_text)
        if actual_paths != expected_paths:
            raise BackupError("backup files do not exactly match manifest")
        if snapshot_hash is None:
            raise BackupError("manifest omits state.sqlite3")
        _sqlite_integrity(root / "state.sqlite3")
        return ValidationResult(
            True,
            snapshot_hash,
            actual_manifest_hash,
            len(entries),
            total,
        )
    except (BackupError, OSError, sqlite3.Error) as exc:
        return ValidationResult(False, None, None, 0, 0, str(exc)[:512])


def create_backup(
    connection: sqlite3.Connection,
    evidence_root: Path,
    approved_bundles: Iterable[str],
    publish_root: Path,
    *,
    machine_id: str = MACHINE_ID,
    backup_id: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    current_evidence_bytes: int = 0,
    available_bytes: int | None = None,
    minimum_free_bytes: int = MINIMUM_FREE_BYTES,
) -> BackupResult:
    """Publish one verified snapshot and approved immutable bundle set atomically.

    Failure is returned as metadata.  ``transfer_status`` is always
    ``not_attempted`` because restic transport belongs to an external service.
    """
    staging: Path | None = None
    admission: DiskAdmission | None = None
    approved_tree: _ApprovedTree | None = None
    try:
        _validate_target(machine_id)
        approved_bundles = tuple(approved_bundles)
        if not all(
            isinstance(value, int) and value > 0
            for value in (max_files, max_total_bytes, max_file_bytes)
        ):
            raise BackupError("backup limits must be positive integers")
        publish_root = Path(publish_root)
        evidence_root = Path(evidence_root)
        publish_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if publish_root.is_symlink() or not publish_root.is_dir():
            raise BackupError("publish_root must be a real directory")
        os.chmod(publish_root, 0o700)
        approved_tree = _approved_files(
            evidence_root,
            approved_bundles,
            max_files=max_files - 1,
            max_total_bytes=max_total_bytes,
            max_file_bytes=max_file_bytes,
        )
        evidence_bytes = approved_tree.total_bytes
        projected = evidence_bytes + 1024 * 1024  # conservative SQLite/header allowance
        free_bytes = (
            shutil.disk_usage(publish_root).free if available_bytes is None else available_bytes
        )
        admission = assess_disk_admission(
            current_evidence_bytes,
            free_bytes,
            projected,
            capture_class="protected",
            minimum_free_bytes=minimum_free_bytes,
        )
        if not admission.admitted:
            raise BackupError("; ".join(admission.reasons))
        created = _utc_text(clock())
        if backup_id is None:
            backup_id = created.replace(":", "").replace("-", "").replace(".", "")
        backup_id = _safe_bundle_name(backup_id)
        final = publish_root / backup_id
        if final.exists() or final.is_symlink():
            raise BackupError("backup_id already exists")
        staging = Path(tempfile.mkdtemp(prefix=".backup-", dir=publish_root))
        os.chmod(staging, 0o700)
        snapshot = staging / "state.sqlite3"
        _snapshot(connection, snapshot, max_bytes=max_file_bytes)
        relative_files = [Path("state.sqlite3")]
        _revalidate_approved_tree(evidence_root, approved_tree)
        for identity in approved_tree.files:
            destination_relative = Path("evidence") / identity.relative
            destination = staging / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _copy_approved_file(approved_tree, identity, destination)
            relative_files.append(destination_relative)
        _revalidate_approved_tree(evidence_root, approved_tree)
        entries = _manifest_entries(staging, relative_files)
        if len(entries) > max_files or sum(int(item["size"]) for item in entries) > max_total_bytes:
            raise BackupError("completed backup exceeds configured bounds")
        manifest = {
            "format_version": 1,
            "machine_id": MACHINE_ID,
            "created_at": created,
            "backup_id": backup_id,
            "approved_bundles": sorted(_safe_bundle_name(name) for name in approved_bundles),
            "files": entries,
            "transport": {"owner": "external-service", "status": "not_attempted"},
        }
        manifest_bytes = _canonical_json(manifest) + b"\n"
        admission = assess_disk_admission(
            current_evidence_bytes,
            free_bytes,
            sum(int(item["size"]) for item in entries) + len(manifest_bytes) + 65,
            capture_class="protected",
            minimum_free_bytes=minimum_free_bytes,
        )
        if not admission.admitted:
            raise BackupError("; ".join(admission.reasons))
        _write_private(staging / "manifest.json", manifest_bytes)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        _write_private(staging / "manifest.sha256", (manifest_hash + "\n").encode("ascii"))
        validation = validate_backup(
            staging, max_files=max_files, max_total_bytes=max_total_bytes
        )
        if not validation.valid:
            raise BackupError(f"pre-publish verification failed: {validation.error}")
        _fsync_directories(staging)
        os.rename(staging, final)
        staging = None
        parent_fd = _open_directory(publish_root)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return BackupResult(
            True,
            backup_id,
            final,
            validation.snapshot_sha256,
            validation.manifest_sha256,
            validation.file_count,
            validation.total_bytes,
            "isolated_restore_and_hashes_ok",
            "not_attempted",
            admission=admission,
        )
    except (BackupError, OSError, sqlite3.Error) as exc:
        return BackupResult(
            False,
            backup_id,
            None,
            None,
            None,
            0,
            0,
            "failed",
            "not_attempted",
            type(exc).__name__,
            str(exc)[:512],
            admission,
        )
    finally:
        if approved_tree is not None:
            approved_tree.close()
        if staging is not None and staging.exists():
            try:
                _discard_staging(staging)
            except OSError:
                # The failure result remains authoritative even on a filesystem
                # that forbids deletion of an unpublished read-only directory.
                pass


def restore_backup(
    backup_path: Path,
    destination: sqlite3.Connection,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> ValidationResult:
    """Verify an artifact, then restore its database into a caller connection."""
    result = validate_backup(
        Path(backup_path), max_files=max_files, max_total_bytes=max_total_bytes
    )
    if not result.valid:
        return result
    if not isinstance(destination, sqlite3.Connection):
        return ValidationResult(False, None, None, 0, 0, "destination is not sqlite3.Connection")
    if destination.in_transaction:
        return ValidationResult(False, None, None, 0, 0, "destination has an open transaction")
    try:
        destination.deserialize((Path(backup_path) / "state.sqlite3").read_bytes())
        check = destination.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise BackupError("restored database failed integrity_check")
        return result
    except (BackupError, OSError, sqlite3.Error) as exc:
        return ValidationResult(False, None, None, 0, 0, str(exc)[:512])
