"""Put restored objects back into Garage with their recorded Content-Type and user metadata.

  python backup_restore_objects.py push --work DIR --mirror DIR [--keys-from FILE]
      [--overwrite] [--skip-unrecorded] [--rclone PATH]
  python backup_restore_objects.py list [--rclone PATH]   # the keys Garage holds, one per line
  python backup_restore_objects.py map       # rclone --metadata-mapper; reads $RESTORE_OBJECTS_INDEX
  python backup_restore_objects.py verify --objects FILE --keys FILE --listing FILE

A plain `rclone copy` of the restored mirror derives Content-Type from the file extension and
drops the app's user metadata (owner-user-id, golden-truth-catalog-id,
crop-evidence-source-sha256). The backup recorded both in WORK/objects.json (its format:
backup_check.py). Operator procedure: docs/runbooks/minas-tirith/pin-collector-garage.md,
"Restore".

push, the only mode an operator runs, does three things:
1. Plan: the keys to restore are the regular files under MIRROR (optionally only those named
   in --keys-from, e.g. WORK/refs.txt). Every one must have a valid objects.json entry whose
   Size matches the file; a mirror file without one stops the push before anything is
   written, unless --skip-unrecorded (the file is then left out and listed). With
   --keys-from every named key must be restorable, so --skip-unrecorded is refused there:
   a key someone asked for is never silently left out.
2. Copy, with the app's read/write key, never the backup's read-only one:
     rclone copy MIRROR garage:pin-collector-uploads --files-from-raw PLAN
       --metadata --metadata-mapper "PYTHON THIS map" --ignore-existing
   rclone starts `map` once per object, so map never reads objects.json (re-parsing a
   multi-MB inventory per object would make a total-loss restore quadratic). Once the plan
   validates, push writes an index in its scratch directory: one small JSON file per planned
   key, the key's objects.json entry verbatim, named by the SHA-256 of the key, and passes
   that directory to map as $RESTORE_OBJECTS_INDEX. map opens only its key's file and returns
   exactly the recorded metadata; it fails (so rclone fails that object) when the key has no
   index file, the file is unreadable or not a valid entry for that key, or the recorded size
   differs. It never lets an object through without its recorded metadata. The index is
   removed with the scratch directory when push ends; map is not meant to be run on its own.
   Default --ignore-existing: only keys Garage does not hold are written, so a live object
   (possibly newer than the backup) is never replaced. --overwrite swaps it for
   --ignore-times: every planned key is uploaded again, replacing whatever Garage holds (a
   plain copy would skip a same-size, same-mtime object whose metadata is wrong). Use it only
   with the API stopped.
3. Verify: list the destination (`rclone lsjson -R --metadata --files-only`) and compare every
   planned key's size, Content-Type and user metadata with objects.json.

What map returns, from the recorded Metadata (keys lower-cased, as S3 and rclone use them):
everything except the fields rclone derives or cannot write: btime (Last-Modified; rclone
would store it as a bogus x-amz-meta-btime), mtime (rclone writes x-amz-meta-mtime itself from
the file, which carries the object's modtime through the mirror and restic), atime, tier
(read-only), md5chksum (rclone's own), and the object-lock-* fields (rclone reports them only
for a bucket with object lock, which Garage does not implement). content-type comes from
Metadata, else from the record's MimeType. verify ignores the same fields.

Nothing to restore: an empty --keys-from list is a legitimate outcome (the database-intact
flow found no lost keys), so push says so and exits 0 without calling rclone. A whole-mirror
push that plans no keys (an empty mirror, or every file left out by --skip-unrecorded) exits
1: a restored mirror of a bucket that held objects is never empty, so that points at the
wrong directory or a failed restore.

Exit codes: 0 success, 1 a refusal, a failed copy or a mismatch (printed), 2 unusable input.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from typing import NoReturn

INDEX_ENV = "RESTORE_OBJECTS_INDEX"
DEFAULT_DEST = "garage:pin-collector-uploads"
DEFAULT_SECRETS = "/run/secrets/pin-collector"
# Derived or read-only fields: never restored, never compared.
IGNORED = {"mtime", "btime", "atime", "tier", "md5chksum"}
IGNORED_PREFIXES = ("object-lock-",)


class Unusable(Exception):
    """Input that cannot be trusted to restore from."""


def fail_input(message: str) -> NoReturn:
    print(f"backup_restore_objects: {message}", file=sys.stderr)
    sys.exit(2)


def ignored(name: str) -> bool:
    return name in IGNORED or name.startswith(IGNORED_PREFIXES)


def load_objects(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            objects = json.load(handle)
    except (OSError, ValueError) as error:
        raise Unusable(f"cannot read {path}: {error}") from error
    if not isinstance(objects, dict):
        raise Unusable(f"{path} is not an inventory keyed by object key")
    return objects


def recorded_metadata(key: str, entry: object) -> dict[str, str]:
    """The metadata to write for KEY: what backup_check.py recorded, minus derived fields."""
    if not isinstance(entry, dict) or entry.get("Path") != key:
        raise Unusable(f"{key}: no valid objects.json entry")
    raw = entry.get("Metadata")
    if not isinstance(raw, dict):
        raise Unusable(f"{key}: recorded Metadata is not an object")
    metadata: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise Unusable(f"{key}: recorded Metadata has a non-string field {name!r}")
        lower = name.lower()
        if ignored(lower):
            continue
        if lower in metadata and metadata[lower] != value:
            raise Unusable(f"{key}: recorded Metadata has conflicting values for {lower}")
        metadata[lower] = value
    if not metadata.get("content-type"):
        mime = entry.get("MimeType")
        if not isinstance(mime, str) or not mime:
            raise Unusable(f"{key}: no recorded Content-Type")
        metadata["content-type"] = mime
    return metadata


def recorded_size(key: str, entry: dict) -> int:
    size = entry.get("Size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise Unusable(f"{key}: no recorded Size")
    return size


def safe_key(key: str) -> bool:
    parts = key.split("/")
    return bool(key) and not key.startswith("/") and all(part not in ("", ".", "..") for part in parts)


# ── map ──────────────────────────────────────────────────────────────────────────────


def index_name(key: str) -> str:
    """The index file for KEY: fixed length and free of '/', whatever the key holds."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"


