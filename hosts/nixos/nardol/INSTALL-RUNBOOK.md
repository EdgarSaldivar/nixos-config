# Nardol: LUKS2, Clevis, and Tang installation

This is a destructive reinstall of the Samsung 970 EVO Plus **and** WD_BLACK
SN850X. It is not an in-place conversion of Triforce: Ubuntu and the former
escape hatch on the WD are intentionally erased. Tang runs on Pelargir; Minas
is only the remote destination of Pelargir's encrypted restic backup and is not
in Nardol's boot path.

The intended unlock order is:

1. Clevis repeatedly asks Tang at `http://10.0.0.165:7654` for both managed
   volumes and boots unattended.
2. A human can enter the retained LUKS passphrase(s) over initrd SSH on port
   2222.
3. A human can enter the same passphrase(s) at Nardol's local console.

The console passphrase is the only fallback independent of Pelargir and the
network. Store it in a password manager plus one offline recovery copy.

## 0. Immutable hardware facts and stop conditions

Live hardware was rechecked on Triforce on 2026-08-07 and again on 2026-08-10:

| Role | Model and serial | Stable by-id path |
| --- | --- | --- |
| **Install target: root + ESP (ERASE)** | Samsung 970 EVO Plus 2TB, `S6S2NS0T629836M` | `/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M` |
| **Install target: encrypted `/srv` (ERASE)** | WD_BLACK SN850X 4TB, `24160W802539` | `/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539` |
| Preserve: currently unmanaged | Crucial P3 Plus 4TB, `2336E873EE7A` | `/dev/disk/by-id/nvme-CT4000P3PSSD8_2336E873EE7A` |

The Samsung appeared as `nvme2n1` on August 7 and `nvme0n1` on August 10.
That change without any hardware change is direct evidence that the kernel name
is not stable and must never appear in an install command.

Stop immediately if any of these are false:

- The tiny save-game set has been copied off Triforce, checksummed, and opened
  or otherwise validated from another machine.
- `10.0.0.118` is reserved or excluded from DHCP for MAC
  `9c:6b:00:36:e0:e8`. Both initrd and stage 2 use that address.
- The serial-qualified Samsung path resolves to the 1.8 TiB Samsung device and
  the serial-qualified WD path resolves to the 3.6 TiB SN850X.
- The Crucial serial above is still present and absent from `disko.devices`.
- A local keyboard/display recovery path has been tested at least once.

Physically removing the Crucial during installation is the strongest fence. Do
not remove the WD: it is now an intentional wipe target. The declarative
exact-device guards and flake check are defense in depth, not a substitute for
reading both wipe targets.

The live Triforce check showed `10.0.0.118` was obtained by DHCP from the Dream
Machine Pro. That proves the current lease, not a reservation. Confirm the
router-side reservation/exclusion in UniFi before relying on the static initrd.

> **Rerunning disko destroys the filesystem and LUKS header, including the
> Clevis token.** A normal `nixos-rebuild` is safe; a later disko run is another
> destructive reinstall and requires re-enrollment.

### Select the tiny state before erasing the WD

The live Triforce review found `/home/edgar/games/config` at **1.1 TB** and
`/home/edgar/games/data` at **56 GB**. Do not copy either tree wholesale. Most
of that is replaceable game/profile data. Triforce's active Steam home is under
`games/config/profile-data/user/WolfSteam`, despite the misleading directory
name and the older auxiliary mount under `games/data/steam`.

The active Elden Ring save directory is below:

```text
games/config/profile-data/user/WolfSteam/.steam/steam/steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing/<steam-id>/
```

Copy the entire small `EldenRing` directory, including `ER0000.sl2`,
`ER0000.co2`, their backups, `steam_autocloud.vdf`, and any archives. On
2026-08-08 the live `.co2` was modified on August 7, while the similarly named
files directly under `games/data/steam` were from June; those older copies are
useful extra recovery material but are not the authoritative save.

