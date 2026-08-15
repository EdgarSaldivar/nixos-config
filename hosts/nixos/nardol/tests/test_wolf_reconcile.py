from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest
import tomlkit


NARDOL = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("wolf_reconcile", NARDOL / "wolf-reconcile.py")
reconcile = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconcile
SPEC.loader.exec_module(reconcile)

PATHS = {
    "user": {
        "steamapps": "/srv/games/steamapps",
        "nonsteam": "/srv/games/nonsteam",
        "mods": "/srv/mods",
        "namedVolumes": ["lutris"],
    },
    "guest": {
        "steamapps": "/srv/games/guest-steamapps",
        "nonsteam": "/srv/games/guest-nonsteam",
        "mods": "/srv/mods-guest",
        "namedVolumes": [],
    },
}
for owned in PATHS.values():
    owned["mounts"] = {
        "steamLibrary": f'{owned["steamapps"]}:/home/retro/.steam/steam/steamapps:rw',
        "graphicalSteamLibrary": f'{owned["steamapps"]}:/home/retro/Games/Steam/steamapps:rw',
        "nonsteam": f'{owned["nonsteam"]}:/home/retro/Games/NonSteam:rw',
        "mods": f'{owned["mods"]}:/home/retro/Mods:rw',
        "modding": f'{owned["mods"]}:/home/retro/Modding:rw',
        "downloads": f'{owned["mods"]}/downloads:/home/retro/Downloads:rw',
    }

PINS = {
    "ghcr.io/games-on-whales/es-de:edge": "ghcr.io/games-on-whales/es-de@sha256:f5d1037e9dd6ff7406e190e00457152d0a9dcb4adbc32fe2132585cb5bbe7829",
    "ghcr.io/games-on-whales/firefox:edge": "ghcr.io/games-on-whales/firefox@sha256:1ea7331934d31d346079fb67462b371586d65b5ebb792acee8c0e64e87c185b1",
    "ghcr.io/games-on-whales/kodi:edge": "ghcr.io/games-on-whales/kodi@sha256:e3db2ca9492b85f98c253436c22d47c841009d278b7e8dc7f3f349aca2ebfe8a",
    "ghcr.io/games-on-whales/lutris:edge": "ghcr.io/games-on-whales/lutris@sha256:207005d9e1a839814c7c2b91fa25190d40c388c7dc004eec593556bd807f99f2",
    "ghcr.io/games-on-whales/pegasus:edge": "ghcr.io/games-on-whales/pegasus@sha256:29e7ab082f1c73a92ff25dff66983a83790d109ea49e0826cb0279b7fe5eacd8",
    "ghcr.io/games-on-whales/prismlauncher:edge": "ghcr.io/games-on-whales/prismlauncher@sha256:e2c610f666b019a2e31482641cab6c3330a24add41fd88f939a15f327bf9dda0",
    "ghcr.io/games-on-whales/retroarch:edge": "ghcr.io/games-on-whales/retroarch@sha256:bbcf4523e589fc7177b522ce56ba9507c6530caaf1999e37b37062a189f18cf2",
    "ghcr.io/games-on-whales/wolf-ui:main": "ghcr.io/games-on-whales/wolf-ui@sha256:cd6de1158b29068e4a4d4ce6312976067517239be97200a391be758a6ddfcf9b",
    "ghcr.io/games-on-whales/xfce:edge": "ghcr.io/games-on-whales/xfce@sha256:2ce1db7432bcb60caf5b3da23ea0ad5a24f300f3e7f346045fd6ba74a477ebcd",
    "ghcr.io/edgarsaldivar/nardol-steam-tools:git-214fce8091fc0524d64996a3b225ee3a98251c36": "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8",
    "ghcr.io/games-on-whales/steam:edge": "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8",
    "ghcr.io/games-on-whales/steam@sha256:ded0b1b47acd9adb8af9f068342f26ac31008904d9bbb91045d1a04e7d66a632": "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8",
}


