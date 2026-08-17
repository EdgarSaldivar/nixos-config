#!/usr/bin/env python3
"""Transactionally reconcile Wolf's two reviewed player profiles."""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import json
import os
import re
import signal
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import tomlkit


ROOT_KEYS = {"hostname", "uuid", "config_version", "paired_clients", "profiles", "gstreamer"}
PROFILE_RULES = {
    "moonlight-profile-id": ({"Wolf UI": (1, 1), "Test ball": (0, 1)}, None),
    "user": (
        {
            "Steam": (1, 1),
            "Desktop (xfce)": (1, 1),
            "Firefox": (0, 1),
            "RetroArch": (0, 1),
            "Pegasus": (0, 1),
            "Lutris": (0, 1),
            "Prismlauncher": (0, 1),
            "EmulationStation": (0, 1),
            "Kodi": (0, 1),
        },
        "Edgar",
    ),
    "guest": ({"Steam": (1, 1), "Desktop (xfce)": (1, 1)}, "Guest"),
}
PINNED_IMAGE = re.compile(r"^[^@]+@sha256:[0-9a-f]{64}$")


class PolicyError(RuntimeError):
    pass


class DurabilityError(PolicyError):
    pass


def plain(value: Any) -> Any:
    return value.unwrap() if hasattr(value, "unwrap") else value


def fail(message: str) -> None:
    raise PolicyError(message)


def require_shape(value: Any, expected: Any, where: str) -> None:
    """Require the recursively reviewed table/list/scalar shape."""
    value, expected = plain(value), plain(expected)
    if isinstance(expected, dict):
        if not isinstance(value, dict) or set(value) != set(expected):
            fail(f"{where} has an unexpected table shape")
        for key in expected:
            require_shape(value[key], expected[key], f"{where}.{key}")
    elif isinstance(expected, list):
        if not isinstance(value, list):
            fail(f"{where} must be an array")
        if not expected and value:
            fail(f"{where} must remain empty")
        if expected:
            for index, item in enumerate(value):
                if not any(shape_matches(item, model) for model in expected):
                    fail(f"{where}[{index}] has an unexpected shape")
    elif type(value) is not type(expected):
        fail(f"{where} has the wrong scalar type")


def strict_equal(left: Any, right: Any) -> bool:
    """Recursive equality that refuses Python's bool/int conflation.

    Python holds `True == 1` and `False == 0`, so a plain `!=` accepts a
    reviewed `start_virtual_compositor = true` rewritten as integer `1`, or a
    `"Privileged": false` flipped to `0` inside base_create_json. The Nix
    validator distinguishes those values, so tolerating them here would open a
    policy-parity bypass: a config the Python layer blesses and the Nix layer
    rejects, with the malformed field reaching runtime.
    """
    if isinstance(left, dict) or isinstance(right, dict):
        if not (isinstance(left, dict) and isinstance(right, dict)):
            return False
        if set(left) != set(right):
            return False
        return all(strict_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) or isinstance(right, list):
        if not (isinstance(left, list) and isinstance(right, list)):
            return False
        if len(left) != len(right):
            return False
        return all(strict_equal(item, other) for item, other in zip(left, right))
    if type(left) is not type(right):
        return False
    return bool(left == right)


def shape_matches(value: Any, expected: Any) -> bool:
    try:
        require_shape(value, expected, "value")
        return True
    except PolicyError:
        return False


def validate_paired_clients(value: Any) -> None:
    value = plain(value)
    if not isinstance(value, list):
        fail("paired_clients must be an array")
    for index, client in enumerate(value):
        if not isinstance(client, dict) or not client:
            fail(f"paired_clients[{index}] must be a non-empty table")
        if not all(isinstance(key, str) for key in client):
            fail(f"paired_clients[{index}] has a non-string key")


def validate_root(doc: Any, template: Any) -> None:
    if set(doc) != ROOT_KEYS:
        fail(f"root keys must be exactly {sorted(ROOT_KEYS)}")
    if plain(doc["config_version"]) != 7 or type(plain(doc["config_version"])) is not int:
        fail("config_version must be 7")
    # `hostname` is what Moonlight displays for this host. It was previously
    # only required to be PRESENT, so a list, table, integer or boolean passed
    # validation, was preserved verbatim, and could reach Wolf as an unusable
    # configuration.
    raw_hostname = plain(doc["hostname"])
    if type(raw_hostname) is not str or not raw_hostname.strip():
        fail("hostname must be a non-empty string")
    raw_uuid = plain(doc["uuid"])
    if not isinstance(raw_uuid, str):
        fail("uuid must be a string")
    try:
        uuid.UUID(raw_uuid)
    except (ValueError, AttributeError):
        fail("uuid must be a valid UUID string")
    validate_paired_clients(doc["paired_clients"])
    require_shape(doc["gstreamer"], template["gstreamer"], "gstreamer")


