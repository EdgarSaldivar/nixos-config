"""Tests for manifests/backup_restore_objects.py (run by checks/backup-restore-objects.nix)."""

import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

DEFAULT_SCRIPT = Path(__file__).parents[1] / "manifests" / "backup_restore_objects.py"
SCRIPT = Path(os.environ.get("BACKUP_RESTORE_OBJECTS_SCRIPT", DEFAULT_SCRIPT))

USER = "0b6f2c1e-8d7a-4c1b-9a53-2f4e6d8c0a11"
CATALOG = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
SHA = "a" * 64
UPLOAD = f"uploads/{USER}/photo.jpg"
GOLDEN = f"internal/golden-truth/{CATALOG}/ref.png"
CROP = f"internal/crop-evidence/{SHA}/crop.webp"


def record(key, size, metadata, mime=None):
    """An objects.json value as rclone lsjson -R --metadata of Garage writes it."""
    entry = {
        "Path": key,
        "Name": key.rsplit("/", 1)[-1],
        "Size": size,
        "ModTime": "2026-09-28T03:31:02.123456789Z",
        "IsDir": False,
        "Tier": "STANDARD",
        "Metadata": metadata,
    }
    if mime is not None:
        entry["MimeType"] = mime
    return entry


def derived(extra):
    """Fields rclone's S3 backend adds to every listing, plus EXTRA."""
    return {
        "btime": "2026-09-01T10:00:00Z",
        "mtime": "2026-09-01T09:59:59.5Z",
        "tier": "STANDARD",
        **extra,
    }


INVENTORY = {
    UPLOAD: record(UPLOAD, 5, derived({"content-type": "image/jpeg", "owner-user-id": USER}), "image/jpeg"),
    GOLDEN: record(GOLDEN, 6, derived({"content-type": "image/png", "golden-truth-catalog-id": CATALOG}), "image/png"),
    CROP: record(CROP, 7, derived({"content-type": "image/webp", "crop-evidence-source-sha256": SHA}), "image/webp"),
}
EXPECTED = {
    UPLOAD: {"content-type": "image/jpeg", "owner-user-id": USER},
    GOLDEN: {"content-type": "image/png", "golden-truth-catalog-id": CATALOG},
    CROP: {"content-type": "image/webp", "crop-evidence-source-sha256": SHA},
}


def load_tool():
    spec = importlib.util.spec_from_file_location("backup_restore_objects", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = load_tool()


def write_objects(tmp_path, inventory=INVENTORY):
    path = tmp_path / "objects.json"
    path.write_text(json.dumps(inventory) if not isinstance(inventory, str) else inventory)
    return path


def write_index(tmp_path, inventory=INVENTORY):
    """The per-key index push hands to map, holding every key of INVENTORY."""
    index = tmp_path / "index"
    TOOL.write_index(str(index), inventory, sorted(inventory))
    return index


def run_mapper(index, request):
    env = {key: value for key, value in os.environ.items() if not key.startswith("RESTORE_")}
    if index is not None:
        env["RESTORE_OBJECTS_INDEX"] = str(index)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "map"],
        input=json.dumps(request) if not isinstance(request, str) else request,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def mapper_request(key, size, mime="application/octet-stream"):
    """What rclone sends for a file of the local mirror (fs.mapItem)."""
    return {
        "SrcFs": "/backup/restore-drill/backup/mirror",
        "SrcFsType": "local",
        "DstFs": "garage:pin-collector-uploads",
        "DstFsType": "s3",
        "Remote": key,
        "Size": size,
        "MimeType": mime,
        "ModTime": "2026-09-01T09:59:59.5Z",
        "IsDir": False,
        "Metadata": {"mode": "100644", "uid": "10001", "gid": "10001", "mtime": "2026-09-01T09:59:59.5Z"},
    }


