import gzip
import importlib.util
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


DEFAULT_SCRIPT = Path(__file__).parents[1] / "scripts" / "pod-log-retention.py"
SCRIPT = Path(os.environ.get("POD_LOG_RETENTION_SCRIPT", DEFAULT_SCRIPT))
SPEC = importlib.util.spec_from_file_location("pod_log_retention", SCRIPT)
retention = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = retention
SPEC.loader.exec_module(retention)

DAY = 86400
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc).timestamp()
MAX_AGE = 29 * DAY


def line(days_ago, text):
    stamp = datetime.fromtimestamp(NOW - days_ago * DAY).astimezone()
    # CRI writes nanoseconds and a numeric offset.
    rendered = stamp.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123" + stamp.strftime("%z")
    rendered = rendered[:-2] + ":" + rendered[-2:]
    return f"{rendered} stdout F {text}\n".encode()


@pytest.fixture
def container(tmp_path):
    directory = tmp_path / "pods" / "pin-collector_postgres-0_uid" / "postgres"
    directory.mkdir(parents=True)
    return directory


def run(tmp_path):
    return retention.prune(tmp_path / "pods", MAX_AGE, NOW)


def test_line_time_reads_cri_nanoseconds_and_zulu():
    assert retention.line_time(b"2026-10-23T19:31:44.674816930-07:00 stdout F x\n") == (
        datetime(2026, 10, 24, 2, 31, 44, 674816, tzinfo=timezone.utc).timestamp()
    )
    # RFC3339Nano drops trailing zeros, so the fraction varies in length or is absent.
    assert retention.line_time(b"2026-10-23T19:31:44.67481693-07:00 stdout F x\n") == (
        datetime(2026, 10, 24, 2, 31, 44, 674816, tzinfo=timezone.utc).timestamp()
    )
    assert retention.line_time(b"2026-10-23T12:00:00Z stderr P x\n") == NOW
    assert retention.line_time(b"no stamp here\n") is None
    # Stamp-shaped but not a date: unstamped, never an exception that ends the run.
    assert retention.line_time(b"2026-02-30T00:00:00Z stdout F x\n") is None


def test_recent_files_are_untouched(tmp_path, container):
    live = container / "0.log"
    body = line(28, "a") + line(1, "b")
    live.write_bytes(body)

    counts = run(tmp_path)

    assert live.read_bytes() == body
    assert counts["checked"] == 1
    assert counts["truncated"] == counts["trimmed"] == counts["deleted"] == 0


def test_live_log_keeps_recent_tail_aside_and_is_truncated(tmp_path, container):
    live = container / "0.log"
    live.write_bytes(line(40, "old") + line(30, "older than cap") + line(5, "keep") + line(0, "keep too"))
    live.chmod(0o640)

    counts = run(tmp_path)

    assert live.read_bytes() == b""
    aside = [p for p in container.iterdir() if p.name != "0.log"]
    assert len(aside) == 1
    assert retention.ROTATED.match(aside[0].name)
    assert aside[0].read_bytes() == line(5, "keep") + line(0, "keep too")
    assert aside[0].stat().st_mode & 0o777 == 0o640
    assert counts["truncated"] == 1


def test_live_log_with_nothing_recent_is_only_truncated(tmp_path, container):
    live = container / "1.log"
    live.write_bytes(line(60, "old") + line(31, "old"))

    run(tmp_path)

    assert live.read_bytes() == b""
    assert [p.name for p in container.iterdir()] == ["1.log"]


def test_truncation_is_seen_by_an_open_append_writer(tmp_path, container):
    live = container / "0.log"
    live.write_bytes(line(45, "old"))
    with open(live, "ab") as writer:
        run(tmp_path)
        writer.write(line(0, "after"))
    assert live.read_bytes() == line(0, "after")


def test_kubelet_rotation_mid_trim_never_truncates_the_new_file(tmp_path, container, monkeypatch):
    live = container / "0.log"
    live.write_bytes(line(40, "old") + line(1, "recent"))
    real_keep_offset = retention.keep_offset

    def kubelet_rotates_meanwhile(handle, cutoff):
        offset = real_keep_offset(handle, cutoff)
        # rotateLatestLog renames 0.log, then containerd reopens a fresh one.
        os.rename(live, container / "0.log.20261023-000000")
        live.write_bytes(line(0, "fresh"))
        return offset

    monkeypatch.setattr(retention, "keep_offset", kubelet_rotates_meanwhile)
    run(tmp_path)

    assert live.read_bytes() == line(0, "fresh")
    assert (container / "0.log.20261023-000000").read_bytes() == b""
    kept = b"".join(p.read_bytes() for p in container.iterdir())
    assert line(1, "recent") in kept


