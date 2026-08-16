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

# A phased install must reuse one persistent client key. Letting each
# nixos-anywhere invocation generate its own temporary key can strand the
# machine after kexec when the first invocation deletes that key. Keep this
# client key outside nardol_extra: --extra-files copies that whole tree onto
# the target, where this installer-only private key must never be installed.
nardol_installer_material="$(mktemp -d)"
chmod 0700 "$nardol_installer_material"
nardol_installer_key="$nardol_installer_material/nixos_anywhere_ed25519"
ssh-keygen -t ed25519 -N "" -C "nardol-nixos-anywhere" \
  -f "$nardol_installer_key"
chmod 0600 "$nardol_installer_key"
ssh-keygen -lf "$nardol_installer_key.pub"
```

Record that fingerprint under the client alias `nardol-initrd`.

The current deployment's dedicated material was generated outside Git at
`/Users/edgar/Nardol-Install-Material-2026-08-10`. Its public-key fingerprint
is `SHA256:t5X3WelmimdW5/DAifXMqllki03eEo4BcWh7SC9Mq5s`. The dedicated phased
installer client key is
`SHA256:9smY1CpJT6GPpbcx8k8vhg2+xK13WDAPxUK49PfW8aY`. Reuse them only while
both fingerprints still match, and set:

```bash
nardol_extra=/Users/edgar/Nardol-Install-Material-2026-08-10
nardol_installer_key=/Users/edgar/Nardol-Installer-Client-2026-08-10/nixos_anywhere_ed25519

# Only the initrd host key belongs in the tree copied to Nardol.
test ! -e "$nardol_extra/client"
test "$(find "$nardol_extra" -type f | wc -l | xargs)" = 2
```

Create the installer-only LUKS password file without putting the passphrase in
shell history or Git. Disko uses this same strong recovery passphrase for slot
0 on both managed volumes, so manual recovery may request it twice:

Run this as one compound command. The outer shell consumes the whole quoted
command before either hidden prompt reads from the terminal, so pasting the
block cannot accidentally feed a later command line in as the passphrase:

```bash
bash -c '
set -eu
umask 077
printf "New Nardol LUKS passphrase: "
IFS= read -r -s first
printf "\nConfirm passphrase: "
IFS= read -r -s second
printf "\n"
if [ -z "$first" ] || [ "$first" != "$second" ]; then
  unset first second
  echo "Passphrases were empty or did not match; nothing written" >&2
  exit 1
fi
printf "%s" "$first" > /tmp/nardol-disko-password
chmod 0600 /tmp/nardol-disko-password
unset first second
test -s /tmp/nardol-disko-password
echo "Passphrase file created successfully"
'
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
  -i "$nardol_installer_key" \
  --flake "$nardol_flake#nardol" \
  --phases kexec \
  --build-on remote \
  edgar@triforce
```

After kexec, retain the same client key when entering the installer:

```bash
ssh -i "$nardol_installer_key" root@10.0.0.118
```

Then run:

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
  -i "$nardol_installer_key" \
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

Back in the installer, run the tools inside the just-installed system. Its
profile symlinks are absolute `/nix/store` links, so prepending
`/mnt/nix/var/nix/profiles/system/sw/bin` to the installer's `PATH` does not
work. `nixos-enter` creates a private mount namespace, exposes the live block
devices to the target, and cleans up those bind mounts when each command exits:

```bash
nardol_enter=(nixos-enter --root /mnt --silent --)
nardol_bash=/nix/var/nix/profiles/system/sw/bin/bash
nardol_clevis=/nix/var/nix/profiles/system/sw/bin/clevis
nardol_cryptsetup=/nix/var/nix/profiles/system/sw/bin/cryptsetup
nardol_luks_entries=(
  "root:/dev/disk/by-partlabel/nardol-root-luks"
  "fast:/dev/disk/by-partlabel/nardol-fast-luks"
)

for entry in "${nardol_luks_entries[@]}"; do
  name="${entry%%:*}"
  device="${entry#*:}"
  test -b "$device"
  echo "== $name: $device =="
  "${nardol_enter[@]}" "$nardol_cryptsetup" luksDump "$device" |
    sed -n '1,80p'
done
# Both must report Version: 2, with the recovery passphrase in keyslot 0.

read -r -p "Verified Pelargir Tang thumbprint: " tang_thumbprint
tang_policy="$(printf \
  '{\"url\":\"http://10.0.0.165:7654\",\"thp\":\"%s\"}' \
  "$tang_thumbprint")"

# -y is safe here only because the independently recorded thumbprint is pinned
# in tang_policy. -k - reads the already-uploaded slot-0 passphrase from stdin;
# neither the command line nor the output contains the passphrase.
for entry in "${nardol_luks_entries[@]}"; do
  device="${entry#*:}"
  "${nardol_enter[@]}" "$nardol_clevis" luks bind \
    -d "$device" -s 1 -y -k - tang "$tang_policy" \
    < /tmp/nardol-disko-password