@pytest.mark.parametrize("key", [UPLOAD, GOLDEN, CROP], ids=["uploads", "golden-truth", "crop-evidence"])
def test_mapper_returns_recorded_metadata_for_each_prefix(tmp_path, key):
    result = run_mapper(write_index(tmp_path), mapper_request(key, INVENTORY[key]["Size"]))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"Metadata": EXPECTED[key]}


def test_mapper_takes_content_type_from_mimetype_when_metadata_lacks_it(tmp_path):
    inventory = {UPLOAD: record(UPLOAD, 5, {"Owner-User-Id": USER, "btime": "x"}, "image/heic")}
    result = run_mapper(write_index(tmp_path, inventory), mapper_request(UPLOAD, 5, mime="image/jpeg"))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"Metadata": {"content-type": "image/heic", "owner-user-id": USER}}


def test_mapper_drops_derived_and_object_lock_fields(tmp_path):
    metadata = derived({"content-type": "image/jpeg", "owner-user-id": USER, "atime": "x",
                        "md5chksum": "y", "object-lock-mode": "GOVERNANCE", "cache-control": "no-cache"})
    inventory = {UPLOAD: record(UPLOAD, 5, metadata, "image/jpeg")}
    result = run_mapper(write_index(tmp_path, inventory), mapper_request(UPLOAD, 5))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["Metadata"] == {
        "content-type": "image/jpeg", "owner-user-id": USER, "cache-control": "no-cache",
    }


def test_mapper_fails_closed_on_a_key_missing_from_the_index(tmp_path):
    result = run_mapper(write_index(tmp_path), mapper_request(f"uploads/{USER}/other.jpg", 5))
    assert result.returncode != 0
    assert result.stdout == ""
    assert "no index entry" in result.stderr


@pytest.mark.parametrize("content", ["{not json", "[]", "", "null"], ids=["not-json", "list", "empty", "null"])
def test_mapper_fails_closed_on_a_corrupt_index_entry(tmp_path, content):
    index = write_index(tmp_path)
    (index / TOOL.index_name(UPLOAD)).write_text(content)
    result = run_mapper(index, mapper_request(UPLOAD, 5))
    assert result.returncode != 0
    assert result.stdout == ""


def test_mapper_fails_closed_on_an_index_entry_filed_under_another_key(tmp_path):
    index = write_index(tmp_path)
    (index / TOOL.index_name(UPLOAD)).write_text(json.dumps(INVENTORY[GOLDEN]))
    result = run_mapper(index, mapper_request(UPLOAD, 5))
    assert result.returncode != 0
    assert result.stdout == ""
    assert "no valid objects.json entry" in result.stderr


@pytest.mark.parametrize("index", [None, "missing"], ids=["unset", "missing-directory"])
def test_mapper_fails_without_a_usable_index(tmp_path, index):
    result = run_mapper(None if index is None else tmp_path / index, mapper_request(UPLOAD, 5))
    assert result.returncode != 0
    assert result.stdout == ""


def test_mapper_reads_only_its_own_index_entry(tmp_path, monkeypatch):
    # No objects.json anywhere, and every other entry unreadable: map must not need them.
    index = write_index(tmp_path)
    for key in (GOLDEN, CROP):
        (index / TOOL.index_name(key)).write_text("{corrupt")
    opened = []
    real_open = open

    def tracking_open(path, *args, **kwargs):
        opened.append(os.path.basename(str(path)))
        return real_open(path, *args, **kwargs)

    monkeypatch.setenv("RESTORE_OBJECTS_INDEX", str(index))
    monkeypatch.setattr("builtins.open", tracking_open)
    stdin, stdout = io.StringIO(json.dumps(mapper_request(UPLOAD, 5))), io.StringIO()
    assert TOOL.mapper(stdin, stdout) == 0
    monkeypatch.undo()
    assert json.loads(stdout.getvalue()) == {"Metadata": EXPECTED[UPLOAD]}
    assert opened == [TOOL.index_name(UPLOAD)]


