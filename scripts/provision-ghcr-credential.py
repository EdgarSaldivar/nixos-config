#!/usr/bin/env python3
"""Provision the read-only GHCR pull credential into secrets/pin-collector.yaml.

Run this from an interactive terminal in the nixos-config checkout:

    ./scripts/provision-ghcr-credential.py

The token is read from a hidden prompt and never reaches argv, the environment,
a temporary file, or the terminal. It is validated against ghcr.io BEFORE the
document is rewritten, so an unusable value cannot be stored.

That last part is the point. A previous provisioning run stored the *path* to a
file rather than the file's contents. Both checks used at the time -- "the key is
present" and `sops filestatus` reporting encrypted -- passed on a filesystem
path, because they verify confidentiality rather than correctness. Nothing caught
it until an image pull returned 403 in production.

Every write is refused unless, after re-encryption: the credential extracts back
byte-for-byte, every pre-existing key still decrypts to its original value, the
recipient set is unchanged, and the credential is encrypted at rest.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRETS = REPO_ROOT / "secrets" / "pin-collector.yaml"
KEY = "ghcr_dockerconfigjson"
USERNAME = "EdgarSaldivar"
PACKAGES = ["edgarsaldivar/pin-collector-api", "edgarsaldivar/pin-collector-model-service"]
RECIPIENT_RE = re.compile(r"age1[0-9a-z]{20,}")
TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def find_sops() -> str:
    """Resolve sops from PATH only.

    Never prefer a fixed /tmp path: this process hands the resolved binary
    SOPS_AGE_KEY, so anyone who can pre-create that path harvests the key to
    every secret in the document.
    """
    found = shutil.which("sops")
    if not found:
        die("sops not found on PATH")
    return found


def age_identity() -> str:
    """Derive the admin age key from the SSH key, in memory only."""
    ssh_key = Path.home() / ".ssh" / "id_ed25519"
    if not ssh_key.is_file():
        die(f"{ssh_key} not found; cannot derive the admin age identity")
    for argv in (
        ["ssh-to-age", "-private-key", "-i", str(ssh_key)],
        ["nix", "run", "nixpkgs#ssh-to-age", "--", "-private-key", "-i", str(ssh_key)],
    ):
        if not shutil.which(argv[0]):
            continue
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip().startswith("AGE-SECRET-KEY-"):
            return result.stdout.strip()
    die("could not derive an age identity (need ssh-to-age, or nix to fetch it)")
    raise AssertionError  # unreachable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: they would forward the Authorization header elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        die(f"ghcr.io attempted a redirect to {newurl}; refusing to forward credentials")


def check_shape(token: str) -> None:
    if token != token.strip():
        die("token has leading or trailing whitespace")
    if any(c.isspace() for c in token):
        die("token contains whitespace")
    if "/" in token:
        die("that looks like a file PATH, not a token -- paste the token value itself")
    if not token.startswith(("ghp_", "github_pat_")):
        die("expected a classic (ghp_) or fine-grained (github_pat_) personal access token")


def check_ghcr(token: str) -> None:
    """Prove the token can actually pull each package before storing it.

    A bearer token coming back is not enough: GHCR issues a token with an empty
    access list rather than a 4xx when the credential lacks package read. Use the
    token to fetch a real manifest, which is what the kubelet will do.
    """
    opener = urllib.request.build_opener(NoRedirect)
    basic = base64.b64encode(f"{USERNAME}:{token}".encode()).decode()
    for package in PACKAGES:
        url = f"https://ghcr.io/token?scope=repository:{package}:pull&service=ghcr.io"
        request = urllib.request.Request(url, headers={"Authorization": f"Basic {basic}"})
        try:
            with opener.open(request, timeout=30) as response:
                bearer = json.load(response).get("token")
        except urllib.error.HTTPError as exc:
            die(f"ghcr.io rejected the token for {package}: HTTP {exc.code}")
        except urllib.error.URLError as exc:
            die(f"could not reach ghcr.io: {exc.reason}")
        if not bearer:
            die(f"ghcr.io returned no pull token for {package}")

        manifest = urllib.request.Request(
            f"https://ghcr.io/v2/{package}/manifests/latest",
            method="HEAD",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/vnd.oci.image.index.v1+json,"
                "application/vnd.docker.distribution.manifest.list.v2+json,"
                "application/vnd.oci.image.manifest.v1+json,"
                "application/vnd.docker.distribution.manifest.v2+json",
            },
        )
        try:
            with opener.open(manifest, timeout=30):
                pass
        except urllib.error.HTTPError as exc:
            # 404 means authorised but no ':latest' tag, which is fine -- these images
            # are published by digest. 401/403 means the credential cannot pull.
            if exc.code in (401, 403):
                die(f"token authenticated but cannot pull {package}: HTTP {exc.code}")
            if exc.code != 404:
                die(f"unexpected status pulling {package}: HTTP {exc.code}")
        except urllib.error.URLError as exc:
            die(f"could not reach ghcr.io: {exc.reason}")
        print(f"  ok: pull authorised for {package}")


def sops(binary: str, env: dict[str, str], args: list[str], stdin: str | None = None):
    return subprocess.run(
        [binary, *args], input=stdin, capture_output=True, text=True, cwd=REPO_ROOT, env=env
    )


def extract(binary: str, env: dict[str, str], document: str, key: str) -> str | None:
    result = sops(
        binary, env, ["decrypt", "--input-type", "yaml", "--extract", f'["{key}"]', "/dev/stdin"], document
    )
    return None if result.returncode != 0 else result.stdout


def rewrite(plaintext: str, key: str, value: str) -> str:
    """Drop any existing entry for `key`, then append the replacement.

    Continuation lines of a block scalar are dropped too. Blank lines are held
    rather than treated as the end of the value, since a block scalar may contain
    them; they are only re-emitted if a non-indented line follows.
    """
    lines: list[str] = []
    held: list[str] = []
    skipping = False
    for line in plaintext.splitlines():
        if TOP_LEVEL_KEY_RE.match(line) and line.split(":", 1)[0] == key:
            skipping = True
            held.clear()
            continue
        if skipping:
            if not line.strip():
                held.append(line)
                continue
            if line[0] in " \t":
                held.clear()
                continue
            skipping = False
            lines.extend(held)
            held.clear()
        lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(f"{key}: {json.dumps(value)}")
    return "\n".join(lines) + "\n"


def main() -> None:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        die("run this from an interactive terminal so the token is not echoed")
    if not SECRETS.is_file():
        die(f"{SECRETS} not found")

    binary = find_sops()
    identity = age_identity()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()), "SOPS_AGE_KEY": identity}

    original_ciphertext = SECRETS.read_text()
    original_recipients = set(RECIPIENT_RE.findall(original_ciphertext))
    if not original_recipients:
        die("no age recipients found in the existing document")

    decrypted = sops(binary, env, ["decrypt", "--output-type", "yaml", str(SECRETS)])
    if decrypted.returncode != 0:
        die(f"sops decrypt failed: {decrypted.stderr.strip()[:400]}")
    plaintext = decrypted.stdout

    existing_keys = [
        match.group(1)
        for line in plaintext.splitlines()
        if (match := TOP_LEVEL_KEY_RE.match(line))
    ]
    original_values = {k: extract(binary, env, original_ciphertext, k) for k in existing_keys if k != KEY}

    print("Paste the GHCR read:packages token (input is hidden):")
    with warnings.catch_warnings():
        # getpass falls back to echoed input when terminal control fails; that
        # fallback arrives as a warning, so make it fatal rather than silent.
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass("token: ")
        except getpass.GetPassWarning:
            die("cannot disable terminal echo; refusing to read the token")
    if not token:
        die("no token entered")

    check_shape(token)
    print("validating against ghcr.io ...")
    check_ghcr(token)

    auth = base64.b64encode(f"{USERNAME}:{token}".encode()).decode()
    dockerconfig = json.dumps({"auths": {"ghcr.io": {"auth": auth}}}, separators=(",", ":"))

    encrypted_result = sops(
        binary, env, ["encrypt", "--filename-override", str(SECRETS), "/dev/stdin"],
        rewrite(plaintext, KEY, dockerconfig),
    )
    if encrypted_result.returncode != 0:
        die(f"sops encrypt failed: {encrypted_result.stderr.strip()[:400]}")
    encrypted = encrypted_result.stdout

    # The credential must come back byte-for-byte.
    if (extract(binary, env, encrypted, KEY) or "").strip() != dockerconfig:
        die("refusing to write: round trip did not reproduce the credential")

    # Every pre-existing key must still decrypt to exactly its original value.
    # A failed extraction yields None on both sides and would compare equal, so
    # treat it as fatal rather than letting the check pass vacuously.
    for key, before in original_values.items():
        if before is None:
            die(f"refusing to write: could not read the original value of {key}")
        after = extract(binary, env, encrypted, key)
        if after is None:
            die(f"refusing to write: could not read {key} back after rewrite")
        if after != before:
            die(f"refusing to write: value of {key} changed during rewrite")

    # The recipient set must be unchanged, or a required reader loses access.
    new_recipients = set(RECIPIENT_RE.findall(encrypted))
    if new_recipients != original_recipients:
        die(
            "refusing to write: recipient set changed "
            f"(lost {sorted(original_recipients - new_recipients)}, "
            f"gained {sorted(new_recipients - original_recipients)})"
        )

    # The credential must be encrypted at rest, not passed through in the clear.
    if not re.search(rf"(?m)^{KEY}: ENC\[", encrypted):
        die(f"refusing to write: {KEY} is not encrypted at rest")
    if auth in encrypted:
        die("refusing to write: the credential appears in plaintext in the output")

    # Nothing else may have modified the document while we prompted and validated.
    if SECRETS.read_text() != original_ciphertext:
        die("refusing to write: the secrets file changed while this script was running")

    # Write via a sibling temp file and rename, so an interrupted write cannot
    # leave a truncated, undecryptable secrets document behind. The name is unique
    # and created O_EXCL|O_NOFOLLOW: a predictable, symlink-following path would let
    # a pre-planted link redirect the write and then be renamed over the secrets
    # file, and would also let concurrent runs clobber each other.
    descriptor, temporary_name = tempfile.mkstemp(
        dir=SECRETS.parent, prefix=f".{SECRETS.name}.", suffix=".new"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, SECRETS.stat().st_mode & 0o777)
        os.replace(temporary, SECRETS)
    finally:
        if temporary.exists():
            temporary.unlink()

    print(f"\nwrote {SECRETS.relative_to(REPO_ROOT)}")
    print("the token was validated against both packages before writing; it was never printed")


if __name__ == "__main__":
    main()
