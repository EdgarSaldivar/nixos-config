"""Hold the backup's pipeline lock for the life of this pod: the native sidecar `lock`.

  python backup_lock.py --target /backup --state /run/lock-state/state

concurrencyPolicy: Forbid covers only the Jobs the CronJob controller creates, and the
ReadWriteOnce volume admits any number of pods on pelargir, so a hand-started Job could run
beside the scheduled one. This takes flock(LOCK_EX|LOCK_NB) on TARGET/.lock and keeps the
descriptor open until the pod ends. The kernel drops the lock with the last descriptor, so
a killed pod never leaves a stale lock and nothing has to expire or be taken over.

The outcome goes to STATE (a memory emptyDir shared with the stages), written atomically:
"acquired", "busy" (another pod holds it), "wrong-target" (TARGET is not the directory
pelargir prepared: its sentinel must name TARGET's device:inode) or "error". Every stage
refuses unless it reads exactly "acquired". The sidecar's startupProbe waits for STATE, so
no stage starts before the attempt resolved. Then it sleeps; SIGTERM (the pod ending) exits 0.
"""

import argparse
import fcntl
import os
import signal
import stat
import sys
from typing import NoReturn

SENTINEL = ".pincollector-backup-target"


def publish(path: str, state: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(f"{state}\n")
    os.replace(tmp, path)
    print(f"backup lock: {state}", flush=True)


def target_ok(target: str) -> bool:
    sentinel = os.path.join(target, SENTINEL)
    try:
        if not stat.S_ISREG(os.lstat(sentinel).st_mode):
            return False
        with open(sentinel, encoding="utf-8") as handle:
            expected = handle.read().strip()
        info = os.stat(target)
    except OSError:
        return False
    return expected == f"{info.st_dev}:{info.st_ino}"


def hold() -> NoReturn:
    while True:
        signal.pause()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True)
    parser.add_argument("--state", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # A restarted sidecar starts over: no stale verdict survives it.
    try:
        os.unlink(args.state)
    except FileNotFoundError:
        pass

    if not target_ok(args.target):
        publish(args.state, "wrong-target")
        hold()
    try:
        # Never follow a symlink planted at the lock path; 0660 for the pods' group.
        fd = os.open(
            os.path.join(args.target, ".lock"),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o660,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("the lock path is not a regular file")
    except OSError as error:
        print(f"backup lock: cannot open the lock file: {error}", file=sys.stderr, flush=True)
        publish(args.state, "error")
        hold()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        publish(args.state, "busy")
        hold()
    publish(args.state, "acquired")
    hold()


if __name__ == "__main__":
    main()