Re-pairing Moonlight is recommended for this clean migration. The old
`config.toml` contains Triforce-specific `/home/edgar/games/...` child mounts
and must not be restored verbatim. If preserving pairing is worth the manual
sanitisation, copy only `games/config/cfg/config.toml`, `cert.pem`, and
`key.pem`, keep them out of Git and the Nix store, and replace all old host
mounts with the reviewed `/srv` paths before starting Wolf.

Stop Wolf for the final copy, store the selected files on another physical
machine, record checksums there, and inspect the archive before continuing. Do
not count a copy elsewhere on either wipe target as a backup.

Preflight completed on 2026-08-10: the active 120 MB Elden Ring directory and
the five older recovery candidates were copied to
`/Users/edgar/Nardol-Migration-Backup-2026-08-10-preflight` on the Mac. All 13
files match Triforce by SHA-256, both ZIP archives pass `unzip -t`, and the
recorded hashes pass `shasum -a 256 -c SHA256SUMS`. This proves the selected
scope and destination, but it does not replace the final stopped-Wolf copy
immediately before kexec.

## 1. Deploy and prove Tang first

Deploy this configuration's Pelargir changes using the normal NixOS deployment
workflow. Do not begin Nardol's install until all of these succeed on Pelargir:

```bash
sudo systemctl status tangd.socket
curl --fail --silent --show-error http://127.0.0.1:7654/adv >/dev/null
tang-show-keys 7654
```

Record the `tang-show-keys` thumbprint independently. Enrollment pins this
thumbprint; do not replace it with a blind trust prompt.

Tang is intentionally reachable only from `10.0.0.118` on `eth0` and from
Pelargir's loopback. A request from a laptop, Tailscale, WireGuard, or a pod
should fail. Tang is unauthenticated HTTP by design; the network and systemd
source ACLs are its exposure boundary.

Trigger and inspect the first local backup staging copy:

```bash
sudo systemctl start pelargir-tang-health.service
sudo systemctl start pelargir-stage-tang-state.service
sudo find /var/lib/restic-staging/pelargir/tang -maxdepth 1 -type f -ls
```

Before binding Nardol, let the scheduled restic job complete or run the existing
Pelargir backup during a maintenance window. That job briefly quiesces the home
namespace workloads. Verify an off-host snapshot contains the entire `tang/`
tree, including hidden retired keys. Repeat this proof after every Tang key
rotation.

Pelargir deployment proof from 2026-08-10:

- Tang thumbprint: `ZJfBGUu7O_NarxA_aH51-h22UntH42ix-4hw727Tw44`
- Triforce at source `10.0.0.118` can fetch the advertisement; the Mac cannot.
- After the shared-runtime-directory fix, 60 of 60 concurrent Triforce requests
  succeeded, with no failed units or namespace errors.
- Both Tang files were staged and then verified byte-for-byte through restic
  snapshot `efbd46cd` (`2026-08-10T12:47:53-07:00`).
- Home Assistant, Zigbee2MQTT, and Mosquitto returned to 1/1; Pelargir remained
  `Ready` with zero failed systemd units.

## 2. Evaluate the destructive scope

Bind every evaluation and installer phase to the same reviewed commit. Replace
the placeholder below with the full commit from the final Codex/Claude review
receipt; do not derive it from whatever happens to be checked out at install
time. Keep this local shell open through section 5.

```bash
nardol_reviewed_revision='PASTE_THE_40_CHARACTER_REVIEWED_COMMIT'
nardol_repository="$(git rev-parse --show-toplevel)"
# Upstream nixos-anywhere 1.13.0 tag, verified 2026-08-08.
nardol_anywhere_revision=bad98b0685cf47eaeadcaf6787da8b51cf025693

test "${#nardol_reviewed_revision}" -eq 40
test "$(git -C "$nardol_repository" rev-parse --verify \
  "${nardol_reviewed_revision}^{commit}")" = "$nardol_reviewed_revision"
test "$(git -C "$nardol_repository" rev-parse HEAD)" = \
  "$nardol_reviewed_revision"
test -z "$(git -C "$nardol_repository" status \
  --porcelain=v1 --untracked-files=all)"

nardol_flake="git+file://${nardol_repository}?rev=${nardol_reviewed_revision}"

nix flake check --no-build "$nardol_flake"

nix eval --json \
  "$nardol_flake#nixosConfigurations.nardol.config.disko.devices.disk" \
  --apply 'disks: builtins.mapAttrs (_: disk: disk.device) disks'
```