def write_index(directory: str, objects: dict, keys: list[str]) -> None:
    """One file per key holding its objects.json entry verbatim; map re-validates it."""
    os.mkdir(directory, 0o700)
    for key in keys:
        with open(os.path.join(directory, index_name(key)), "x", encoding="utf-8") as handle:
            json.dump(objects.get(key), handle, sort_keys=True)


def load_index_entry(directory: str, key: str) -> object:
    path = os.path.join(directory, index_name(key))
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as error:
        raise Unusable(f"{key}: no index entry (not in the plan)") from error
    except (OSError, ValueError) as error:
        raise Unusable(f"{key}: cannot read its index entry {path}: {error}") from error


def mapper(stdin, stdout) -> int:
    index = os.environ.get(INDEX_ENV)
    if not index:
        print(f"map: {INDEX_ENV} is not set", file=sys.stderr)
        return 2
    try:
        request = json.load(stdin)
        if not isinstance(request, dict):
            raise Unusable("mapper input is not an object")
        # Remote is the object's path relative to the source root: the mirror root is the
        # bucket root, so it is the object key.
        key = request.get("Remote")
        if not isinstance(key, str) or not safe_key(key):
            raise Unusable(f"mapper input has no usable Remote: {key!r}")
        if request.get("IsDir") is True:
            raise Unusable(f"{key}: directories are not restored")
        entry = load_index_entry(index, key)
        metadata = recorded_metadata(key, entry)
        size = request.get("Size")
        if isinstance(size, int) and size >= 0 and size != recorded_size(key, entry):
            raise Unusable(f"{key}: file is {size} bytes, objects.json records {entry.get('Size')}")
    except (Unusable, ValueError) as error:
        print(f"map: {error}", file=sys.stderr)
        return 1
    json.dump({"Metadata": metadata}, stdout, sort_keys=True)
    stdout.write("\n")
    return 0


# ── plan ─────────────────────────────────────────────────────────────────────────────


def mirror_files(mirror: str) -> set[str]:
    keys = set()
    for root, _dirs, files in os.walk(mirror):
        for name in files:
            full = os.path.join(root, name)
            if os.path.isfile(full) and not os.path.islink(full):
                keys.add(os.path.relpath(full, mirror).replace(os.sep, "/"))
    return keys