@pytest.mark.parametrize(
    "entry",
    [
        record(UPLOAD, 5, {"owner-user-id": USER}),  # no Content-Type anywhere
        record(UPLOAD, 5, None, "image/jpeg"),  # Metadata not an object
        record(f"uploads/{USER}/elsewhere.jpg", 5, {"content-type": "image/jpeg"}),  # Path is not the key
        record(UPLOAD, 5, {"content-type": "image/jpeg", "owner-user-id": 7}),  # non-string value
    ],
    ids=["no-content-type", "no-metadata", "wrong-path", "non-string"],
)
def test_mapper_fails_closed_on_an_invalid_entry(tmp_path, entry):
    result = run_mapper(write_index(tmp_path, {UPLOAD: entry}), mapper_request(UPLOAD, 5))
    assert result.returncode != 0
    assert result.stdout == ""


def test_mapper_fails_when_the_file_size_differs_from_the_record(tmp_path):
    result = run_mapper(write_index(tmp_path), mapper_request(UPLOAD, 4))
    assert result.returncode != 0
    assert "objects.json records 5" in result.stderr


@pytest.mark.parametrize("remote", ["", "/abs", "../x", "uploads//x"])
def test_mapper_rejects_an_unusable_remote(tmp_path, remote):
    result = run_mapper(write_index(tmp_path), mapper_request(remote, 5))
    assert result.returncode != 0


def test_mapper_rejects_non_json_input(tmp_path):
    result = run_mapper(write_index(tmp_path), "not json")
    assert result.returncode != 0
    assert result.stdout == ""


# ── verify ───────────────────────────────────────────────────────────────────────────


def destination_item(key, size, metadata, mime):
    """rclone lsjson -R --metadata of the restored object on Garage."""
    return {"Path": key, "Size": size, "MimeType": mime, "IsDir": False,
            "Metadata": derived(metadata)}


def run_verify(tmp_path, listing, keys=(UPLOAD, GOLDEN, CROP)):
    objects = write_objects(tmp_path)
    keys_path = tmp_path / "keys.txt"
    keys_path.write_text("".join(f"{key}\n" for key in keys))
    listing_path = tmp_path / "listing.json"
    listing_path.write_text(json.dumps(listing))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "verify", "--objects", str(objects),
         "--keys", str(keys_path), "--listing", str(listing_path)],
        capture_output=True, text=True, check=False,
    )


def good_listing():
    return [
        destination_item(key, INVENTORY[key]["Size"], EXPECTED[key], EXPECTED[key]["content-type"])
        for key in (UPLOAD, GOLDEN, CROP)
    ] + [destination_item("uploads/unrelated/live.jpg", 9, {"content-type": "image/jpeg"}, "image/jpeg")]


def test_verify_passes_when_the_destination_matches(tmp_path):
    result = run_verify(tmp_path, good_listing())
    assert result.returncode == 0, result.stderr
    assert "3 keys checked, 0 problems" in result.stderr


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda items: items[0]["Metadata"].update({"content-type": "image/jpeg; charset=x"}), "metadata"),
        (lambda items: items[1]["Metadata"].pop("golden-truth-catalog-id"), "metadata"),
        (lambda items: items[2]["Metadata"].update({"mode": "100644"}), "metadata"),
        (lambda items: items[0].update({"Size": 4}), "size 4"),
        (lambda items: items.pop(2), "missing in destination"),
    ],
    ids=["content-type", "user-metadata-lost", "extra-metadata", "size", "missing"],
)
def test_verify_detects_a_mismatch(tmp_path, change, message):
    listing = good_listing()
    change(listing)
    result = run_verify(tmp_path, listing)
    assert result.returncode == 1
    assert message in result.stderr


# ── push, with a stand-in rclone that drives the mapper the way rclone does ───────────