The second command must print exactly these two keys and values:

```json
{
  "fast": "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539",
  "root": "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M"
}
```

Force evaluation of the generated destructive script. On the Apple Silicon Mac,
use `--dry-run`: realizing an x86_64-linux script locally requires a Linux
builder, while nixos-anywhere later builds it on the remote installer.

```bash
nix build --dry-run \
  "$nardol_flake#nixosConfigurations.nardol.config.system.build.diskoScript"
```

## 3. Prepare non-repository install material

Create a dedicated initrd SSH host key. It is copied into the unencrypted ESP,
so never reuse Nardol's normal SSH host key.

```bash
nardol_extra="$(mktemp -d)"
install -d -m 0700 "$nardol_extra/etc/secrets/initrd"
ssh-keygen -t ed25519 -N "" -C "nardol-initrd" \
  -f "$nardol_extra/etc/secrets/initrd/ssh_host_ed25519_key"
chmod 0600 "$nardol_extra/etc/secrets/initrd/ssh_host_ed25519_key"
ssh-keygen -lf "$nardol_extra/etc/secrets/initrd/ssh_host_ed25519_key.pub"
```

Record that fingerprint under the client alias `nardol-initrd`.

The current deployment's dedicated material was generated outside Git at
`/Users/edgar/Nardol-Install-Material-2026-08-10`. Its public-key fingerprint
is `SHA256:t5X3WelmimdW5/DAifXMqllki03eEo4BcWh7SC9Mq5s`. Reuse it only while
that fingerprint still matches, and set:

```bash
nardol_extra=/Users/edgar/Nardol-Install-Material-2026-08-10
```

Create the installer-only LUKS password file without putting the passphrase in
shell history or Git. Disko uses this same strong recovery passphrase for slot
0 on both managed volumes, so manual recovery may request it twice:

```bash
umask 077
read -r -s -p "New Nardol LUKS passphrase: " nardol_luks_passphrase
printf '\n'
printf '%s' "$nardol_luks_passphrase" > /tmp/nardol-disko-password
unset nardol_luks_passphrase
test -s /tmp/nardol-disko-password
```

The path must match `disko.nix`. With nixos-anywhere, both arguments below are
intentionally `/tmp/nardol-disko-password`: the first is a destination path in
the ephemeral installer; the second is the local source. Never substitute a
device path for the first argument.

## 4. Enter the installer, then re-prove the disks

Use a pinned nixos-anywhere release or boot an official NixOS installer. The
pinned nixos-anywhere release supports entering the current Ubuntu host as a
non-root user, then switches to root after kexec. Its documented prerequisite is
passwordless sudo for that initial user; direct Ubuntu root SSH is unnecessary.

The 2026-08-10 preflight proved `edgar@triforce` has ordinary password-gated
sudo, no root `authorized_keys`, and no passwordless sudo. Immediately before
the kexec phase, use `sudo visudo -f /etc/sudoers.d/nixos-anywhere` on Triforce
to install this temporary maintenance grant:

```sudoers
edgar ALL=(root) NOPASSWD: ALL
```

Validate the file and the non-interactive path from a fresh SSH connection:

```bash
sudo chmod 0440 /etc/sudoers.d/nixos-anywhere
sudo visudo -cf /etc/sudoers.d/nixos-anywhere
sudo -k
sudo -n true
```

This grant is deliberately broad because nixos-anywhere executes the reviewed
kexec payload as root; pretending a user-writable payload can be narrowly
authorized would provide false isolation. It disappears with Ubuntu at kexec.
Do not create it early, reuse it for unrelated work, or continue if the final
`sudo -n true` fails.

A phased nixos-anywhere flow keeps a safe inspection point after kexec. Re-run
the revision and cleanliness checks immediately before handing control to the
fully pinned nixos-anywhere commit:

```bash
test "$(git -C "$nardol_repository" rev-parse HEAD)" = \
  "$nardol_reviewed_revision"
test -z "$(git -C "$nardol_repository" status \
  --porcelain=v1 --untracked-files=all)"

nix run "github:nix-community/nixos-anywhere/$nardol_anywhere_revision" -- \
  --flake "$nardol_flake#nardol" \
  --phases kexec \
  --build-on remote \
  edgar@triforce
```

After kexec, SSH into the installer and run:

```bash
lsblk -d -o NAME,SIZE,MODEL,SERIAL,TRAN

nardol_root_target=/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M
nardol_fast_target=/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539

test -b "$nardol_root_target"
test "$(lsblk -dn -o SERIAL "$nardol_root_target" | xargs)" = S6S2NS0T629836M
test "$(lsblk -dn -o MODEL "$nardol_root_target" | xargs)" = "Samsung SSD 970 EVO Plus 2TB"

test -b "$nardol_fast_target"
test "$(lsblk -dn -o SERIAL "$nardol_fast_target" | xargs)" = 24160W802539
test "$(lsblk -dn -o MODEL "$nardol_fast_target" | xargs)" = "WD_BLACK SN850X 4000GB"

# This is the only NVMe that must remain outside the wipe list.
test -b /dev/disk/by-id/nvme-CT4000P3PSSD8_2336E873EE7A

lsblk -o NAME,PATH,SIZE,MODEL,SERIAL,FSTYPE,MOUNTPOINTS
```

Do not proceed based on a kernel name or size alone. Stop if the model/serial
checks fail, if the installer address is not `10.0.0.118`, or if the live disk
view differs from the table in section 0. There is no Ubuntu escape hatch after
the next phase.

## 5. Install without rebooting

Run the destructive phases but deliberately omit `reboot`; Clevis must be bound
and verified while the installer still has the new LUKS header available:

```bash
test "$(git -C "$nardol_repository" rev-parse HEAD)" = \
  "$nardol_reviewed_revision"
test -z "$(git -C "$nardol_repository" status \
  --porcelain=v1 --untracked-files=all)"

nix run "github:nix-community/nixos-anywhere/$nardol_anywhere_revision" -- \
  --flake "$nardol_flake#nardol" \
  --phases disko,install \
  --build-on remote \
  --disk-encryption-keys \
    /tmp/nardol-disko-password /tmp/nardol-disko-password \
  --extra-files "$nardol_extra" \
  root@10.0.0.118
```

At this point both the Samsung and WD have been erased. Do not reboot until
section 6 is complete and the installed system closure exists under `/mnt`.

## 6. Bind Clevis in slot 1 and retain slot 0

Back in the installer, use the tools from the just-installed system:

```bash
export PATH="/mnt/nix/var/nix/profiles/system/sw/bin:$PATH"
nardol_luks_entries=(
  "root:/dev/disk/by-partlabel/nardol-root-luks"
  "fast:/dev/disk/by-partlabel/nardol-fast-luks"
)

for entry in "${nardol_luks_entries[@]}"; do
  name="${entry%%:*}"
  device="${entry#*:}"
  test -b "$device"
  echo "== $name: $device =="
  cryptsetup luksDump "$device" | sed -n '1,80p'
done
# Both must report Version: 2, with the recovery passphrase in keyslot 0.

read -r -p "Verified Pelargir Tang thumbprint: " tang_thumbprint
tang_policy="$(printf \
  '{\"url\":\"http://10.0.0.165:7654\",\"thp\":\"%s\"}' \
  "$tang_thumbprint")"

# -y is safe here only because the independently recorded thumbprint is pinned
# in tang_policy. Each bind prompts for the existing slot-0 LUKS passphrase.
for entry in "${nardol_luks_entries[@]}"; do
  device="${entry#*:}"
  clevis luks bind -d "$device" -s 1 -y tang "$tang_policy"
done
unset tang_thumbprint tang_policy
```

Verify all three properties on **both** volumes before rebooting:

```bash
for entry in "${nardol_luks_entries[@]}"; do
  name="${entry%%:*}"
  device="${entry#*:}"

  echo "== $name: LUKS2 slots and token =="
  cryptsetup luksDump "$device" | sed -n '1,160p'
  # Version 2; keyslots 0 and 1 enabled; Clevis token references slot 1.

  clevis luks list -d "$device"
  # Exactly: 1: tang '{"url":"http://10.0.0.165:7654"}'

  # Prove the retained passphrase without closing the mounted install.
  cryptsetup open --test-passphrase "$device"

  # Prove Tang can recover its slot without printing the recovered key.
  clevis luks unlock -d "$device" -n "nardol-clevis-test-$name"
  cryptsetup close "nardol-clevis-test-$name"
done
```

Now capture both post-binding LUKS2 headers and copy them off Nardol:

```bash
cryptsetup luksHeaderBackup /dev/disk/by-partlabel/nardol-root-luks \
  --header-backup-file /tmp/nardol-root-luks2-header.img
cryptsetup luksHeaderBackup /dev/disk/by-partlabel/nardol-fast-luks \
  --header-backup-file /tmp/nardol-fast-luks2-header.img
chmod 0600 \
  /tmp/nardol-root-luks2-header.img \
  /tmp/nardol-fast-luks2-header.img
```

Treat both header backups as sensitive and store them separately from the
recovery passphrase. Copy them off the installer before reboot. Remove the
temporary local password file after both headers, the passphrase, and the
initrd key fingerprint have been stored successfully.

## 7. First automatic boot

Leave Pelargir and `tangd.socket` running, then reboot the installer. Nardol
should pass through initrd without human input. After stage 2 is reachable:

```bash
hostname
systemctl status nardol-clevis-binding-check.service
systemctl status nardol-gaming-readiness.service docker-wolf.service
systemctl --failed
ip -br address
sudo cryptsetup status nardol-root
sudo cryptsetup status nardol-fast
sudo clevis luks list -d /dev/disk/by-partlabel/nardol-root-luks
sudo clevis luks list -d /dev/disk/by-partlabel/nardol-fast-luks
findmnt / /srv
sudo docker info --format '{{json .Runtimes}}'
sudo docker exec wolf nvidia-smi -L
sudo -u edgar test -w /srv/games/steamapps
sudo -u edgar test -w /srv/games/steamapps/.nardol-mod-staging
sudo -u edgar test -w /srv/mods
sudo -u edgar test -w /srv/mods/downloads
sudo -u edgar test -w /srv/mods/backups
nardol_lan="$(ip -o link | awk -F': ' \
  '$0 ~ /link\/ether 9c:6b:00:36:e0:e8/ { print $2 }')"
test -n "$nardol_lan"
sudo ethtool "$nardol_lan" | grep -E 'Supports Wake-on|Wake-on:'
```

Confirm there is one `10.0.0.118` address after the initrd-to-stage-2 DHCP
handover. Test an actual power-off/cold boot as well as a warm reboot. During a
simultaneous site power recovery, Clevis keeps retrying while the LUKS prompt
exists, so a slower Pelargir boot should eventually release Nardol.

The I211 output must advertise `g` in `Supports Wake-on` and report
`Wake-on: g`. After the first clean shutdown, send a magic packet from another
system on the same LAN and prove a full cold wake before relying on Nardol as a
remotely operated host.

Wolf, its PulseAudio fallback, and every default Wolf UI/application image are
digest-pinned and systemd-managed through `docker-wolf.service`; the controller
is no longer a privileged container. Its configuration lives at
`/srv/wolf/config`, per-app home state at `/srv/wolf/data`, the shared Steam
library (including `common`, `compatdata`, and Workshop content) at
`/srv/games/steamapps`, mod packages and maintenance files at `/srv/mods`, and
Docker's own data root at `/srv/docker`. The Docker socket inside Wolf is still
root-equivalent by design, because Wolf creates the per-game containers.

Wolf automatically mounts each app's persistent state directory as its complete
`/home/retro`. Native CK3 local mods, Paradox launcher playsets, and similar
state below `/home/retro/.local/share/Paradox Interactive` therefore persist in
`/srv/wolf/data`; they do not need a second host bind mount. The explicit
`steamapps` mount exists to make large games, Proton prefixes, and Workshop
content stable and shareable independently of a particular Wolf app title.