def normalized_app(app: Any) -> dict[str, Any]:
    result = copy.deepcopy(plain(app))
    result.pop("video", None)
    result.pop("audio", None)
    runner = result.get("runner")
    if isinstance(runner, dict) and "base_create_json" in runner:
        try:
            runner["base_create_json"] = json.loads(runner["base_create_json"])
        except (TypeError, json.JSONDecodeError) as error:
            fail(f"invalid base_create_json: {error}")
    return result


def app_map(template: Any) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for profile in template["profiles"]:
        profile_id = plain(profile["id"])
        result[profile_id] = {
            plain(app["title"]): normalized_app(app) for app in profile.get("apps", [])
        }
    return result


def raw_app_map(template: Any) -> dict[str, dict[str, Any]]:
    """The reviewed app tables themselves, overrides and trivia intact."""
    result: dict[str, dict[str, Any]] = {}
    for profile in template["profiles"]:
        result[plain(profile["id"])] = {
            plain(app["title"]): app for app in profile.get("apps", [])
        }
    return result


def restore_reviewed_apps(doc: Any, template: Any) -> list[str]:
    """Reconcile drifted reviewed apps back to their template definition.

    Wolf owns config.toml at runtime. Its /apps/add and /profiles/add endpoints
    rewrite the whole document and DROP per-app `video`/`audio` override
    tables — and the reviewed Test ball app carries both. Failing closed on
    that drift would mean ExecStartPre exits 1 and Wolf never starts again
    without manual config surgery, which is a robustness REGRESSION against the
    substring checks this replaced.

    Restoring the reviewed definition is precisely what a reconciler is for.
    Drift we cannot safely auto-correct is deliberately NOT handled here and
    still fails closed in validate_apps: unknown profile ids, apps outside the
    total allowlist, cross-profile writable mounts and named-volume violations.
    Restoring an unreviewed app would be indistinguishable from laundering an
    injected one, so those are left to be rejected.
    """
    reviewed = raw_app_map(template)
    restored: list[str] = []
    for profile in doc["profiles"]:
        profile_id = plain(profile.get("id"))
        if profile_id not in PROFILE_RULES or profile_id not in reviewed:
            continue
        rules, _display_name = PROFILE_RULES[profile_id]
        apps = profile.get("apps")
        if apps is None:
            continue
        for app in apps:
            title = plain(app.get("title"))
            if title not in rules or title not in reviewed[profile_id]:
                continue
            # Restore ONLY the override tables Wolf's app-add endpoint drops.
            # Repairing the whole app body would also repair hostile drift —
            # an unpinned image, a guest named volume, a cross-profile writable
            # mount — laundering it into a valid config and silencing exactly
            # the rejections validate_apps exists to make. Overrides are
            # encoder/display settings whose shape validate_override already
            # constrains, so reinstating a missing one cannot smuggle anything.
            # A present-but-edited override is deliberate variation; leave it.
            for media in ("video", "audio"):
                if media in reviewed[profile_id][title] and media not in app:
                    app[media] = copy.deepcopy(reviewed[profile_id][title][media])
                    restored.append(f"{profile_id}/{title}.{media}")
    return restored


def split_mount(mount: str) -> tuple[str, str]:
    parts = mount.rsplit(":", 2)
    if len(parts) == 3 and parts[2] in {"ro", "rw"}:
        return parts[0], parts[2]
    if len(parts) == 2:
        return parts[0], "rw"
    fail(f"malformed mount {mount!r}")