FAKE_RCLONE = r'''
import csv, json, os, subprocess, sys
args = sys.argv[1:]
store = os.environ["FAKE_STORE"]
state = json.load(open(store)) if os.path.exists(store) else {}
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_LOG"] + ".env", "a") as log:
    log.write(json.dumps({k: v for k, v in os.environ.items() if k.startswith("RCLONE_CONFIG_GARAGE_")}) + "\n")
if args[0] == "copy":
    src, flags = args[1], args[3:]
    keys = open(flags[flags.index("--files-from-raw") + 1]).read().split()
    mapper = next(csv.reader([flags[flags.index("--metadata-mapper") + 1]], delimiter=" "))
    if os.environ.get("FAKE_CLOBBER"):
        open(os.environ["FAKE_CLOBBER"], "w").write("{clobbered")
    with open(os.environ["FAKE_LOG"] + ".mapenv", "a") as log:
        log.write(json.dumps({k: v for k, v in os.environ.items() if k.startswith("RESTORE_")}) + "\n")
    failed = False
    for key in keys:
        if "--ignore-existing" in flags and key in state:
            continue
        size = os.path.getsize(os.path.join(src, key))
        request = {"Remote": key, "Size": size, "MimeType": "application/octet-stream",
                   "IsDir": False, "Metadata": {"mode": "100644"}}
        out = subprocess.run(mapper, input=json.dumps(request), capture_output=True, text=True)
        if out.returncode != 0:
            sys.stderr.write(out.stderr)
            failed = True
            continue
        state[key] = {"Path": key, "Size": size, "IsDir": False,
                      "Metadata": dict(json.loads(out.stdout)["Metadata"], btime="now", mtime="then")}
    json.dump(state, open(store, "w"))
    sys.exit(1 if failed else 0)
if args[0] == "lsjson":
    json.dump(list(state.values()), sys.stdout)
if args[0] == "lsf":
    sys.stdout.write("".join(f"{key}\n" for key in sorted(state)))
'''


@pytest.fixture
def restored(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    write_objects(work)
    mirror = tmp_path / "mirror"
    for key in (UPLOAD, GOLDEN, CROP):
        path = mirror / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * INVENTORY[key]["Size"])
    fake = tmp_path / "rclone"
    fake.write_text(f"#!{sys.executable}\n{FAKE_RCLONE}")
    fake.chmod(0o755)
    return work, mirror, fake


def run_tool(tmp_path, *args, **extra_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RCLONE_", "RESTORE_", "FAKE_"))}
    env.update(FAKE_STORE=str(tmp_path / "store.json"), FAKE_LOG=str(tmp_path / "log"), **extra_env)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, check=False)


def run_push(tmp_path, work, mirror, fake, *extra, dest="fake:bucket", **extra_env):
    return run_tool(tmp_path, "push", "--work", str(work), "--mirror", str(mirror), "--rclone", str(fake),
                    "--dest", dest, "--tmp", str(tmp_path), *extra, **extra_env)


def logged(tmp_path):
    return [json.loads(line) for line in (tmp_path / "log").read_text().splitlines()]


def test_push_restores_every_key_with_its_recorded_metadata(tmp_path, restored):
    result = run_push(tmp_path, *restored)
    assert result.returncode == 0, result.stderr
    store = json.loads((tmp_path / "store.json").read_text())
    assert {key: {k: v for k, v in item["Metadata"].items() if k not in ("btime", "mtime")}
            for key, item in store.items()} == EXPECTED
    copy = logged(tmp_path)[0]
    assert copy[:3] == ["copy", str(restored[1]), "fake:bucket"]
    assert "--metadata" in copy and "--ignore-existing" in copy and "--ignore-times" not in copy


def test_push_maps_from_its_index_never_from_objects_json_and_removes_it(tmp_path, restored):
    work, mirror, fake = restored
    before = set(tmp_path.iterdir())
    # The stand-in rclone corrupts objects.json before it runs the mapper: every object must
    # still get its metadata, because map reads only the index push built after planning.
    result = run_push(tmp_path, work, mirror, fake, FAKE_CLOBBER=str(work / "objects.json"))
    assert result.returncode == 0, result.stderr
    store = json.loads((tmp_path / "store.json").read_text())
    assert {key: {k: v for k, v in item["Metadata"].items() if k not in ("btime", "mtime")}
            for key, item in store.items()} == EXPECTED
    (mapenv,) = [json.loads(line) for line in (tmp_path / "log.mapenv").read_text().splitlines()]
    assert list(mapenv) == ["RESTORE_OBJECTS_INDEX"]
    assert not os.path.exists(mapenv["RESTORE_OBJECTS_INDEX"])
    leftover = {path.name for path in set(tmp_path.iterdir()) - before}
    assert leftover == {"store.json", "log", "log.env", "log.mapenv"}