done
unset tang_thumbprint tang_policy
```

Verify all three properties on **both** volumes before rebooting:

```bash
for entry in "${nardol_luks_entries[@]}"; do
  name="${entry%%:*}"
  device="${entry#*:}"

  echo "== $name: LUKS2 slots and token =="
  "${nardol_enter[@]}" "$nardol_cryptsetup" luksDump "$device" |
    sed -n '1,160p'
  # Version 2; keyslots 0 and 1 enabled; Clevis token references slot 1.

  "${nardol_enter[@]}" "$nardol_clevis" luks list -d "$device"
  # Exactly: 1: tang '{"url":"http://10.0.0.165:7654"}'

  # Prove the retained passphrase without closing the mounted install.
  "${nardol_enter[@]}" "$nardol_cryptsetup" open \
    --test-passphrase --key-file - "$device" \
    < /tmp/nardol-disko-password

  # The install already has this LUKS device open, so a second mapper is not a
  # valid test. Recover slot 1 through Tang and pipe it directly into
  # cryptsetup's passphrase test without printing or storing the recovered key.
  "${nardol_enter[@]}" "$nardol_bash" -c '
    set -eo pipefail
    "$1" luks pass -d "$3" -s 1 |
      "$2" open --test-passphrase --key-file - "$3"
  ' _ "$nardol_clevis" "$nardol_cryptsetup" "$device"
done
```

Now capture both post-binding LUKS2 headers and copy them off Nardol:

```bash
"${nardol_enter[@]}" "$nardol_cryptsetup" luksHeaderBackup \
  /dev/disk/by-partlabel/nardol-root-luks \
  --header-backup-file /tmp/nardol-root-luks2-header.img
"${nardol_enter[@]}" "$nardol_cryptsetup" luksHeaderBackup \
  /dev/disk/by-partlabel/nardol-fast-luks \
  --header-backup-file /tmp/nardol-fast-luks2-header.img
chmod 0600 \
  /mnt/tmp/nardol-root-luks2-header.img \
  /mnt/tmp/nardol-fast-luks2-header.img
```

The paths visible from the installer are `/mnt/tmp/nardol-*-luks2-header.img`;
copy those files off-host and compare remote/local SHA-256 hashes before reboot.

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
sudo -u edgar test -w /srv/games/nonsteam
sudo -u edgar test -w /srv/mods
sudo -u edgar test -w /srv/mods/downloads
sudo -u edgar test -w /srv/mods/backups
sudo -u edgar test -w /srv/games/guest-steamapps
sudo -u edgar test -w /srv/games/guest-steamapps/.nardol-mod-staging
sudo -u edgar test -w /srv/games/guest-nonsteam
sudo -u edgar test -w /srv/mods-guest
sudo -u edgar test -w /srv/mods-guest/downloads
sudo -u edgar test -w /srv/mods-guest/backups
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

Deployment proof from 2026-08-10:

- The destructive install used reviewed revision
  `69996dc2de5cd93324432ebfcd4eccca919c160b`; the post-boot foreign-PV guard was
  switched at `21004775b2a92b8b044ff823d7365142601ca319`.
- Both automatic Tang unlocks succeeded (initial boot and a warm reboot), and
  the restricted initrd SSH fallback was reachable at `10.0.0.118:2222` during
  the first boot.
- Both LUKS2 devices retain keyslots 0 and 1, have exactly one Clevis token
  pointing at slot 1, and list exactly
  `1: tang '{"url":"http://10.0.0.165:7654"}'`. Slot 0 and non-printing Tang
  recovery tests passed on both volumes before the first reboot.
- The post-binding header backups are stored with mode 0600 under
  `/Users/edgar/Nardol-LUKS-Headers-2026-08-10` on the Mac. Their verified
  SHA-256 hashes are `8d45d553e063986fdc8d480b3d1b0ac85f9d8b95acdc2d558da66e991f44dd02`
  (root) and `0485a8d66a013476c93c72dbb7a15f67ef17c3f424b99bf903aea049d35d4071`
  (fast).
- The preserved Crucial still has its original EFI and Proxmox LVM partitions.
  Nardol keeps LVM's cryptsetup-required udev rules but rejects all PV scans in
  initrd and stage 2, so the foreign `pve` VG stays inactive. This guard is
  enforced by the `nardol-unlock-contract` flake check.
- The warm reboot reached `systemctl is-system-running = running` with zero
  failed units. `/` and `/srv` are the `nardol-root` and `nardol-fast` mappings;
  the RTX 4090 is visible on the host and inside Wolf; Docker uses `/srv/docker`
  with the NVIDIA runtime; and `docker-wolf.service` is active.
- The I211 advertises magic-packet support and is armed as `Wake-on: g`. The
  remaining recovery proofs are a real cold power-off/WOL drill and both
  Tang-down passphrase drills: restricted initrd SSH and the physical console.
- Moonlight pairing, the first Steam/Elden Ring launch, and the selective save
  restore succeeded. The pre-restore copy remains at
  `/srv/mods/backups/elden-ring-pre-restore-2026-08-10T152928-0700`.
- The general Steam/XFCE modding workspace was deployed from
  `0c01362ff1f4701abbfc15a24bfd98dfd9240555`. A successful Seamless Coop
  installation and launch exercised Edgar's graphical modding path, Steam
  library, and restored `.co2` save.
- After that proof, the obsolete 71 GiB Steam rollback tree below Wolf's
  private app home was deleted. Its empty bind target was recreated as
  `1000:1000` mode `0750`; `/srv/games/steamapps` remains Edgar's active
  library. Known-bad NixOS generation 9 was also removed, while known-good
  generation 8 remains the immediate rollback target.

### One-time Guest pre-deployment emptiness gate

This subsection documents the target of the coordinated two-profile change; a
documentation-only revision does not make it deployable. Do not run this gate
or the acceptance procedure until the reviewed Nix/TOML/Python implementation
is present in the same deployment revision. That implementation must add the
`guest` profile and its mounts and directories, make legacy Steam
normalization profile-aware, and replace the current whole-file policy that
permits only one Steam app and one XFCE app with a per-profile policy. Stop if
any of those implementation preconditions is absent: otherwise the current
normalizer can rewrite a Guest Steam app to Edgar's mounts, or the start policy
will reject the second Steam/XFCE pair.

The deployment must also include a reviewed configuration-only migration that
inserts the complete `guest` profile into the existing
`/srv/wolf/data/cfg/config.toml` before the Wolf controller starts. The template
is copied only when `config.toml` is absent, but Nardol already has that file;
adding Guest only to `wolf-config.template.toml` is therefore inert. The
existing reconciler's image and Steam/XFCE line rewrites do not create a
profile. Stop unless the deployed migration handles the existing file and
preserves its pairing, overrides, and Edgar profile. This configuration change
does not move any of Edgar's data.

Run this once, immediately before the deployment that first creates the Guest
profile. Quiesce Wolf first:

```bash
sudo systemctl stop docker-wolf
test "$(sudo systemctl is-active docker-wolf)" = inactive

