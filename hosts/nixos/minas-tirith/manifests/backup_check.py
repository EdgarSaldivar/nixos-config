"""Keep the cumulative object inventory and prove the dump's references are backed up.

  python backup_check.py --work /backup/work --mirror /backup/mirror \
      --target /backup --lock-state /run/lock-state/state
  python backup_check.py --work DIR --mirror DIR --no-lock   # a restored copy (drill)

In the backup Job it runs only when TARGET is the directory pelargir prepared (its
sentinel names TARGET's device:inode) and this pod holds the pipeline lock (the lock
sidecar, backup_lock.py, wrote exactly "acquired" to the lock-state file); otherwise it
exits 1 before reading or writing anything. One of --lock-state and --no-lock is required.

Inputs in WORK: objects-1.json and objects-2.json (`rclone lsjson -R --metadata
--files-only` of the bucket, taken before and after the database dump), refs.txt (every
object key the dump references, one per line) and objects.json (the cumulative inventory
from earlier runs, absent on the first).

An entry is VALID when it is an object whose Path equals its key, whose Metadata is an
object, and which carries a Content-Type (MimeType, or content-type in Metadata): what a
restore needs to put the object back. Listing records that are not valid (null, Path-only,
...) are counted, reported and never merged.

1. Prune: an objects.json entry whose key is neither a file in MIRROR nor in refs.txt is
   dropped. The mirror loses a key only in the previous run's final `rclone sync`, which
   runs after that run's restic snapshot, so the object and its entry are both in history.
2. Merge: every valid listed object is added to objects.json, keyed by Path; listing 1 then
   listing 2 overwrite older entries (the newest valid listing wins). No entry is dropped
   here, and a valid entry is never replaced by an invalid one.
3. Verify: every key in refs.txt must be a regular file under MIRROR AND have a valid entry
   in objects.json.

objects.json is written (atomically) before the verdict, so a failed run keeps what it
learned. Exit 0 when every reference is backed up, 1 when any is missing (they are
printed) or the guard refuses, 2 on unusable input.
"""

import argparse
import json
import os
import sys
from typing import NoReturn

SENTINEL = ".pincollector-backup-target"


def fail_input(message: str) -> NoReturn:
    print(f"backup_check: {message}", file=sys.stderr)
    sys.exit(2)


def refuse(message: str) -> NoReturn:
    print(f"refusing: {message}", file=sys.stderr)
    sys.exit(1)


def guard(target: str, lock_state: str) -> None:
    """The same checks as backup_guard.sh: the right directory, and the lock held."""
    sentinel = os.path.join(target, SENTINEL)
    if os.path.islink(sentinel) or not os.path.isfile(sentinel):
        refuse(f"{target} is not the prepared PinCollector backup target")
    with open(sentinel, encoding="utf-8") as handle:
        expected = handle.read().strip()
    info = os.stat(target)
    actual = f"{info.st_dev}:{info.st_ino}"
    if actual != expected:
        refuse(f"{target} is {actual}, the prepared target is {expected or '<empty>'}")
    try:
        with open(lock_state, encoding="utf-8") as handle:
            state = handle.read().strip()
    except OSError:
        state = ""
    if state != "acquired":
        refuse(f"the backup lock is not held by this pod (lock state: {state or 'none'})")


def valid(key: str, entry: object) -> bool:
    if not isinstance(entry, dict) or entry.get("Path") != key:
        return False
    metadata = entry.get("Metadata")
    if not isinstance(metadata, dict):
        return False
    mime = entry.get("MimeType")
    if isinstance(mime, str) and mime:
        return True
    return any(
        isinstance(name, str) and name.lower() == "content-type" and isinstance(value, str) and value
        for name, value in metadata.items()
    )