def test_push_keeps_an_existing_object_by_default_and_verify_reports_it(tmp_path, restored):
    store = tmp_path / "store.json"
    store.write_text(json.dumps({UPLOAD: {"Path": UPLOAD, "Size": 5, "Metadata": {"content-type": "image/jpeg"}}}))
    result = run_push(tmp_path, *restored)
    assert result.returncode == 1
    assert f"{UPLOAD}: metadata" in result.stderr
    result = run_push(tmp_path, *restored, "--overwrite")
    assert result.returncode == 0, result.stderr
    assert "--ignore-times" in logged(tmp_path)[-2]


def test_push_writes_nothing_when_a_mirror_file_has_no_record(tmp_path, restored):
    work, mirror, fake = restored
    stray = mirror / "uploads" / USER / "stray.jpg"
    stray.write_bytes(b"x")
    result = run_push(tmp_path, work, mirror, fake)
    assert result.returncode == 1
    assert "stray.jpg" in result.stderr
    assert not (tmp_path / "log").exists()
    result = run_push(tmp_path, work, mirror, fake, "--skip-unrecorded")
    assert result.returncode == 0, result.stderr
    assert "stray.jpg" not in json.loads((tmp_path / "store.json").read_text())


def test_push_restricts_to_keys_from(tmp_path, restored):
    work, mirror, fake = restored
    refs = work / "refs.txt"
    refs.write_text(f"{GOLDEN}\n")
    result = run_push(tmp_path, work, mirror, fake, "--keys-from", str(refs))
    assert result.returncode == 0, result.stderr
    assert list(json.loads((tmp_path / "store.json").read_text())) == [GOLDEN]
    refs.write_text(f"{GOLDEN}\nuploads/{USER}/gone.jpg\n")
    result = run_push(tmp_path, work, mirror, fake, "--keys-from", str(refs))
    assert result.returncode == 1
    assert "not files in the mirror" in result.stderr


def test_push_with_an_empty_key_list_is_a_clean_no_op(tmp_path, restored):
    work, mirror, fake = restored
    refs = work / "lost-keys.txt"
    refs.write_text("")
    result = run_push(tmp_path, work, mirror, fake, "--keys-from", str(refs))
    assert result.returncode == 0, result.stderr
    assert "nothing to restore (the key list is empty)" in result.stderr
    assert not (tmp_path / "log").exists()


def test_push_of_a_mirror_with_nothing_to_restore_fails(tmp_path, restored):
    work, _mirror, fake = restored
    empty = tmp_path / "empty-mirror"
    empty.mkdir()
    result = run_push(tmp_path, work, empty, fake)
    assert result.returncode == 1
    assert "no usable file in the mirror" in result.stderr
    assert not (tmp_path / "log").exists()


def test_push_refuses_a_malformed_objects_json(tmp_path, restored):
    work, mirror, fake = restored
    (work / "objects.json").write_text("{")
    result = run_push(tmp_path, work, mirror, fake)
    assert result.returncode == 2
    assert not (tmp_path / "log").exists()


def test_mapper_command_survives_rclone_quoting():
    command = TOOL.mapper_command('/py th"on', "/scripts/backup_restore_objects.py")
    assert next(csv.reader([command], delimiter=" ")) == ['/py th"on', "/scripts/backup_restore_objects.py", "map"]