def validate_apps(doc: Any, template: Any, paths: dict[str, Any]) -> None:
    expected = app_map(template)
    profiles: dict[str, Any] = {}
    for profile in doc["profiles"]:
        profile_id = plain(profile.get("id"))
        if profile_id in profiles:
            fail(f"duplicate profile id {profile_id!r}")
        if profile_id not in PROFILE_RULES:
            fail(f"unknown profile id {profile_id!r}")
        profiles[profile_id] = profile
    if set(profiles) != set(PROFILE_RULES):
        fail("the moonlight, user, and guest profiles are all required")

    named_owners: dict[str, str] = {}
    owned_roots = {
        profile_id: tuple(paths[profile_id][key] for key in ("steamapps", "nonsteam", "mods"))
        for profile_id in ("user", "guest")
    }
    for profile_id, profile in profiles.items():
        rules, display_name = PROFILE_RULES[profile_id]
        if display_name is not None and plain(profile.get("name")) != display_name:
            fail(f"profile {profile_id!r} must be named {display_name!r}")
        apps = profile.get("apps", [])
        titles = [plain(app.get("title")) for app in apps]
        if len(titles) != len(set(titles)):
            fail(f"profile {profile_id!r} contains duplicate app titles")
        if any(title not in rules for title in titles):
            fail(f"profile {profile_id!r} contains an unreviewed app")
        for title, (minimum, maximum) in rules.items():
            count = titles.count(title)
            if not minimum <= count <= maximum:
                fail(f"profile {profile_id!r} has invalid multiplicity for {title!r}")
        for app in apps:
            title = plain(app["title"])
            for media in ("video", "audio"):
                if media in app:
                    validate_override(app[media], f"profile {profile_id!r} app {title!r}.{media}")
            if not strict_equal(normalized_app(app), expected[profile_id][title]):
                fail(f"profile {profile_id!r} app {title!r} differs from its reviewed identity")
            runner = plain(app).get("runner", {})
            image = runner.get("image")
            if image is not None and not PINNED_IMAGE.fullmatch(image):
                fail(f"profile {profile_id!r} app {title!r} has an unpinned image")
            for mount in runner.get("mounts", []):
                source, mode = split_mount(mount)
                if not source.startswith("/"):
                    allowed = paths.get(profile_id, {}).get("namedVolumes", [])
                    if source not in allowed:
                        fail(f"profile {profile_id!r} may not use named volume {source!r}")
                    old_owner = named_owners.setdefault(source, profile_id)
                    if old_owner != profile_id:
                        fail(f"named volume {source!r} is shared across profiles")
                elif mode == "rw" and profile_id in owned_roots:
                    for owner, roots in owned_roots.items():
                        if owner != profile_id and any(source == root or source.startswith(root + "/") for root in roots):
                            fail(f"profile {profile_id!r} writes profile {owner!r} storage")


def validate_override(value: Any, where: str) -> None:
    value = plain(value)
    if not isinstance(value, dict) or not value:
        fail(f"{where} must be a non-empty table")
    for key, child in value.items():
        if not isinstance(key, str):
            fail(f"{where} has a non-string key")
        if isinstance(child, dict):
            validate_override(child, f"{where}.{key}")
        elif isinstance(child, list):
            if any(isinstance(item, (dict, list)) for item in child):
                fail(f"{where}.{key} must contain scalar values")
        elif not isinstance(child, (str, int, float, bool)):
            fail(f"{where}.{key} has an unsupported value")


def protected_spans(text: str) -> dict[str, Any]:
    """Extract byte-sensitive state that reconciliation is forbidden to edit."""
    lines = text.splitlines(keepends=True)
    spans: dict[str, Any] = {"uuid": [], "paired_clients": [], "overrides": [], "comments": []}
    # Comments are protected independently of the nodes they decorate. Wolf's
    # reviewed strings contain no literal '#', so retaining the exact suffix
    # also covers inline comments without mistaking URLs or image digests.
    spans["comments"] = [line[line.index("#"):] for line in lines if "#" in line]
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if re.match(r"^uuid\s*=", stripped):
            spans["uuid"].append(line)
        if re.match(r"^paired_clients\s*=", stripped):
            block = [line]
            balance = line.count("[") - line.count("]")
            while balance > 0 and index + 1 < len(lines):
                index += 1
                block.append(lines[index])
                balance += lines[index].count("[") - lines[index].count("]")
            spans["paired_clients"].append("".join(block))
        if re.match(r"^\[profiles\.apps\.(video|audio)\]$", stripped):
            block = [line]
            cursor = index + 1
            while cursor < len(lines) and not lines[cursor].lstrip().startswith("["):
                block.append(lines[cursor])
                cursor += 1
            spans["overrides"].append("".join(block))
            index = cursor - 1
        index += 1
    return spans


