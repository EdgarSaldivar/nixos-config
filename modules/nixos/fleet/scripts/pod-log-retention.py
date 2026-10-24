#!/usr/bin/env python3
"""Delete Kubernetes container log lines older than a fixed age.

kubelet rotates /var/log/pods by SIZE only (container-log-max-size, default
10Mi, x container-log-max-files, default 5). There is no age knob. A busy
container (traefik) cycles through its five files in about a day; a quiet one
(postgres, garage) can keep the same 0.log for as long as its Pod lives, which
is months. This program is the age cap kubelet does not have.

Layout it acts on, as written by kubelet + containerd's CRI plugin:

    /var/log/pods/<ns>_<pod>_<uid>/<container>/<restart>.log
    /var/log/pods/<ns>_<pod>_<uid>/<container>/<restart>.log.<YYYYmmdd-HHMMSS>[.gz]

Every line starts with an RFC 3339 timestamp: `<time> <stream> <P|F> <text>`.
Lines are written in order, so everything before the first line newer than the
cutoff is older than the cutoff.

  * A rotated file is no longer written. Its old lines are dropped by
    rewriting it beside itself and renaming over it; a file with nothing left
    is deleted.
  * A `<restart>.log` may still be open in containerd. containerd opens it
    O_APPEND, so truncating it in place is safe: the next write lands at the
    new end. The lines worth keeping are first copied to a rotated name that
    kubelet's own cleanup recognises, exactly as kubelet would have rotated it.
    The truncate goes through the descriptor that was read, never the path:
    kubelet may rename 0.log and have containerd reopen a fresh one meanwhile,
    and truncating by path would then empty the NEW file. Through the
    descriptor it empties only the file whose tail was just copied aside.
    This is logrotate's `copytruncate`, with its known gap: a line appended
    after the final catch-up read and before the truncate is lost. That window
    is a few microseconds of this program, and it opens only on a file whose
    first line is a month old.

Files whose first line has no timestamp are not touched, and the program exits
1 so the unit shows failed rather than silently keeping logs past the cap.
"""

import gzip
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

LIVE = re.compile(r"^\d+\.log$")
ROTATED = re.compile(r"^\d+\.log\.\d{8}-\d{6}(\.gz)?$")
# The CRI timestamp is RFC 3339 with nanoseconds; datetime takes microseconds.
STAMP = re.compile(rb"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d) ")
# kubelet's rotation suffix (container_log_manager.go timestampFormat), local time.
ROTATED_SUFFIX = "%Y%m%d-%H%M%S"


def line_time(line):
    """Seconds since the epoch for a CRI log line, or None if it has no stamp."""
    match = STAMP.match(line)
    if match is None:
        return None
    whole, fraction, zone = match.groups()
    text = whole.decode()
    if fraction:
        text += "." + fraction[:6].decode().ljust(6, "0")
    text += "+00:00" if zone == b"Z" else zone.decode()
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        # Shaped like a stamp but not a date (2026-02-30): treat as unstamped.
        return None


def opener(path):
    return gzip.open if path.name.endswith(".gz") else open


def first_line(path):
    """The first line of a log, decompressed; b"" for an empty file."""
    with opener(path)(path, "rb") as handle:
        return handle.readline()


def create_private(path):
    """Create a file nobody else can read until its mode is copied from the source."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600), "wb")


def aside_name(path, now):
    """A kubelet-style rotated name for a live log's kept tail.

    One second in the past, so it cannot be the name kubelet itself picks if it
    rotates this file in the same second.
    """
    return path.with_name(f"{path.name}.{time.strftime(ROTATED_SUFFIX, time.localtime(now - 1))}")


def keep_offset(handle, cutoff):
    """Advance past every line older than cutoff; return where the rest starts.

    A line with no stamp of its own (none are expected) is treated as part of
    the line before it, and so is dropped with it.
    """
    while True:
        offset = handle.tell()
        line = handle.readline()
        if not line:
            return offset
        stamp = line_time(line)
        if stamp is not None and stamp >= cutoff:
            return offset


def trim_rotated(path, cutoff):
    """Drop old lines from a file nothing writes to any more."""
    temporary = path.with_name(f".{path.name}.prune")
    with opener(path)(path, "rb") as source:
        start = keep_offset(source, cutoff)
        source.seek(start)
        rest = source.read()
    if not rest:
        path.unlink()
        return "deleted"
    with create_private(temporary) as raw:
        if path.name.endswith(".gz"):
            # Closing the GzipFile writes its trailer; `raw` is closed after it.
            with gzip.GzipFile(fileobj=raw, mode="wb") as target:
                target.write(rest)
        else:
            raw.write(rest)
    os.chmod(temporary, path.stat().st_mode & 0o7777)
    os.replace(temporary, path)
    return "trimmed"


def trim_live(path, cutoff, now):
    """Copy the recent tail of a possibly-open log aside, then truncate it."""
    with open(path, "r+b") as source:
        start = keep_offset(source, cutoff)
        source.seek(start)
        rest = source.read()
        if rest:
            # Exclusive create: never overwrite a rotation kubelet already made.
            with create_private(aside_name(path, now)) as target:
                target.write(rest)
                # Whatever containerd appended while that was written.
                target.write(source.read())
                os.fchmod(target.fileno(), os.fstat(source.fileno()).st_mode & 0o7777)
        os.ftruncate(source.fileno(), 0)
    return "truncated"


def prune(root, max_age, now):
    cutoff = now - max_age
    counts = {"checked": 0, "trimmed": 0, "deleted": 0, "truncated": 0, "gone": 0, "failed": 0}
    # A rewrite interrupted before its rename leaves its temporary behind, and that
    # name matches neither pattern below, so it would never age out on its own.
    for leftover in root.glob("*/*/.*.prune"):
        leftover.unlink(missing_ok=True)
    for path in sorted(root.glob("*/*/*")):
        if path.is_symlink() or not path.is_file():
            continue
        live = LIVE.match(path.name) is not None
        if not live and ROTATED.match(path.name) is None:
            continue
        counts["checked"] += 1
        try:
            line = first_line(path)
        except FileNotFoundError:
            # Its Pod was deleted after the directory was listed; Jobs churn.
            counts["gone"] += 1
            continue
        except (OSError, EOFError) as error:
            # gzip raises BadGzipFile (an OSError) or EOFError on a damaged file.
            print(f"FAIL: {path}: {error}", file=sys.stderr)
            counts["failed"] += 1
            continue
        if not line:
            continue
        stamp = line_time(line)
        if stamp is None:
            print(f"FAIL: {path}: first line has no timestamp; left as is", file=sys.stderr)
            counts["failed"] += 1
            continue
        if stamp >= cutoff:
            continue
        try:
            outcome = trim_live(path, cutoff, now) if live else trim_rotated(path, cutoff)
        except FileNotFoundError:
            counts["gone"] += 1
            continue
        except (OSError, EOFError) as error:
            print(f"FAIL: {path}: {error}", file=sys.stderr)
            counts["failed"] += 1
            continue
        counts[outcome] += 1
        print(f"{outcome}: {path}")
    return counts


def main():
    root = Path(os.environ.get("POD_LOG_ROOT", "/var/log/pods"))
    max_age = int(os.environ["POD_LOG_MAX_AGE_SECONDS"])
    if not root.is_dir():
        print(f"{root} does not exist; nothing to do")
        return 0
    counts = prune(root, max_age, time.time())
    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