@pytest.fixture
def template(tmp_path):
    text = (NARDOL / "wolf-config.template.toml").read_text()
    for source, target in PINS.items():
        text = text.replace(source, target)
    path = tmp_path / "template.toml"
    path.write_text(text)
    return path


def old_text(template: Path) -> str:
    doc = tomlkit.parse(template.read_text())
    del doc["profiles"][-1]
    profile(doc, "user")["name"] = "User"
    return '# A unique identifier for this host\nuuid = "12345678-1234-4234-9234-123456789abc"\n' + tomlkit.dumps(doc)


def profile(doc, profile_id):
    return next(item for item in doc["profiles"] if item["id"] == profile_id)


def app(doc, profile_id, title):
    return next(item for item in profile(doc, profile_id)["apps"] if item["title"] == title)


def write_doc(path: Path, doc) -> bytes:
    value = tomlkit.dumps(doc).encode()
    path.write_bytes(value)
    return value


def assert_rejected(template: Path, config: Path, mutate):
    doc = tomlkit.parse(config.read_text())
    mutate(doc)
    before = write_doc(config, doc)
    with pytest.raises(reconcile.PolicyError):
        reconcile.run(template, config, PATHS, PINS)
    assert config.read_bytes() == before


def test_fresh_generation_is_valid_and_transactional(template, tmp_path):
    config = tmp_path / "fresh" / "config.toml"
    assert reconcile.run(template, config, PATHS, PINS)
    data = tomllib.loads(config.read_text())
    assert [item["id"] for item in data["profiles"]] == ["moonlight-profile-id", "user", "guest"]
    assert config.stat().st_mode & 0o777 == 0o600
    assert (config.parent / "config.toml.pre-two-player").exists()
    assert (config.parent / "config.toml.bak").exists()