sudo docker ps -a --no-trunc
test -z "$(sudo docker ps -a --format '{{.Names}}' |
  grep -Ei 'guest|_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || true)"
```

Review the complete `docker ps -a` output as well as the assertion. Stop if any
Guest container or any application container matching the
`*_<lobby-uuid>` session pattern remains; do not assume stopping the controller
removed its children.

Use lstat-style checks that inspect the path object itself. A symlink, any
existing non-directory object, or any entry in an existing directory is a
collision:

```bash
guest_paths=(
  /srv/wolf/data/profile-data/guest
  /srv/games/guest-steamapps
  /srv/games/guest-nonsteam
  /srv/mods-guest
)

for path in "${guest_paths[@]}"; do
  if sudo test -L "$path"; then
    echo "STOP: Guest path is a symlink: $path" >&2
    exit 1
  fi
  if sudo test -e "$path" && ! sudo test -d "$path"; then
    echo "STOP: Guest path is not a directory: $path" >&2
    exit 1
  fi
  if sudo test -d "$path"; then
    if ! first_entry="$(sudo find "$path" -mindepth 1 -maxdepth 1 -print -quit)"; then
      echo "STOP: cannot inspect Guest directory: $path" >&2
      exit 1
    fi
    if test -n "$first_entry"; then
      echo "STOP: Guest path is not empty: $path" >&2
      exit 1
    fi
  fi