def test_lines_appended_before_the_truncate_are_kept_aside(tmp_path, container, monkeypatch):
    live = container / "0.log"
    live.write_bytes(line(40, "old") + line(1, "recent"))
    real_aside_name = retention.aside_name

    def containerd_writes_meanwhile(path, now):
        with open(live, "ab") as writer:
            writer.write(line(0, "appended mid-copy"))
        return real_aside_name(path, now)

    monkeypatch.setattr(retention, "aside_name", containerd_writes_meanwhile)
    run(tmp_path)

    (aside,) = [p for p in container.iterdir() if p.name != "0.log"]
    assert aside.read_bytes() == line(1, "recent") + line(0, "appended mid-copy")
    assert live.read_bytes() == b""


def test_aside_is_named_a_second_back_and_never_world_readable(tmp_path, container, monkeypatch):
    live = container / "0.log"
    live.write_bytes(line(40, "old") + line(1, "recent"))
    live.chmod(0o640)
    modes = []
    real_fdopen = os.fdopen

    def record_mode(fd, *args, **kwargs):
        modes.append(os.fstat(fd).st_mode & 0o777)
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(retention.os, "fdopen", record_mode)
    old_umask = os.umask(0o022)
    try:
        run(tmp_path)
    finally:
        os.umask(old_umask)

    assert modes == [0o600]
    expected = time.strftime(retention.ROTATED_SUFFIX, time.localtime(NOW - 1))
    assert (container / f"0.log.{expected}").stat().st_mode & 0o777 == 0o640


def test_symlinked_logs_are_never_followed(tmp_path, container):
    outside = tmp_path / "outside.log"
    body = line(60, "old, and not ours")
    outside.write_bytes(body)
    (container / "0.log").symlink_to(outside)

    counts = run(tmp_path)

    assert outside.read_bytes() == body
    assert counts["checked"] == 0


def test_a_pod_deleted_mid_run_is_gone_not_failed(tmp_path, container, monkeypatch):
    (container / "0.log").write_bytes(line(40, "old"))
    real_first_line = retention.first_line

    def pod_deleted(path):
        shutil.rmtree(path.parent.parent)
        return real_first_line(path)

    monkeypatch.setattr(retention, "first_line", pod_deleted)
    counts = run(tmp_path)

    assert counts["gone"] == 1 and counts["failed"] == 0


def test_rotated_plain_and_gzip_files_are_trimmed_or_deleted(tmp_path, container):
    mixed = container / "0.log.20260920-010101"
    mixed.write_bytes(line(35, "old") + line(10, "keep"))
    mixed_gz = container / "0.log.20260921-010101.gz"
    with gzip.open(mixed_gz, "wb") as handle:
        handle.write(line(33, "old") + line(2, "keep"))
    stale_gz = container / "0.log.20260901-010101.gz"
    with gzip.open(stale_gz, "wb") as handle:
        handle.write(line(50, "old") + line(30, "old"))

    counts = run(tmp_path)

    assert mixed.read_bytes() == line(10, "keep")
    with gzip.open(mixed_gz, "rb") as handle:
        assert handle.read() == line(2, "keep")
    assert not stale_gz.exists()
    assert not list(container.glob(".*"))
    assert counts["trimmed"] == 2 and counts["deleted"] == 1


def test_interrupted_rewrite_leftovers_are_removed(tmp_path, container):
    leftover = container / ".0.log.20260920-010101.prune"
    leftover.write_bytes(line(3, "copy of a recent line"))

    run(tmp_path)

    assert not leftover.exists()


def test_unrecognised_names_and_empty_files_are_ignored(tmp_path, container):
    other = container / "0.log.tmp"
    other.write_bytes(line(90, "kubelet temp file"))
    (container / "0.log").write_bytes(b"")

    counts = run(tmp_path)

    assert other.exists()
    assert counts["checked"] == 1 and counts["failed"] == 0


def test_unstamped_or_damaged_files_fail_loudly_and_are_kept(tmp_path, container):
    unstamped = container / "0.log"
    unstamped.write_bytes(b"not a cri line\n")
    damaged = container / "0.log.20260901-010101.gz"
    damaged.write_bytes(b"\x1f\x8b not gzip")

    counts = run(tmp_path)

    assert unstamped.exists() and damaged.exists()
    assert counts["failed"] == 2


def test_main_exit_status(tmp_path, container, monkeypatch, capsys):
    (container / "0.log").write_bytes(line(40, "old"))
    monkeypatch.setenv("POD_LOG_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("POD_LOG_MAX_AGE_SECONDS", str(MAX_AGE))
    monkeypatch.setattr(time, "time", lambda: NOW)

    assert retention.main() == 0
    assert "truncated=1" in capsys.readouterr().out

    (container / "0.log").write_bytes(b"garbage\n")
    assert retention.main() == 1

    monkeypatch.setenv("POD_LOG_ROOT", str(tmp_path / "absent"))
    assert retention.main() == 0