def verify_preservation(before_text: str, before: Any, after_text: str, after: Any) -> None:
    for key in ("uuid", "paired_clients"):
        if plain(before[key]) != plain(after[key]):
            fail(f"semantic preservation failed for {key}")
    after_profiles = {plain(profile["id"]): profile for profile in after["profiles"]}
    for profile in before["profiles"]:
        profile_id = plain(profile["id"])
        after_apps = {
            plain(app["title"]): app for app in after_profiles[profile_id].get("apps", [])
        }
        for app in profile.get("apps", []):
            title = plain(app["title"])
            for media in ("video", "audio"):
                original = plain(app.get(media))
                # An override PRESENT in the source is the operator's and must
                # survive byte-for-byte. An ABSENT one may be reinstated from
                # the reviewed template: that is the restoration path which
                # stops Wolf's own app-add rewrite from bricking startup, and
                # it adds reviewed content rather than altering anyone's.
                if original is None:
                    continue
                if original != plain(after_apps[title].get(media)):
                    fail("semantic preservation failed for app video/audio overrides")
    old_spans, new_spans = protected_spans(before_text), protected_spans(after_text)
    for kind in ("uuid", "paired_clients"):
        if old_spans[kind] != new_spans[kind]:
            fail(f"textual preservation failed for {kind}")
    # Restoration may ADD an override block, so this is a subset check rather
    # than equality — but every block the source carried must still be there,
    # verbatim.
    if any(span not in new_spans["overrides"] for span in old_spans["overrides"]):
        fail("textual preservation failed for overrides")
    remaining = iter(new_spans["comments"])
    if not all(any(candidate == comment for candidate in remaining) for comment in old_spans["comments"]):
        fail("textual preservation failed for comments")


def replace_known_images(doc: Any, rewrites: dict[str, str]) -> None:
    for profile in doc["profiles"]:
        for app in profile.get("apps", []):
            runner = app.get("runner")
            if runner is not None and plain(runner.get("image")) in rewrites:
                assign_preserving_trivia(runner, "image", rewrites[plain(runner["image"])])


def assign_preserving_trivia(container: Any, key: str, value: Any) -> None:
    old = container.get(key)
    replacement = tomlkit.item(value)
    if old is not None and hasattr(old, "trivia") and hasattr(replacement, "trivia"):
        replacement._trivia = copy.copy(old.trivia)
        if hasattr(old, "_t") and hasattr(replacement, "_t"):
            replacement._t = old._t
    container[key] = replacement


def reconcile_legacy_user(doc: Any, template: Any, paths: dict[str, Any]) -> None:
    profiles = {plain(profile["id"]): profile for profile in doc["profiles"]}
    if "guest" not in profiles:
        guest = next(profile for profile in template["profiles"] if plain(profile["id"]) == "guest")
        doc["profiles"].append(copy.deepcopy(guest))
    user = profiles.get("user")
    if user is None:
        fail("user profile is missing")
    # "User" is the single reviewed pre-two-player display-name migration.
    # Missing or arbitrary names remain candidate-validation failures.
    if plain(user.get("name")) == "User":
        assign_preserving_trivia(user, "name", "Edgar")
    apps = {plain(app.get("title")): app for app in user.get("apps", [])}
    if "Steam" not in apps or "Desktop (xfce)" not in apps:
        fail("user Steam and XFCE apps are required")

    allocator = "/run/opengl-driver/lib/libnvidia-allocator.so.1:/usr/lib/x86_64-linux-gnu/libnvidia-allocator.so.1:ro"
    steam = apps["Steam"]["runner"]
    steam_env = plain(steam.get("env", []))
    required_env = [
        "PROTON_LOG=1", "RUN_SWAY=true", "GOW_REQUIRED_DEVICES=/dev/input/* /dev/dri/* /dev/nvidia*",
        "__EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/10_nvidia.json:/usr/share/glvnd/egl_vendor.d/50_mesa.json",
        "STEAM_DIR=/home/retro/.steam/steam",
    ]
    if steam_env in [required_env[:3], required_env[:4]]:
        steam["env"] = copy.deepcopy(required_env)
    old_mounts = plain(steam.get("mounts", []))
    p = paths["user"]
    current_mounts = [
        p["mounts"]["steamLibrary"], p["mounts"]["graphicalSteamLibrary"],
        p["mounts"]["nonsteam"], p["mounts"]["mods"], p["mounts"]["modding"], allocator,
    ]
    legacy_mounts = [
        [],
        [f'{p["steamapps"]}:/home/retro/.steam/debian-installation/steamapps:rw', p["mounts"]["mods"], allocator],
        [p["mounts"]["steamLibrary"], p["mounts"]["mods"], allocator],
        [p["mounts"]["steamLibrary"], f'{p["steamapps"]}:/home/retro/Games/Steam:rw', p["mounts"]["mods"], allocator],
    ]
    if old_mounts in legacy_mounts:
        steam["mounts"] = copy.deepcopy(current_mounts)

    xfce = apps["Desktop (xfce)"]["runner"]
    xfce_env = plain(xfce.get("env", []))
    required_xfce_env = [
        "GOW_REQUIRED_DEVICES=/dev/input/* /dev/dri/* /dev/nvidia*",
        "STEAM_DIR=/home/retro/Games/Steam",
    ]
    if xfce_env == required_xfce_env[:1]:
        xfce["env"] = copy.deepcopy(required_xfce_env)
    old_xfce_mounts = plain(xfce.get("mounts", []))
    current_xfce_mounts = [
        p["mounts"]["graphicalSteamLibrary"], p["mounts"]["nonsteam"],
        p["mounts"]["modding"], p["mounts"]["downloads"],
    ]
    legacy_xfce_mounts = [
        [],
        [f'{p["steamapps"]}:/home/retro/Games/Steam:rw', p["mounts"]["modding"], p["mounts"]["downloads"]],
    ]
    if old_xfce_mounts in legacy_xfce_mounts:
        xfce["mounts"] = copy.deepcopy(current_xfce_mounts)


