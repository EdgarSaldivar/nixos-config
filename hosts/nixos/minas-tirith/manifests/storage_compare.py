"""Compare two `rclone lsjson -R --metadata --files-only` listings of the same bucket.

  python storage_compare.py SRC.json DST.json --diff DIFF.txt [--report-only]

Equal means: the same keys, and per key the same size, Content-Type and user metadata.
Fields rclone itself writes or derives on copy (mtime, btime, tier, md5chksum) and the
listing's own ModTime are not part of the object's identity and are ignored.

Writes every key that exists in SRC but differs or is missing in DST to DIFF (one per
line, the format `rclone --files-from` reads). Keys only in DST cannot be repaired by a
copy; they are reported and always fail. With --report-only it exits 0 unless a key is
only in DST; otherwise it exits 1 on any difference.
"""

import argparse
import json
import sys

IGNORED = {"mtime", "btime", "atime", "tier", "md5chksum"}


def load(path: str) -> dict[str, dict]:
    with open(path, encoding="utf-8") as handle:
        items = json.load(handle)
    listing = {}
    for item in items:
        if item.get("IsDir"):
            continue
        metadata = {
            key.lower(): value
            for key, value in (item.get("Metadata") or {}).items()
            if key.lower() not in IGNORED
        }
        metadata.setdefault("content-type", item.get("MimeType", ""))
        listing[item["Path"]] = {"size": item.get("Size"), "metadata": metadata}
    return listing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("src")
    parser.add_argument("dst")
    parser.add_argument("--diff", required=True)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    src, dst = load(args.src), load(args.dst)
    differing = []
    for key in sorted(src):
        other = dst.get(key)
        if other is None:
            differing.append(key)
            print(f"missing in destination: {key}", file=sys.stderr)
        elif other != src[key]:
            differing.append(key)
            fields = [name for name in ("size", "metadata") if other[name] != src[key][name]]
            print(f"differs ({', '.join(fields)}): {key}", file=sys.stderr)
    extra = sorted(set(dst) - set(src))
    for key in extra:
        print(f"only in destination: {key}", file=sys.stderr)

    with open(args.diff, "w", encoding="utf-8") as handle:
        handle.writelines(f"{key}\n" for key in differing)

    print(
        f"{len(src)} source objects, {len(dst)} destination objects, "
        f"{len(differing)} to repair, {len(extra)} only in destination",
        file=sys.stderr,
    )
    if extra:
        return 1
    if differing and not args.report_only:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