done
```

Every object and directory-content probe above runs through `sudo`; an
unprivileged permission denial below `/srv/wolf` must never be mistaken for an
absent or empty path.

Stop for operator review on any collision. Keep Wolf stopped throughout the
deployment and activation; start it with `sudo systemctl start docker-wolf`
only after activation has completed. This is strictly a one-time transition
gate. Never enforce it after Guest has begun using these paths, because their
non-emptiness is then expected and valuable.

Wolf, its PulseAudio fallback, and every default Wolf UI/application image are
digest-pinned and systemd-managed through `docker-wolf.service`; the controller
is no longer a privileged container. Wolf derives both its
configuration/pairing directory (`cfg/`) and per-app homes (`profile-data/`)
from `HOST_APPS_STATE_FOLDER`, so they live at `/srv/wolf/data/cfg` and
`/srv/wolf/data/profile-data`. Docker's own data root is at `/srv/docker`. The
Docker socket inside Wolf is still root-equivalent by design, because Wolf
creates the per-game containers.

The two-profile target is:

| Player | Profile ID | Display name | Steam library | Non-Steam games | Mods | Steam app home |
| --- | --- | --- | --- | --- | --- | --- |
| Edgar | `user` | `Edgar` | `/srv/games/steamapps` | `/srv/games/nonsteam` | `/srv/mods` | `/srv/wolf/data/profile-data/user/WolfSteam` |
| Guest | `guest` | `Guest` | `/srv/games/guest-steamapps` | `/srv/games/guest-nonsteam` | `/srv/mods-guest` | `/srv/wolf/data/profile-data/guest/WolfSteam` |

The existing profile ID `user` deliberately displays as `Edgar`. Do not
"fix" that mismatch: changing the ID would orphan the 4.9G live Steam home.
There is no data migration; Edgar's paths remain untouched. Guest starts with
empty, separate storage. No mount is shared between profiles except the
reviewed read-only NVIDIA allocator bind. This is mount-enforced isolation,
not a naming convention.

Wolf has no profile access control. Any paired client can select either profile
in the Wolf UI, so using the matching Steam account is a convention at the UI
layer even though the underlying storage is genuinely isolated. This is an
accepted household risk and cannot be fixed in Wolf configuration.

Wolf automatically mounts each app's persistent state directory as its complete
`/home/retro`. Native CK3 local mods, Paradox launcher playsets, and similar
state below `/home/retro/.local/share/Paradox Interactive` therefore persist in
that profile's app home under `/srv/wolf/data/profile-data`; they do not need a
second host bind mount. Each profile's explicit `steamapps` mount keeps its own
large games, Proton prefixes, and Workshop content stable independently of a
particular Wolf app title.
The current GoW Steam image uses `/home/retro/.steam/steam` as its active Steam
root; `/home/retro/.steam/debian-installation` is a separate inactive directory
and must not be used as the bind-mount target. The runner also overrides
`STEAM_DIR` to the active root so the pinned toolbox image resolves manifests
and prefixes consistently.

The Wolf `Desktop (xfce)` app is the graphical maintenance environment. Within
each profile, it bind-mounts that player's encrypted persistent data at three
visible home folders: `Games/Steam/steamapps` contains that player's Steam
library tree, `Games/NonSteam` contains game files installed through XFCE and
later added to that player's Steam as non-Steam shortcuts, `Modding` contains
that player's packages, manager state, and backups, and `Downloads` maps
directly to that player's `Modding/downloads`. Both game roots are declarative
mounts rather than paths baked into the desktop image.
The pinned XFCE image includes Firefox, Thunar, Xarchiver, Mousepad, zip, unzip,
and 7-Zip, so downloading, extracting, copying, and editing a mod does not
require SSH or a terminal. The Steam app receives the same per-player `Games`
views and a `Modding` alias, so graphical file-picker paths remain consistent
between apps. Within one profile, close that player's Steam session before
changing game files in XFCE, then close that player's XFCE before starting
Steam again; the two apps share that player's writable library and must not
modify it concurrently. Across profiles there is no writable shared storage,
so no cross-player coordination is required. Each player mods their own
library independently, and co-op partners generally need matching mod sets, so
deploy mods per player.
See [modding-desktop.md](modding-desktop.md) for the portable mount contract
and a host-independent Wolf example.

NixOS exposes NVIDIA's GLVND vendor registration at
`/run/opengl-driver/share/glvnd/egl_vendor.d/10_nvidia.json`, outside the
Ubuntu-based Wolf image's default lookup directories. The controller therefore
sets `__EGL_VENDOR_LIBRARY_FILENAMES` to that NVIDIA manifest followed by the
image's Mesa manifest. NVIDIA must come first for the selected DRM node, while
this GLVND build requires both manifests to expose the
`EGL_EXT_device_enumeration` extension used by Wolf's compositor. Without that
ordering and combination, the compositor either selects Mesa/Zink for the
NVIDIA node or cannot enumerate EGL devices, and Moonlight reports a misleading
generic UDP-firewall error even though the advertised Wolf ports are open.

The NVIDIA container runtime provides the driver libraries but not NVRTC,
which is a separate CUDA redistributable required to register GStreamer's
`cudaconvertscale` element. Wolf mounts only Nixpkgs' `cuda_nvrtc` library
output read-only at `/opt/nardol-nvrtc` and adds that path to
`LD_LIBRARY_PATH`. Without it, Wolf silently falls back from NVENC to software
x264/x265 even though `nvidia-smi` succeeds inside the controller.

NixOS's NVIDIA container toolkit injects child-application driver libraries
below `/run/opengl-driver` and `/usr/local/nvidia`, while the GoW Steam image
checks Ubuntu's `/usr/lib/x86_64-linux-gnu` path before generating its NVIDIA
EGL and Vulkan registration files. The Steam runner therefore bind-mounts the
host's `libnvidia-allocator.so.1` at that expected path and selects the
generated NVIDIA GLVND manifest ahead of Mesa. Without this compatibility
mount, Steam's inner Sway compositor selects Mesa/Zink and exits immediately
with `EGL_NOT_INITIALIZED`, which Moonlight presents as a closed session.

Wolf's NVIDIA zero-copy path is explicitly disabled with
`WOLF_USE_ZERO_COPY=FALSE`: at the pinned Wolf revision, that path initializes
NVENC successfully but then fails to allocate its `GsCUDABuf` DMA buffer on
this NixOS/NVIDIA stack, terminating the Moonlight session with a generic DRM
or connection error. This setting selects Wolf's supported CUDA upload/copy
path; it does not disable NVENC or cause the CPU software-encoder fallback.

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

Edgar's mod archives belong in `/srv/mods/downloads`, small save backups in
`/srv/mods/backups`, and manager manifests in `/srv/mods/manifests`; Guest uses
the corresponding subdirectories below `/srv/mods-guest`. A manager that
deploys via hardlinks or atomic renames should stage below that player's own
`steamapps/.nardol-mod-staging`: `/srv/games/steamapps/.nardol-mod-staging` for
Edgar or `/srv/games/guest-steamapps/.nardol-mod-staging` for Guest. Staging
there remains within the same bind mount as its per-player target game.

Pair Moonlight again unless the old configuration was sanitised as described in
section 0. For Edgar, install Elden Ring once so Steam creates app ID `1245620`
under `/srv/games/steamapps/compatdata`, stop Edgar's Steam child, and restore
the selected `EldenRing` save directory to the matching `AppData/Roaming`
path. Restore as `edgar` or run `sudo chown -R 1000:1000` on the restored
directory, then verify it is writable with
`sudo -u edgar test -w <restored-EldenRing-directory>`. Do not blindly restore
either old multi-gigabyte tree, and do not copy any of Edgar's data to Guest.

Guest signs into their own Steam account in their own session and downloads
their own copies (approximately 272G for the intended set) into the initially
empty `/srv/games/guest-steamapps`. Do not copy manifests and do not perform
`LastOwner` fixups. Steam's default library root
`/home/retro/.steam/steam` is already registered; do not add
`/home/retro/.steam/steam/steamapps` as a library, because `steamapps` is the
library root's child and adding it would be wrong. Each account must own its own
licences. Steam Family Sharing lends a library to only one person at a time and
cuts the borrower off when the owner plays, so it cannot provide simultaneous
use.

### Guest backup and recovery scope

Classify every Guest-bearing location before setting backup policy:

| Location | Classification and recovery rule |
| --- | --- |
| `/srv/wolf/data/profile-data/guest/WolfSteam` | Complete Steam app home. It includes account-local Steam state and is **not replaceable**; back it up. |
| `/srv/wolf/data/profile-data/guest/WolfXFCE` | Complete XFCE app home, including desktop and tool state. It is **not replaceable**; back it up. |
| `/srv/games/guest-steamapps` | Most installed game payloads are redownloadable. `compatdata` and any saves stored elsewhere inside the library are **not replaceable**; select them for backup. |
| `/srv/games/guest-nonsteam` | User-supplied games and files are **not replaceable**; back them up. |
| `/srv/mods-guest` | User-curated archives, manager state, manifests, backups, and mod sets are **not replaceable**; back them up. |
| Steam userdata and Steam Cloud | Validate Cloud support, sync status, and actual coverage separately for every title. Treat uncovered userdata and saves as non-replaceable. |

Do not assume every title stores saves in `compatdata`; native titles and
individual Windows games may use other locations. Record and test the actual
save path for each title before calling its recovery coverage complete.

### Two-profile deployment acceptance

Run the cheapest gate first, before any deployment or runtime work:

```bash
nix flake check --no-build
```

Stop on failure. Then perform the reviewed deployment, including the one-time
emptiness gate above for the initial Guest deployment. After activation, start
Wolf and prove that the configuration reconciler is byte-stable on a second
start:

```bash
sudo systemctl start docker-wolf
sudo systemctl is-active --quiet docker-wolf
first_config_hash="$(sudo sha256sum /srv/wolf/data/cfg/config.toml)"
sudo systemctl restart docker-wolf
sudo systemctl is-active --quiet docker-wolf
second_config_hash="$(sudo sha256sum /srv/wolf/data/cfg/config.toml)"
test "$first_config_hash" = "$second_config_hash"
sudo -u edgar test -w /srv/games/guest-steamapps
sudo -u edgar test -w /srv/games/guest-steamapps/.nardol-mod-staging
sudo -u edgar test -w /srv/games/guest-nonsteam
sudo -u edgar test -w /srv/mods-guest
sudo -u edgar test -w /srv/mods-guest/downloads
sudo -u edgar test -w /srv/mods-guest/backups
```

Next run a no-download two-session smoke test. Confirm the `Guest` profile is
visible in Wolf UI. Use two distinct, already paired Moonlight clients; select
`Edgar` from one and `Guest` from the other. Launch Edgar's existing Steam app
and Guest's `Desktop (xfce)`; optionally open the Firefox installed inside the
Guest desktop. There is no separate Guest Firefox app. Keep both sessions open
for the first container and isolation check below.

Select only application containers whose names end in the
`*_<lobby-uuid>` session pattern, then assert exactly two with distinct lobby
UUIDs:

```bash
mapfile -t application_containers < <(
  sudo docker ps --format '{{.Names}}' |
    grep -E '_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)
test "${#application_containers[@]}" -eq 2
first_lobby_uuid="${application_containers[0]##*_}"
second_lobby_uuid="${application_containers[1]##*_}"
test "$first_lobby_uuid" != "$second_lobby_uuid"
```

The `wolf` controller and audio or other helper containers also run during this
test; do not assert that only two total containers exist. Run the assertion for
both application pairs in this procedure. Inspect every mount on the two
selected application containers in each pair, including Wolf's automatic
app-home binds:

```bash
sudo docker inspect --format \
  '{{.Name}}{{range .Mounts}}{{printf "\n%s\t%s\t%s\t%t" .Type .Name .Source .RW}}{{end}}' \
  "${application_containers[@]}"
```

Canonicalize each bind-mount source and compare mounts only across the two
containers. Stop if the same canonical path, or an ancestor and descendant,
appears across containers and either side is writable. Stop if both containers
use the same named-volume identity. The sole allowlisted cross-container mount
is the canonical
`/run/opengl-driver/lib/libnvidia-allocator.so.1` bind, and it must be read-only
on both sides. Duplicate aliases inside one container are not cross-player
overlap and must not be rejected.

After recording that first pair's inspection, end both application sessions.
Keep the clients paired and distinct, then launch Edgar's `Desktop (xfce)` and
Guest's Steam app, still without downloading anything, and repeat the same
exactly-two-container assertion and mount inspection. This second pair is
mandatory: it proves that Guest Steam has Guest's library and automatic
`WolfSteam` app home, and that Edgar XFCE retains Edgar's mounts. At no point
run Steam and XFCE concurrently within the same profile. Retain both pairs'
`docker inspect` output with the acceptance record; do not approve isolation
from the TOML alone or without inspecting Guest Steam.

Only after that no-download smoke passes, Guest buys and downloads one small
test title into Guest's library. Before the two-session AAA test, write the
title's actual name and its exact, independently verified save location in the
acceptance record. Stop if either is unknown; do not substitute a guessed
`compatdata` path. With Edgar running a known game in the other session, launch
the recorded Guest title, create a recognizable save at the recorded location,
and prove both sessions remain independently usable.

While both games run, use `nvidia-smi` to show two encoder sessions and verify
audio and controller input route independently to the correct Moonlight client.
End one session and confirm the other session, its audio, and its controller
remain alive. Finally restart Wolf and repeat the persistence checks:

```bash
sudo systemctl restart docker-wolf
sudo systemctl is-active --quiet docker-wolf
```

Verify Edgar's Steam login and complete
`/srv/wolf/data/profile-data/user/WolfSteam` app home survived, existing
overrides and Moonlight pairing are untouched, and a final short two-session
test still passes.

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

## 10. Portable unlock: the USB key

Nardol travels to LAN parties. Clevis binds Tang at `10.0.0.165`, which is
reachable only from home, and that is the point: network-bound encryption means
a machine that leaves the house does not unlock itself. TPM binding was
considered and rejected for exactly that reason — it would make a stolen machine
self-decrypting.

The portable path is a dedicated USB stick carrying a 4096-byte key at a fixed
raw offset, enrolled in **keyslot 2** on both volumes. Slot 0 (passphrase) and
slot 1 (Clevis) are unchanged.

### What this is, and is not

This is **possession separation, not two-factor authentication**. Nardol stolen
alone stays protected. Nardol and the stick taken from the same bag open both
volumes with no Tang and no human secret. Therefore, as operational policy:
transport and store the stick separately from the machine, insert it only to
boot, remove it after unlock, and keep the sealed offline copy in a third
location.

The stick **is a key**. Never use it for file transfer, never lend it, and never
initialize, partition, format or "repair" it — any of those destroys the key.

### Unlock order is not a sequence

Once `keyFileTimeout` expires, systemd opens **one** password request, and
Clevis answers that same request concurrently. Do not wait for Clevis to finish
before typing a passphrase; both are racing to satisfy one prompt.

A stick inserted *after* the timeout expires is **not** retried.

`keyFileTimeout` bounds waiting for the device to *appear*. It does not bound
reads from a stick that enumerates and then stalls — a dying stick can still
wedge cryptsetup, and the recovery is to physically remove it.

### Qualify the stick before enrolling

1. Confirm a unique serial and that the `by-id` path survives replug, reboot and
   a different physical port.
2. Run a **destructive full-capacity integrity test** (`f3write`/`f3read` or
   equivalent). Stable enumeration is not enough: counterfeit or failing flash
   that misreports capacity corrupts the key extent silently.
3. Confirm it enumerates through `usb_storage`. `uas` is **not** in
   `boot.initrd.availableKernelModules`; a UAS-only device will not be readable
   in the initrd.

### Provision and enrol

Values are fixed and asserted by `nix flake check`: offset **4194304** (4 MiB),
size **4096**, and a non-zero `keyFileTimeout`.

```bash
# On nardol, with the qualified stick attached.
ID=/dev/disk/by-id/usb-<model>_<serial>-0:0
test "$(lsblk -ndo TRAN "$(readlink -f "$ID")")" = usb   # refuse anything else

dd if=/dev/urandom of=/root/nardol.key bs=4096 count=1
dd if=/root/nardol.key of="$ID" seek=1 bs=4096 count=1 conv=fsync
dd if="$ID" skip=1 bs=4096 count=1 2>/dev/null | sha256sum   # must match the file

for d in /dev/disk/by-partlabel/nardol-root-luks \
         /dev/disk/by-partlabel/nardol-fast-luks; do
  cryptsetup luksAddKey --key-slot 2 "$d" /root/nardol.key
  cryptsetup luksDump "$d" | grep -E '^  [0-9]+: luks2'   # expect 0, 1, 2
done
```

Seal an offline copy of `/root/nardol.key`, then remove it from the host.

**Refresh the header backups immediately.** The stored headers contain slots 0
and 1 only; restoring one later would silently delete the USB slot. Take and
verify fresh off-host backups now, and record them per §12.

Re-running **disko destroys both the Clevis and USB enrolments**. Installer
verification must require slots 0/1/2 and token 0 → slot 1.

## 11. Unlocking away from home

Stage 2 already uses DHCP, so the running system adapts to any network. The
initrd now does too: it takes a **DHCP address only** — no gateway, no DNS, no
router advertisements, no classless routes — and identifies itself as
`nardol-initrd`.

**With the stick:** insert it before powering on. Both volumes unlock
unattended.

**Without the stick, with a laptop on the same LAN:**

1. Find the address. There is no initrd mDNS, and a DHCP hostname is a lease
   label rather than a discovery protocol, so: read the party router's DHCP
   lease table for `nardol-initrd`. If that is unavailable, scan the subnet for
   an open port 2222.
2. `ssh -p 2222 root@<address>` from any RFC1918 client and enter the
   passphrase. The key is forced to the password agent; no shell is possible.

**Without the stick and with no DHCP server at all** (laptop plugged directly
into nardol): there is no address and initrd SSH is unreachable. IPv4 link-local
is deliberately disabled — it would be false comfort, since discovery still
fails. **The console passphrase is the guaranteed path.** Bring a keyboard and a
display, or do not travel without the stick.

ProxyJump via pelargir is **home-LAN recovery only**. It cannot reach a machine
physically at a party.

## 12. Revocation, rotation, and header custody

### Revocation — retire the USB path entirely

Never destroy a credential before proving the survivors.

1. On **both** volumes, prove what will remain:
   `cryptsetup open --test-passphrase --key-slot 0 <dev>`, and confirm Clevis
   slot 1 by an actual Tang-supplied unlock — not by assuming Tang is up.
2. Create and verify rollback headers.
3. Revoke **one volume at a time**: `cryptsetup luksKillSlot <dev> 2`, confirm
   slot 2 is absent in that dump, re-test the surviving paths on that volume
   before touching the next.
4. Cold boot and confirm the machine unlocks without the stick.
5. **Only then** destroy the stick and the sealed copy, and take final verified
   header backups.

### Rotation — replace the key, keep the path

State first which transaction applies: **(a)** rewriting the same device, or
**(b)** replacing it with different media.

0. **Preflight.** Confirm slot 3 is free on both volumes and slots 0/1/2 are
   present. An occupied slot 3 means a **previous rotation was interrupted** —
   identify what it holds, test it, and complete or unwind that transaction
   before starting a new one. Verify the target `by-id` path resolves to the
   expected serial before any write.
1. Stage the new key in **slot 3** on both volumes.
2. Test slot 3 on both (`--test-passphrase --key-slot 3`).
3. Write the new stick; read back the 4096-byte extent at offset 4194304 and
   compare hashes.
4. Transaction (b) only: update the `by-id` path and run **`nixos-rebuild
   boot` — install the generation, do not merely build it.** A build leaves the
   booted initrd carrying the old path, so the test below would pass for the
   wrong reason. Verify the selected boot entry and its initrd belong to the new
   generation.
5. **Cold boot isolated from Tang** on slot 3: Tang unreachable, no passphrase
   entered. Prove both volumes opened from USB using the §13 evidence rules. A
   boot with Tang reachable proves nothing.
6. Remove **old slot 2** on each volume. This must precede recreating it —
   `luksAddKey --key-slot 2` fails while the slot is occupied.
7. Add the new key explicitly to slot 2 and test it on both volumes.
8. Retain slot 3 until step 7 passes on both — it is the only rollback in this
   window. Then remove slot 3.
9. Final cold boot on slot 2. Refresh and verify headers; retire predecessors.

**Same-stick rotation** is permitted only as *routine* logical rotation, with the
remanence risk accepted explicitly: overwriting the extent cannot erase the old
bytes, because flash translation layers relocate writes. When rotation is
motivated by **compromise**, use different media and physically destroy the old
stick.

### Header and ciphertext custody

Fresh backups do **not** invalidate old ones. Restoring a post-enrolment header
resurrects a revoked key; restoring a pre-USB header deletes a live one. So
"revocation without re-encryption" holds only while every older copy stays under
control.

Maintain **two** artifact registers, because a header and a raw snapshot are not
the same object:

*Header copies* — SHA-256, volume UUID, creation date, slot/key generation,
every location, retention system and its own expiry, custodian, destruction
evidence.

*Ciphertext copies* — artifact type (snapshot / replica / image / dedup
reference), immutable ID or snapshot ID, volume UUID, **volume-key epoch**,
matching header IDs, locations and derivatives, retention, custodian,
destruction evidence.

The volume-key epoch and matching-header link are what let you decide whether an
escaped *pair* exists. Without both registers that decision cannot be made.

**Re-encryption does not "complete" revocation.** Separate the two exposures:

- *Live and future ciphertext*: changing the LUKS volume key neutralises an
  unaccounted old header against what is on the disks now.
- *Historical ciphertext*: an escaped snapshot, together with its matching
  header, stays decryptable by the revoked key **forever**. Re-encrypting the
  live volume cannot reach a copy that already left.

If an old ciphertext copy and the retired credential may both have escaped, do
not declare revocation complete. Record the residual exposure — which volume,
which key generation, which copies are unaccounted — as dated accepted risk.

### What loss actually requires

Recovery of a volume requires **all three**: intact ciphertext on a working
device, **and** a compatible header, **and** at least one credential matching
*that header generation* — slot 0, slot 2 (stick or sealed copy), or slot 1 with
Tang reachable.

A header is a precondition, not an alternative: losing every usable header loses
the volume even with every credential intact, and a restored header is useless
if no credential matching it survives. Media failure or ciphertext damage is
unrecoverable regardless of credentials.

**LUKS recovery is not a data backup.** Off-host data backups are a separate
control and must not be conflated with header custody.

## 13. Unlock drills

Each row is a **separate cold boot** with its own isolation. Never infer the
mechanism from "it booted" — attribute it from the journal.

| # | Drill | Isolation | Expected evidence, per volume |
|---|---|---|---|
| 1 | USB-only | Tang unreachable, no passphrase, stick present | cryptsetup unit succeeds; **no** Clevis success message; no password consumed |
| 2 | Tang-only | stick absent, no passphrase, home LAN | Clevis `Unlocked ... successfully` per device, after the timeout elapses |
| 3 | Console-only | stick absent, Tang unreachable, answer **only** at the console | mapping opens; SSH untouched |
| 4 | SSH-only | stick absent, Tang unreachable, answer **only** over port 2222 | mapping opens; console untouched |
| 5 | No-DHCP console | laptop direct, no DHCP server | no lease; wait-online fails after 20s; console prompt still answerable |
| 6 | Foreign DHCP | party-like subnet | lease obtained; `nardol-initrd` in the lease table; SSH reachable from RFC1918; unlock completes |
| 7 | NEG wrong key | corrupt the extent | key rejected, falls through to prompt, no hang |
| 8 | NEG late insert | insert after the timeout | not retried; falls through |
| 9 | NEG stick pulled | remove mid-unlock | record behaviour and recovery action |

Drills 3 and 4 must be separate boots: both interfaces answer the *same* shared
password request, so satisfying one proves nothing about the other.

Record the exact journal command used for each row. Drill 5 and 6 exist because
those two capabilities are claimed by §11 and are otherwise untested.

`keyFileTimeout` starts at 10s. Measure by-id appearance across every port and
set the final value above the worst observed enumeration time with margin,
normally 10–15s. Then assert the measured value in the contract.

### What the contract can and cannot prove

`nix flake check` proves **declarative intent only**. It cannot assert any live
header state — not slot 0, not slot 2, not a temporary slot 3. Someone can
`luksKillSlot 0` on the live disks with the check still green.

Runtime verification lives here: periodic `luksDump` confirming slots 0/1/2, and
periodic cold-boot `cryptsetup open --test-passphrase` drills. A temporary slot 3
during rotation is expected; final slot numbering is a runbook invariant, not a
contractable one.