def test_existing_one_player_path_backups_and_byte_stable_second_run(template, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    original = config.read_bytes()
    assert reconcile.run(template, config, PATHS, PINS)
    immutable = (tmp_path / "config.toml.pre-two-player").read_bytes()
    assert immutable == original == (tmp_path / "config.toml.bak").read_bytes()
    first = config.read_bytes()
    assert not reconcile.run(template, config, PATHS, PINS)
    assert config.read_bytes() == first
    assert (tmp_path / "config.toml.pre-two-player").read_bytes() == immutable


@pytest.mark.parametrize("image", [
    "ghcr.io/games-on-whales/steam:edge",
    "ghcr.io/games-on-whales/steam@sha256:ded0b1b47acd9adb8af9f068342f26ac31008904d9bbb91045d1a04e7d66a632",
])
@pytest.mark.parametrize("variant", range(4))
def test_legacy_images_env_and_mount_forms_are_normalized(template, tmp_path, image, variant):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    doc = tomlkit.parse(config.read_text())
    steam = app(doc, "user", "Steam")["runner"]
    steam["image"] = image
    steam["env"] = list(steam["env"][: 3 + variant % 2])
    allocator = "/run/opengl-driver/lib/libnvidia-allocator.so.1:/usr/lib/x86_64-linux-gnu/libnvidia-allocator.so.1:ro"
    steam["mounts"] = [
        [],
        ["/srv/games/steamapps:/home/retro/.steam/debian-installation/steamapps:rw", PATHS["user"]["mounts"]["mods"], allocator],
        [PATHS["user"]["mounts"]["steamLibrary"], PATHS["user"]["mounts"]["mods"], allocator],
        [PATHS["user"]["mounts"]["steamLibrary"], "/srv/games/steamapps:/home/retro/Games/Steam:rw", PATHS["user"]["mounts"]["mods"], allocator],
    ][variant]
    xfce = app(doc, "user", "Desktop (xfce)")["runner"]
    xfce["env"] = list(xfce["env"][:1])
    xfce["mounts"] = [] if variant % 2 == 0 else [
        "/srv/games/steamapps:/home/retro/Games/Steam:rw",
        PATHS["user"]["mounts"]["modding"],
        PATHS["user"]["mounts"]["downloads"],
    ]
    write_doc(config, doc)
    reconcile.run(template, config, PATHS, PINS)
    result = tomllib.loads(config.read_text())
    assert app(result, "user", "Steam")["runner"]["mounts"] == list(app(tomllib.loads(template.read_text()), "user", "Steam")["runner"]["mounts"])
    assert app(result, "user", "Desktop (xfce)")["runner"]["env"][-1] == "STEAM_DIR=/home/retro/Games/Steam"


def test_text_and_semantic_preservation_with_styles_comments_and_overrides(template, tmp_path):
    config = tmp_path / "config.toml"
    text = old_text(template)
    text = text.replace('uuid = "12345678-1234-4234-9234-123456789abc"', "uuid = '12345678-1234-4234-9234-123456789abc' # identity")
    text = text.replace("paired_clients = []", "paired_clients = [\n  { certificate = 'kept' }, # paired\n]")
    text = text.replace("# Profiles:", "# custom comment survives\n# Profiles:")
    text = text.replace('name = "User"', "name = 'User' # display comment")
    text = text.replace(
        'image = "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8"',
        "image = 'ghcr.io/games-on-whales/steam:edge' # image comment",
        1,
    )
    config.write_text(text)
    before = tomllib.loads(text)
    spans = reconcile.protected_spans(text)
    reconcile.run(template, config, PATHS, PINS)
    after_text = config.read_text()
    after = tomllib.loads(after_text)
    assert before["uuid"] == after["uuid"]
    assert before["paired_clients"] == after["paired_clients"]
    for kind in ("uuid", "paired_clients", "overrides"):
        assert spans[kind] == reconcile.protected_spans(after_text)[kind]
    assert "# custom comment survives\n" in after_text
    assert "name = 'Edgar' # display comment" in after_text
    assert "# image comment\n" in after_text


@pytest.mark.parametrize("mutation", [
    lambda d: d["profiles"].append(copy.deepcopy(profile(d, "guest"))),
    lambda d: profile(d, "guest").pop("name"),
    lambda d: profile(d, "guest").__setitem__("name", "guest"),
    lambda d: profile(d, "guest")["apps"].pop(),
    lambda d: app(d, "guest", "Steam")["runner"].__setitem__("mounts", []),
    lambda d: profile(d, "guest")["apps"].append(copy.deepcopy(app(d, "user", "Firefox"))),
    lambda d: profile(d, "user").__setitem__("name", "Guest"),
    lambda d: profile(d, "guest").__setitem__("name", "Edgar"),
])
def test_guest_and_display_name_negatives(template, tmp_path, mutation):
    config = tmp_path / "config.toml"
    reconcile.run(template, config, PATHS, PINS)
    assert_rejected(template, config, mutation)


@pytest.mark.parametrize("profile_id,title", [
    ("user", "Steam"), ("user", "Desktop (xfce)"),
    ("guest", "Steam"), ("guest", "Desktop (xfce)"),
])
def test_required_apps_cannot_be_missing_or_duplicated(template, tmp_path, profile_id, title):
    config = tmp_path / "config.toml"
    reconcile.run(template, config, PATHS, PINS)
    canonical = config.read_bytes()
    assert_rejected(template, config, lambda d: profile(d, profile_id)["apps"].append(copy.deepcopy(app(d, profile_id, title))))
    config.write_bytes(canonical)
    assert_rejected(template, config, lambda d: profile(d, profile_id)["apps"].remove(app(d, profile_id, title)))


@pytest.mark.parametrize("mutation", [
    lambda d: profile(d, "moonlight-profile-id")["apps"].append(copy.deepcopy(app(d, "moonlight-profile-id", "Wolf UI"))),
    lambda d: profile(d, "moonlight-profile-id")["apps"].append(copy.deepcopy(app(d, "moonlight-profile-id", "Test ball"))),
    lambda d: app(d, "moonlight-profile-id", "Test ball")["runner"].__setitem__("run_cmd", "arbitrary root command"),
    lambda d: d["profiles"].append(tomlkit.item({"id": "intruder", "apps": []})),
])
def test_total_allowlist_rejects_controller_process_and_unknown_profiles(template, tmp_path, mutation):
    config = tmp_path / "config.toml"
    reconcile.run(template, config, PATHS, PINS)
    assert_rejected(template, config, mutation)


@pytest.mark.parametrize("mutation", [
    lambda d: app(d, "user", "Steam")["runner"].__setitem__("image", "unknown:latest"),
    lambda d: app(d, "user", "Steam")["runner"].__setitem__("image", "unknown@sha256:" + "1" * 64),
    lambda d: app(d, "guest", "Steam")["runner"]["mounts"].append("guest-volume:/data:rw"),
    lambda d: app(d, "guest", "Steam")["runner"]["mounts"].append("lutris:/var/lutris/:rw"),
    lambda d: app(d, "guest", "Steam")["runner"]["mounts"].append("/srv/mods:/stolen:rw"),
])
def test_images_named_volumes_and_cross_profile_mounts(template, tmp_path, mutation):
    config = tmp_path / "config.toml"
    reconcile.run(template, config, PATHS, PINS)
    assert "lutris:/var/lutris/:rw" in app(tomllib.loads(config.read_text()), "user", "Lutris")["runner"]["mounts"]
    assert_rejected(template, config, mutation)


@pytest.mark.parametrize("mutation", [
    lambda d: d.pop("uuid"),
    lambda d: d.__setitem__("uuid", 1),
    lambda d: d.__setitem__("uuid", "not-a-uuid"),
    lambda d: d.__setitem__("config_version", 6),
    lambda d: d.__setitem__("paired_clients", {}),
    lambda d: d.__setitem__("paired_clients", [1]),
    lambda d: d.__setitem__("unknown_root", True),
    lambda d: d["gstreamer"]["video"].pop("default_sink"),
    lambda d: d["gstreamer"].__setitem__("audio", "malformed"),
    lambda d: app(d, "moonlight-profile-id", "Test ball").__setitem__("video", "malformed"),
])
def test_root_schema_negatives(template, tmp_path, mutation):
    config = tmp_path / "config.toml"
    reconcile.run(template, config, PATHS, PINS)
    assert_rejected(template, config, mutation)


def test_candidate_validation_and_write_failures_are_precommit(template, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    before = config.read_bytes()
    monkeypatch.setattr(reconcile, "validate_apps", lambda *_: reconcile.fail("candidate rejected"))
    with pytest.raises(reconcile.PolicyError):
        reconcile.run(template, config, PATHS, PINS)
    assert config.read_bytes() == before
    monkeypatch.undo()
    monkeypatch.setattr(reconcile, "write_temp", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        reconcile.run(template, config, PATHS, PINS)
    assert config.read_bytes() == before


def test_interruptions_split_at_rename(template, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    before = config.read_bytes()
    monkeypatch.setenv("WOLF_RECONCILE_FAILPOINT", "before-rename")
    with pytest.raises(reconcile.PolicyError):
        reconcile.run(template, config, PATHS, PINS)
    assert config.read_bytes() == before
    monkeypatch.setenv("WOLF_RECONCILE_FAILPOINT", "after-rename")
    with pytest.raises(reconcile.DurabilityError):
        reconcile.run(template, config, PATHS, PINS)
    assert b'id = "guest"' in config.read_bytes()


def test_postcommit_sync_failure_keeps_candidate_live(template, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    monkeypatch.setattr(reconcile, "sync_live", lambda *_: (_ for _ in ()).throw(OSError("fsync parent")))
    with pytest.raises(reconcile.DurabilityError):
        reconcile.run(template, config, PATHS, PINS)
    assert b'id = "guest"' in config.read_bytes()


def test_idempotent_path_still_syncs_and_propagates_failure(template, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    reconcile.run(template, config, PATHS, PINS)
    before = config.read_bytes()
    called = []
    def broken(path):
        called.append(path)
        raise OSError("durability retry")
    monkeypatch.setattr(reconcile, "sync_live", broken)
    with pytest.raises(OSError):
        reconcile.run(template, config, PATHS, PINS)
    assert called == [config]
    assert config.read_bytes() == before


def test_mode_uid_gid_preserved(template, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    config.chmod(0o640)
    before = config.stat()
    reconcile.run(template, config, PATHS, PINS)
    after = config.stat()
    assert stat.S_IMODE(after.st_mode) == 0o640
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


# --- security-review fixes -------------------------------------------------


@pytest.mark.parametrize("mutation", [
    lambda doc: app(doc, "user", "Steam").__setitem__("start_virtual_compositor", 1),
    lambda doc: app(doc, "moonlight-profile-id", "Test ball").__setitem__("start_audio_server", 0),
])
def test_bool_is_not_interchangeable_with_int(template, tmp_path, mutation):
    """Python holds True == 1, so a plain != accepted retyped reviewed flags.

    The Nix validator distinguishes them, so tolerating it here was a
    policy-parity bypass letting a malformed field reach runtime.
    """
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    reconcile.run(template, config, PATHS, PINS)
    assert_rejected(template, config, mutation)


@pytest.mark.parametrize("value", [7, ["Wolf"], "", "   "])
def test_hostname_must_be_a_non_empty_string(template, tmp_path, value):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    reconcile.run(template, config, PATHS, PINS)
    assert_rejected(template, config, lambda doc: doc.__setitem__("hostname", value))


def test_dropped_overrides_are_restored_rather_than_bricking(template, tmp_path):
    """Wolf's /apps/add rewrite drops per-app video/audio tables.

    Test ball carries both. Failing closed on that drift meant ExecStartPre
    exited 1 and Wolf never started again without manual config surgery.
    """
    config = tmp_path / "config.toml"
    doc = tomlkit.parse(old_text(template))
    ball = app(doc, "moonlight-profile-id", "Test ball")
    assert "video" in ball and "audio" in ball
    del ball["video"]
    del ball["audio"]
    write_doc(config, doc)

    assert reconcile.run(template, config, PATHS, PINS)
    after = tomllib.loads(config.read_text())
    restored = next(
        item
        for entry in after["profiles"] if entry["id"] == "moonlight-profile-id"
        for item in entry["apps"] if item["title"] == "Test ball"
    )
    assert "video" in restored and "audio" in restored


def test_hostile_drift_is_still_rejected_not_restored(template, tmp_path):
    """Restoration must never launder drift that validate_apps exists to catch."""
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    reconcile.run(template, config, PATHS, PINS)
    assert_rejected(
        template,
        config,
        lambda doc: app(doc, "guest", "Steam")["runner"]["mounts"].append(
            "/srv/games/steamapps:/home/retro/stolen:rw"
        ),
    )


def test_concurrent_write_aborts_instead_of_clobbering(template, tmp_path, monkeypatch):
    """Wolf owns this file too; a write landing mid-transaction must not be lost."""
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    original = reconcile.write_temp

    def racing_write_temp(parent, data, metadata):
        config.write_bytes(config.read_bytes() + b"\n# concurrent Wolf write\n")
        return original(parent, data, metadata)

    monkeypatch.setattr(reconcile, "write_temp", racing_write_temp)
    with pytest.raises(reconcile.PolicyError):
        reconcile.run(template, config, PATHS, PINS)
    assert b"concurrent Wolf write" in config.read_bytes()


def test_signal_inside_the_rename_window_leaves_the_original(template, tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(old_text(template))
    before = config.read_bytes()
    monkeypatch.setenv("WOLF_RECONCILE_FAILPOINT", "at-rename")
    with pytest.raises(reconcile.TerminatedError):
        reconcile.run(template, config, PATHS, PINS)
    assert config.read_bytes() == before