An `ExecStartPre` policy creates the initial config from the reviewed Wolf v7
template, upgrades the exact known mutable tags in a restored Triforce config,
and refuses to start if any other child image lacks an `@sha256:` digest. New
apps added in Wolf UI therefore need an immutable image reference before the
next start; treat a policy failure as a supply-chain guard, not as a reason to
remove the check.

The game-agnostic custom Steam toolbox under
`hosts/nixos/nardol/steam-tools` is active through its reviewed immutable GHCR
digest. The image adds Protontricks/Winetricks, YAD, archive and installer
utilities, a lightweight terminal maintenance environment, Ludusavi, and
`nardol-modctl`; it contains no games, mods, credentials, Wine/Proton builds, or
drivers. Wolf startup migrates either the prior Steam edge tag or its previously
reviewed upstream digest to this toolbox image without touching pairing or app
state. Do not install tools with `apt` in a running child: Wolf deletes that
container at session end.

Mod archives belong in `/srv/mods/downloads`, small save backups in
`/srv/mods/backups`, and manager manifests in `/srv/mods/manifests`. A manager
that deploys via hardlinks or atomic renames should stage below
`/srv/games/steamapps/.nardol-mod-staging`, which remains within the same bind
mount as its target game inside the Steam child.

Pair Moonlight again unless the old configuration was sanitised as described in
section 0. Install Elden Ring once so Steam creates app ID `1245620` under
`/srv/games/steamapps/compatdata`, stop the Steam child, and restore the selected
`EldenRing` save directory to the matching `AppData/Roaming` path. Restore as
`edgar` or run `sudo chown -R 1000:1000` on the restored directory, then verify
it is writable with `sudo -u edgar test -w <restored-EldenRing-directory>`.
Do not blindly restore either old multi-gigabyte tree.

## 8. Prove both manual fallbacks

Do this only after automatic boot has succeeded and while a local console is
available.

Runtime-mask the Tang socket on Pelargir. A plain `stop` is insufficient for a
drill because the five-minute health timer (or a restic dependency) would start
the socket again automatically:

```bash
sudo systemctl mask --runtime --now tangd.socket
```

Reboot Nardol. From inside the LAN, the initrd SSH fallback is:

```bash
ssh -p 2222 -tt \
  -o HostKeyAlias=nardol-initrd \
  root@10.0.0.118
```

From elsewhere on the tailnet, Pelargir is the jump host:

```bash
ssh -J edgar@pelargir -p 2222 -tt \
  -o HostKeyAlias=nardol-initrd \
  root@10.0.0.118
```

The authorized key forces `systemd-tty-ask-password-agent`; enter the recovery
passphrase for every outstanding root/fast request (it may be requested twice).
The SSH connection closes as stage 2 starts. Port forwarding and an initrd
shell are intentionally unavailable.

Repeat once at the physical console with Tang stopped. This is the independent
recovery path when Pelargir or the network is down. Restore Tang afterwards:

```bash
sudo systemctl unmask --runtime tangd.socket
sudo systemctl start tangd.socket
curl --fail --silent --show-error http://127.0.0.1:7654/adv >/dev/null
```

## 9. Tang loss or rotation

`/var/lib/tang` is a recovery set. Back up the whole directory, including
retired keys, and restore it as a unit. After a Pelargir rebuild, restore the
tree before serving production requests, verify `tang-show-keys 7654` matches
the recorded thumbprint, then prove a Nardol cold boot.

If Tang state is irretrievably lost, neither volume is lost:

1. Unlock both Nardol volumes with slot 0 at the console or through initrd SSH.
2. Bring up a new Tang key set and record its thumbprint.
3. Prove slot 0 again with `cryptsetup open --test-passphrase` on both
   `/dev/disk/by-partlabel/nardol-root-luks` and `nardol-fast-luks`.
4. Remove only slot 1 from each device with `clevis luks unbind -d DEVICE -s
   1`.
5. Rebind slot 1 on each device to the verified new thumbprint and create both
   fresh header backups.

Never remove slot 0. Never rotate or delete retired Tang keys until every bound
client has been revalidated and an off-host backup has been restored in a drill.