def load_listing(path: str) -> tuple[dict[str, dict], list[str]]:
    """Valid file entries keyed by Path, and a description of every rejected record."""
    try:
        with open(path, encoding="utf-8") as handle:
            items = json.load(handle)
    except (OSError, ValueError) as error:
        fail_input(f"cannot read listing {path}: {error}")
    if not isinstance(items, list):
        fail_input(f"{path} is not an rclone lsjson list")
    listing = {}
    rejected = []
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("IsDir") is True:
            continue
        key = item.get("Path") if isinstance(item, dict) else None
        if isinstance(key, str) and valid(key, item):
            listing[key] = item
        else:
            name = key if isinstance(key, str) else "<no Path>"
            rejected.append(f"{os.path.basename(path)}[{index}] {name}")
    return listing, rejected


def load_inventory(path: str) -> dict[str, object]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            inventory = json.load(handle)
    except (OSError, ValueError) as error:
        fail_input(f"cannot read inventory {path}: {error}")
    if not isinstance(inventory, dict):
        fail_input(f"{path} is not an inventory keyed by Path")
    return inventory


def load_refs(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return sorted({line.rstrip("\n") for line in handle if line.strip()})
    except OSError as error:
        fail_input(f"cannot read references {path}: {error}")


def mirror_keys(mirror: str) -> set[str]:
    keys = set()
    for root, _dirs, files in os.walk(mirror):
        for name in files:
            full = os.path.join(root, name)
            if os.path.isfile(full) and not os.path.islink(full):
                keys.add(os.path.relpath(full, mirror).replace(os.sep, "/"))
    return keys


def in_mirror(mirror: str, key: str) -> bool:
    # A key is a relative object path; anything that would resolve outside the mirror
    # (absolute, `..`, empty segments) is not a backed-up object.
    parts = key.split("/")
    if key.startswith("/") or any(part in ("", ".", "..") for part in parts):
        return False
    full = os.path.join(mirror, *parts)
    return os.path.isfile(full) and not os.path.islink(full)


def write_atomic(path: str, inventory: dict[str, object]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(inventory, handle, sort_keys=True, indent=1)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", required=True)
    parser.add_argument("--mirror", required=True)
    parser.add_argument("--target", help="the backup root whose sentinel is checked")
    locking = parser.add_mutually_exclusive_group(required=True)
    locking.add_argument("--lock-state", help="the lock sidecar's state file; must say acquired")
    locking.add_argument("--no-lock", action="store_true", help="no guard (a restored copy)")
    args = parser.parse_args(argv)

    if args.lock_state:
        if not args.target:
            parser.error("--lock-state needs --target")
        guard(args.target, args.lock_state)

    if not os.path.isdir(args.mirror):
        fail_input(f"mirror {args.mirror} is not a directory")
    first, rejected_first = load_listing(os.path.join(args.work, "objects-1.json"))
    second, rejected_second = load_listing(os.path.join(args.work, "objects-2.json"))
    refs = load_refs(os.path.join(args.work, "refs.txt"))
    inventory_path = os.path.join(args.work, "objects.json")
    inventory = load_inventory(inventory_path)

    present = mirror_keys(args.mirror)
    referenced = set(refs)
    pruned = sorted(key for key in inventory if key not in present and key not in referenced)
    for key in pruned:
        del inventory[key]
    # Listings hold only valid records, so a valid entry is only ever replaced by a valid one.
    inventory.update(first)
    inventory.update(second)
    write_atomic(inventory_path, inventory)

    rejected = rejected_first + rejected_second
    if rejected:
        print(f"backup_check: {len(rejected)} listing records rejected as invalid:", file=sys.stderr)
        for record in rejected:
            print(f"  {record}", file=sys.stderr)
    missing = [key for key in refs if not in_mirror(args.mirror, key) or not valid(key, inventory.get(key))]
    print(
        f"backup_check: {len(refs)} referenced keys, {len(inventory)} inventory entries "
        f"({len(pruned)} pruned), {len(present)} mirrored files, {len(rejected)} rejected records"
    )
    if missing:
        print(f"backup_check: {len(missing)} referenced keys are NOT backed up:", file=sys.stderr)
        for key in missing:
            print(f"  {key}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