def test_push_refuses_skip_unrecorded_with_keys_from(tmp_path, restored):
    work, mirror, fake = restored
    (mirror / "uploads" / USER / "stray.jpg").write_bytes(b"x")
    refs = work / "refs.txt"
    refs.write_text(f"{GOLDEN}\nuploads/{USER}/stray.jpg\n")
    result = run_push(tmp_path, work, mirror, fake, "--keys-from", str(refs), "--skip-unrecorded")
    assert result.returncode == 2
    assert "--skip-unrecorded cannot be combined with --keys-from" in result.stderr
    assert not (tmp_path / "log").exists()
    # Without the flag a named key that has no record stops the push before any write.
    result = run_push(tmp_path, work, mirror, fake, "--keys-from", str(refs))
    assert result.returncode == 1
    assert "stray.jpg: no valid objects.json entry" in result.stderr
    assert not (tmp_path / "log").exists()


KEY_ID = "GK0123456789abcdef01234567"
SECRET = "5ec7e7" * 10 + "abcd"


def garage_secrets(tmp_path, key_id=KEY_ID, secret=SECRET):
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    if key_id is not None:
        (secrets / "garage-app-key-id").write_text(key_id + "\n")
    if secret is not None:
        (secrets / "garage-app-secret").write_text(secret + "\n")
    return secrets


def test_push_hands_the_app_key_to_rclone_only_through_its_environment(tmp_path, restored):
    secrets = garage_secrets(tmp_path)
    result = run_push(tmp_path, *restored, "--secrets", str(secrets), dest="garage:pin-collector-uploads")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "log").read_text()
    assert [call[0] for call in logged(tmp_path)] == ["copy", "lsjson"]
    assert SECRET not in calls and KEY_ID not in calls
    assert SECRET not in result.stdout + result.stderr
    for line in (tmp_path / "log.env").read_text().splitlines():
        env = json.loads(line)
        assert env["RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID"] == KEY_ID
        assert env["RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY"] == SECRET
        assert env["RCLONE_CONFIG_GARAGE_ENDPOINT"] == "http://garage:3900"
        assert env["RCLONE_CONFIG_GARAGE_TYPE"] == "s3"


@pytest.mark.parametrize(
    "key_id, secret",
    [(KEY_ID, None), (None, SECRET), (KEY_ID, ""), (None, None)],
    ids=["no-secret-file", "no-key-id-file", "empty-secret", "no-files"],
)
def test_push_fails_closed_without_the_app_key(tmp_path, restored, key_id, secret):
    secrets = garage_secrets(tmp_path, key_id, secret)
    result = run_push(tmp_path, *restored, "--secrets", str(secrets), dest="garage:pin-collector-uploads")
    assert result.returncode == 2
    assert "Garage app key" in result.stderr
    assert SECRET not in result.stdout + result.stderr and KEY_ID not in result.stdout + result.stderr
    assert not (tmp_path / "log").exists()


def test_list_prints_the_destination_keys_with_the_app_key(tmp_path, restored):
    (tmp_path / "store.json").write_text(json.dumps({GOLDEN: {}, UPLOAD: {}}))
    secrets = garage_secrets(tmp_path)
    result = run_tool(tmp_path, "list", "--rclone", str(restored[2]), "--secrets", str(secrets),
                      "--tmp", str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == sorted([GOLDEN, UPLOAD])
    assert logged(tmp_path) == [["lsf", "-R", "--files-only", "garage:pin-collector-uploads"]]
    assert SECRET not in (tmp_path / "log").read_text()


@pytest.mark.parametrize("mime, code", [("image/jpeg", 0), ("application/octet-stream", 1)],
                         ids=["match", "mismatch"])
def test_verify_takes_content_type_from_mimetype_when_metadata_lacks_it(tmp_path, mime, code):
    listing = good_listing()
    del listing[0]["Metadata"]["content-type"]
    listing[0]["MimeType"] = mime
    result = run_verify(tmp_path, listing)
    assert result.returncode == code, result.stderr
    if code:
        assert f'{UPLOAD}: metadata {{"content-type": "application/octet-stream"' in result.stderr