def read_keys(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return sorted({line.rstrip("\n") for line in handle if line.strip()})
    except OSError as error:
        fail_input(f"cannot read keys {path}: {error}")


def plan(objects: dict, mirror: str, wanted: list[str] | None, skip_unrecorded: bool) -> list[str]:
    present = mirror_files(mirror)
    if wanted is None:
        candidates = sorted(present)
    else:
        absent = [key for key in wanted if key not in present]
        if absent:
            print(f"plan: {len(absent)} requested keys are not files in the mirror:", file=sys.stderr)
            for key in absent:
                print(f"  {key}", file=sys.stderr)
            sys.exit(1)
        candidates = wanted
    keys, unrecorded = [], []
    for key in candidates:
        entry = objects.get(key)
        try:
            recorded_metadata(key, entry)
            size = recorded_size(key, entry)
        except Unusable as error:
            unrecorded.append(str(error))
            continue
        actual = os.path.getsize(os.path.join(mirror, *key.split("/")))
        if actual != size:
            unrecorded.append(f"{key}: file is {actual} bytes, objects.json records {size}")
            continue
        keys.append(key)
    if unrecorded:
        verdict = "left out" if skip_unrecorded else "nothing written"
        print(f"plan: {len(unrecorded)} mirror files have no usable record ({verdict}):", file=sys.stderr)
        for line in unrecorded:
            print(f"  {line}", file=sys.stderr)
        if not skip_unrecorded:
            sys.exit(1)
    return keys


# ── verify ───────────────────────────────────────────────────────────────────────────


def listing_state(item: dict) -> tuple[object, dict[str, str]]:
    metadata = {
        name.lower(): value
        for name, value in (item.get("Metadata") or {}).items()
        if isinstance(name, str) and not ignored(name.lower())
    }
    if not metadata.get("content-type") and item.get("MimeType"):
        metadata["content-type"] = item["MimeType"]
    return item.get("Size"), metadata


def verify(objects: dict, keys: list[str], listing_path: str) -> int:
    try:
        with open(listing_path, encoding="utf-8") as handle:
            items = json.load(handle)
    except (OSError, ValueError) as error:
        fail_input(f"cannot read listing {listing_path}: {error}")
    if not isinstance(items, list):
        fail_input(f"{listing_path} is not an rclone lsjson list")
    destination = {
        item["Path"]: item
        for item in items
        if isinstance(item, dict) and not item.get("IsDir") and isinstance(item.get("Path"), str)
    }
    problems = []
    for key in keys:
        entry = objects.get(key)
        try:
            expected = (recorded_size(key, entry), recorded_metadata(key, entry))
        except Unusable as error:
            problems.append(str(error))
            continue
        item = destination.get(key)
        if item is None:
            problems.append(f"{key}: missing in destination")
            continue
        actual = listing_state(item)
        if actual[0] != expected[0]:
            problems.append(f"{key}: size {actual[0]}, recorded {expected[0]}")
        if actual[1] != expected[1]:
            problems.append(f"{key}: metadata {json.dumps(actual[1], sort_keys=True)}, "
                            f"recorded {json.dumps(expected[1], sort_keys=True)}")
    print(f"verify: {len(keys)} keys checked, {len(problems)} problems", file=sys.stderr)
    for line in problems:
        print(f"  {line}", file=sys.stderr)
    return 1 if problems else 0


# ── push ─────────────────────────────────────────────────────────────────────────────


def mapper_command(python: str, script: str) -> str:
    # rclone parses --metadata-mapper as space-separated, CSV-quoted fields.
    return " ".join('"' + part.replace('"', '""') + '"' for part in (python, script, "map"))


def rclone_env(dest: str, secrets: str, index: str, tmp: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(HOME=tmp, RCLONE_CONFIG=os.path.join(tmp, "rclone.conf"))
    if index:
        env[INDEX_ENV] = index
    open(env["RCLONE_CONFIG"], "a", encoding="utf-8").close()
    if dest.startswith("garage:"):
        # The app key (read/write), read from its files into this process's children only:
        # nothing on argv, nothing in the Pod spec.
        try:
            with open(os.path.join(secrets, "garage-app-key-id"), encoding="utf-8") as handle:
                key_id = handle.read().strip()
            with open(os.path.join(secrets, "garage-app-secret"), encoding="utf-8") as handle:
                secret = handle.read().strip()
        except OSError as error:
            # OSError names the file, never its content.
            fail_input(f"cannot read the Garage app key: {error}")
        if not key_id or not secret:
            fail_input(f"the Garage app key files in {secrets} are empty")
        env.update(
            RCLONE_CONFIG_GARAGE_TYPE="s3",
            RCLONE_CONFIG_GARAGE_PROVIDER="Other",
            RCLONE_CONFIG_GARAGE_ENDPOINT=env.get("RESTORE_GARAGE_ENDPOINT", "http://garage:3900"),
            RCLONE_CONFIG_GARAGE_REGION="us-east-1",
            RCLONE_CONFIG_GARAGE_FORCE_PATH_STYLE="true",
            RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID=key_id,
            RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY=secret,
        )
    return env


def push(args: argparse.Namespace) -> int:
    objects_path = os.path.abspath(os.path.join(args.work, "objects.json"))
    try:
        objects = load_objects(objects_path)
    except Unusable as error:
        fail_input(str(error))
    if not os.path.isdir(args.mirror):
        fail_input(f"mirror {args.mirror} is not a directory")
    wanted = read_keys(args.keys_from) if args.keys_from else None
    keys = plan(objects, args.mirror, wanted, args.skip_unrecorded)
    if not keys:
        if wanted is not None:
            # The list was empty (nothing lost): a clean no-op, not a failure.
            print("push: nothing to restore (the key list is empty)", file=sys.stderr)
            return 0
        print("push: nothing to restore (no usable file in the mirror)", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(dir=args.tmp) as tmp:
        plan_path = os.path.join(tmp, "plan.txt")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.writelines(f"{key}\n" for key in keys)
        index = os.path.join(tmp, "index")
        write_index(index, objects, keys)
        env = rclone_env(args.dest, args.secrets, index, tmp)
        copy = [
            args.rclone, "copy", args.mirror, args.dest,
            "--files-from-raw", plan_path,
            "--metadata",
            "--metadata-mapper", mapper_command(sys.executable, os.path.abspath(__file__)),
            "--ignore-times" if args.overwrite else "--ignore-existing",
            "--stats-one-line", "-v",
        ]
        print(f"push: {len(keys)} keys to {args.dest} "
              f"({'overwriting' if args.overwrite else 'only keys the destination lacks'})", file=sys.stderr)
        if subprocess.run(copy, env=env, check=False).returncode != 0:
            print("push: rclone copy failed (see above); verifying what arrived", file=sys.stderr)
            copied = False
        else:
            copied = True
        listing_path = os.path.join(tmp, "destination.json")
        with open(listing_path, "w", encoding="utf-8") as handle:
            listed = subprocess.run(
                [args.rclone, "lsjson", "-R", "--metadata", "--files-only", args.dest],
                env=env, stdout=handle, check=False,
            )
        if listed.returncode != 0:
            print("push: listing the destination failed", file=sys.stderr)
            return 1
        verdict = verify(objects, keys, listing_path)
    return 0 if copied and verdict == 0 else 1


def list_keys(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(dir=args.tmp) as tmp:
        env = rclone_env(args.dest, args.secrets, "", tmp)
        listed = subprocess.run([args.rclone, "lsf", "-R", "--files-only", args.dest], env=env, check=False)
    return 0 if listed.returncode == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("map", help="rclone --metadata-mapper program")

    check = modes.add_parser("verify", help="compare a destination listing with objects.json")
    check.add_argument("--objects", required=True)
    check.add_argument("--keys", required=True, help="the restored keys, one per line")
    check.add_argument("--listing", required=True, help="rclone lsjson -R --metadata of the destination")

    run = modes.add_parser("push", help="plan, copy with recorded metadata, verify")
    run.add_argument("--work", required=True, help="the restored work/ (holds objects.json)")
    run.add_argument("--mirror", required=True, help="the restored mirror/ (the bucket root)")
    run.add_argument("--keys-from", help="restore only these keys (e.g. WORK/refs.txt)")
    run.add_argument("--overwrite", action="store_true",
                     help="replace objects the destination already holds (API stopped only)")
    run.add_argument("--skip-unrecorded", action="store_true",
                     help="leave out mirror files without a usable objects.json record")
    run.add_argument("--dest", default=DEFAULT_DEST)
    run.add_argument("--secrets", default=DEFAULT_SECRETS, help="directory holding the Garage app key")
    run.add_argument("--rclone", default="rclone")
    run.add_argument("--tmp", default=None, help="scratch directory (default: the system one)")

    listing = modes.add_parser("list", help="print the keys the destination holds, one per line")
    listing.add_argument("--dest", default=DEFAULT_DEST)
    listing.add_argument("--secrets", default=DEFAULT_SECRETS, help="directory holding the Garage app key")
    listing.add_argument("--rclone", default="rclone")
    listing.add_argument("--tmp", default=None, help="scratch directory (default: the system one)")
    args = parser.parse_args(argv)

    if args.mode == "map":
        return mapper(sys.stdin, sys.stdout)
    if args.mode == "verify":
        try:
            objects = load_objects(args.objects)
        except Unusable as error:
            fail_input(str(error))
        return verify(objects, read_keys(args.keys), args.listing)
    if args.mode == "list":
        return list_keys(args)
    if args.keys_from and args.skip_unrecorded:
        parser.error("--skip-unrecorded cannot be combined with --keys-from: every named key must be restored")
    return push(args)


if __name__ == "__main__":
    sys.exit(main())