def parse(text: str, label: str) -> Any:
    try:
        return tomlkit.parse(text)
    except Exception as error:
        fail(f"cannot parse {label}: {error}")


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_live(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_directory(path.parent)


def apply_metadata(fd: int, metadata: os.stat_result | None) -> None:
    if metadata is None:
        os.fchmod(fd, 0o600)
        return
    os.fchmod(fd, stat.S_IMODE(metadata.st_mode))
    try:
        os.fchown(fd, metadata.st_uid, metadata.st_gid)
    except PermissionError:
        current = os.fstat(fd)
        if (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
            raise


def write_temp(parent: Path, data: bytes, metadata: os.stat_result | None) -> tuple[Path, int]:
    fd, raw_path = tempfile.mkstemp(prefix=".config.toml.", dir=parent)
    path = Path(raw_path)
    try:
        apply_metadata(fd, metadata)
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        return path, fd
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise


def atomic_backup(target: Path, data: bytes, metadata: os.stat_result | None, immutable: bool) -> None:
    temp, fd = write_temp(target.parent, data, metadata)
    try:
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if immutable:
            try:
                os.link(temp, target)
            except FileExistsError:
                existing_fd = os.open(target, os.O_RDONLY)
                try:
                    os.fsync(existing_fd)
                finally:
                    os.close(existing_fd)
                fsync_directory(target.parent)
                return
        else:
            os.replace(temp, target)
        fsync_directory(target.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


class TerminatedError(PolicyError):
    """SIGTERM/SIGINT delivered mid-transaction."""


@contextlib.contextmanager
def transaction_lock(config_path: Path):
    """Serialize the entire read-modify-write against concurrent writers.

    Wolf owns config.toml at runtime and rewrites the whole document to persist
    pairing. Without a lock, a Wolf write landing between our read and our
    rename is silently clobbered by the stale candidate — losing paired
    clients. Two first-run reconcilers could likewise mint different UUIDs and
    race to replace each other's output.
    """
    lock_path = config_path.with_name("config.toml.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def termination_guard():
    """Turn SIGTERM/SIGINT into exceptions so `finally` blocks still run.

    Python's default SIGTERM terminates the interpreter outright, skipping
    cleanup entirely: temp files survive and a rename can be left unsynced with
    no durability failure reported. systemd sends SIGTERM on stop, so this is
    the ordinary path, not an exotic one.
    """

    def terminate(signum: int, _frame: Any) -> None:
        raise TerminatedError(f"terminated by signal {signum}")

    previous: dict[int, Any] = {}
    for number in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[number] = signal.signal(number, terminate)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for number, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(number, handler)


def live_matches(path: Path, expected: bytes) -> bool:
    """Observe commit state from the filesystem rather than assignment order.

    Setting a `committed` flag after os.replace() leaves a window in which a
    signal arrives with the rename already done but the flag still False — the
    handler would then report a pre-commit failure while a candidate is live.
    The filesystem is the only authority on whether the rename happened.
    """
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def run(template_path: Path, config_path: Path, paths: dict[str, Any], rewrites: dict[str, str]) -> bool:
    # The lock lives beside config.toml, so cfg/ must exist before anything can
    # be serialized. Creating it here also closes the fresh-install durability
    # gap: a new directory entry only becomes durable once ITS parent is synced.
    created_parent = not config_path.parent.exists()
    config_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if created_parent:
        fsync_directory(config_path.parent)
        fsync_directory(config_path.parent.parent)
    with transaction_lock(config_path), termination_guard():
        return _reconcile(template_path, config_path, paths, rewrites)


def _reconcile(template_path: Path, config_path: Path, paths: dict[str, Any], rewrites: dict[str, str]) -> bool:
    template_text = template_path.read_text()
    template = parse(template_text, "template")
    exists = config_path.exists()
    if exists:
        source_text = config_path.read_text()
        metadata = config_path.stat()
    else:
        source_text = f'# A unique identifier for this host\nuuid = "{uuid.uuid4()}"\n' + template_text
        metadata = None
    source = parse(source_text, "live config")
    validate_root(source, template)

    candidate = copy.deepcopy(source)
    replace_known_images(candidate, rewrites)
    reconcile_legacy_user(candidate, template, paths)
    for entry in restore_reviewed_apps(candidate, template):
        print(f"Wolf config: restored reviewed app {entry}", file=sys.stderr)
    validate_root(candidate, template)
    validate_apps(candidate, template, paths)
    candidate_text = tomlkit.dumps(candidate)
    reparsed = parse(candidate_text, "serialized candidate")
    validate_root(reparsed, template)
    validate_apps(reparsed, template, paths)
    if exists:
        verify_preservation(source_text, source, candidate_text, reparsed)

    encoded = candidate_text.encode()
    if exists and encoded == source_text.encode():
        sync_live(config_path)
        return False

    temp, fd = write_temp(config_path.parent, encoded, metadata)
    try:
        backup_data = source_text.encode()
        atomic_backup(config_path.with_name("config.toml.pre-two-player"), backup_data, metadata, True)
        atomic_backup(config_path.with_name("config.toml.bak"), backup_data, metadata, False)
        os.fsync(fd)
        if os.environ.get("WOLF_RECONCILE_FAILPOINT") == "before-rename":
            fail("interrupted before rename")
        os.close(fd)
        fd = -1
        # Compare-and-swap. The lock excludes other reconcilers, but Wolf owns
        # this file too; if its bytes changed since we read them, our candidate
        # is stale and replacing it would silently discard that write.
        if exists and config_path.read_bytes() != source_text.encode():
            fail("config.toml changed during reconciliation; aborting rather than clobbering it")
        if os.environ.get("WOLF_RECONCILE_FAILPOINT") == "at-rename":
            raise TerminatedError("signal delivered in the rename window")
        os.replace(temp, config_path)
        if os.environ.get("WOLF_RECONCILE_FAILPOINT") == "after-rename":
            raise DurabilityError("interrupted after rename; candidate is live")
        sync_live(config_path)
        return True
    except BaseException as error:
        # Commit state is read from the filesystem, never from a flag set after
        # os.replace() — a signal landing between the syscall and that
        # assignment would otherwise be reported as a pre-commit failure while
        # the candidate was already live.
        if not isinstance(error, DurabilityError) and live_matches(config_path, encoded):
            raise DurabilityError(f"candidate is live but durability sync failed: {error}") from error
        raise
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--paths-json", required=True, type=Path)
    parser.add_argument("--rewrites-json", required=True, type=Path)
    args = parser.parse_args()
    try:
        run(
            args.template,
            args.config,
            json.loads(args.paths_json.read_text()),
            json.loads(args.rewrites_json.read_text()),
        )
        return 0
    except DurabilityError as error:
        print(f"Wolf config durability failure: {error}", file=sys.stderr)
        return 1
    except (PolicyError, OSError) as error:
        print(f"Wolf config policy failure: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
