# minas-tirith — install runbook

Replacing openSUSE Tumbleweed on `raz-server` with NixOS 26.05, in place, remotely,
without touching nine live ZFS pool disks.

**Read `disko.nix` first.** Nine of this machine's ten drives hold ~98 TB.

> **This repo is PUBLIC.** Two different categories, do not conflate them:
>
> - **Never committed, placeholders below:** LUKS passphrase, BMC address and
>   credentials, the healthchecks URL, password hashes, SSH private keys. These
>   live in `secrets/minas-tirith.yaml` (sops) or your password manager.
> - **Committed on purpose:** NIC MAC, RFC1918 addresses (`10.0.1.6`), and the
>   root NVMe serial. The config cannot match hardware without them, and none is
>   reachable or usable from outside the LAN. The NVMe serial in particular is
>   *load-bearing safety* — it is what pins disko to the one disk it may erase.


---

## 0. Facts this runbook depends on

| | |
|---|---|
| Host | `<HOST-IP>` (becomes `minas-tirith`) |
| BMC | `<BMC-IP>`, user `<BMC-USER>` — SOL works; **virtual media available via the MegaRAC web UI (ISO mount)** |
| Root disk (ONLY disk to touch) | `/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R` |
| Pool disks — NEVER touch | 9 × behind Adaptec HBA `0000:2e:00.0`, driver `aacraid` |
| Backup | `/storage2/backup-2026-07-30` (file-count verified) |
| Config inventory | `/storage2/safety/inventory/` |
| LUKS header backups | `/root/`, `/storage2/safety/`, and offsite on the Mac |

**Rescue reality:** SOL gives console, and an ISO can be mounted through the MegaRAC web UI, so
an unbootable box is recoverable remotely rather than needing a site visit. That materially lowers
the risk of step 6.

**Test it once before starting.** Mount any ISO, boot it, confirm you reach a shell, power off.
Ten minutes. "I can use IPMI" is a reasonable belief until it is demonstrated, and the moment you
would otherwise discover it is false is immediately after the root filesystem has been erased.

---

## 0.5 Client setup — DO THIS FIRST, it gates every later step

There are **two** routes, and the real install used the second:

1. `minas.saldivar.io:2222 → 10.0.1.6:22` — the public double-NAT forward. Works from
   anywhere, but adds `--ssh-port` / `--post-kexec-ssh-port` to every nixos-anywhere call.
2. **`10.0.1.6:22` directly over WireGuard** — verified working for the entire 2026-08-06
   install. Fewer moving parts and no port flags. Prefer it whenever WG is up; fall back
   to (1) if it is not.

A bare `root@<HOST-IP>` means port **22**, which is correct for route 2 and wrong for
route 1 — do not mix them.

Add to `~/.ssh/config` (each stanza on its own lines — a side-by-side layout is not
valid ssh_config and OpenSSH rejects it):

```sshconfig
Host minas
    HostName minas.saldivar.io
    Port 2222
    User edgar

Host minas-initrd
    HostName minas.saldivar.io
    Port 2222
    User root
    HostKeyAlias minas-initrd

Host minas-install
    HostName minas.saldivar.io
    Port 2222
    User root
    HostKeyAlias minas-install

# Route 2 — direct over WireGuard. What the 2026-08-06 install actually used.
Host minas-direct
    HostName 10.0.1.6
    Port 22
    User edgar

Host minas-initrd-direct
    HostName 10.0.1.6
    Port 22
    User root
    HostKeyAlias minas-initrd
```

> ### ⚠️ The initrd listens on port **22**, not 2222
> `boot.nix` moved it (see the comment there): the external forward lands on internal
> port 22, so an initrd on 2222 meant **nothing answered on 22 during initrd** — remote
> unlock from outside, the entire reason initrd SSH exists on a box an hour away, was
> broken. Sharing port 22 is safe because the initrd sshd and the stage-2 sshd never run
> at the same time. `HostKeyAlias` is what keeps their two different host keys apart.

`HostKeyAlias` is what makes this work: the initrd, the installer and the booted
system all answer on the same host:port with **three different host keys**. Without
distinct aliases each one looks like a MITM warning and `StrictHostKeyChecking`
may refuse outright — at exactly the moment you need in.

**Operator shell.** `edgar` logs into **fish**, but every block in these runbooks is
bash. Start operational sessions with:

```bash
ssh -t minas 'exec env BASH_NO_FISH=1 bash -il'
```

**Record the initrd fingerprint now**, so a real key change is distinguishable from
the expected one:

```bash
ssh-keygen -lf /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key.pub
```

---

## 1. Pre-flight gates — ALL must pass

```bash
# Hardware is stable (memory now at 2666 = AMD spec for 4x dual-rank)
ipmitool -I lanplus -C 17 -H <BMC-IP> -U <BMC-USER> -a sensor get VCCM   # expect ~1.20V, ok
ssh minas 'sudo dmesg | grep -c "Machine Check:"'                       # expect 0
ssh minas 'sudo btrfs device stats / | grep corruption'                 # expect 40, NOT climbing

# Backup still intact
ssh minas 'sudo find /storage2/backup-2026-07-30 -type f | wc -l'

# Config evaluates
nix eval .#nixosConfigurations.minas-tirith.config.system.build.toplevel.drvPath

# ⛔ THE AUTHORITATIVE WIPE LIST. Read this, not the config comments.
# disko never scans the machine — it iterates ONLY the disks declared in
# disko.devices.disk, filtered by each disk's `destroy` flag:
#     lib.catAttrs "device" (filterAttrs (n: d: d.destroy) devices.disk)
# This prints that exact list from the same source of truth. Anything shown here
# WILL be partitioned and formatted. Anything not shown is never touched.
nix eval --json .#nixosConfigurations.minas-tirith.config.disko.devices.disk \
  --apply 'ds: builtins.map (d: { inherit (d) device; destroy = d.destroy or true; }) (builtins.attrValues ds)'
#   MUST print EXACTLY one entry, the Samsung NVMe:
#   [{"destroy":true,"device":"/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R"}]
#   More than one entry, or any ata-*/sd* path, means STOP.
```

Gate: **any new MCE, or corruption_errs above 40, stops the install.** Codex's warning applies —
a lower memory speed must not be used to mask a CPU that still faults.

---

## 2. Prepare the files nixos-anywhere ships

`--extra-files` contents must be exact: **sops depends on the SSH host keys**, and without them
the console password never materialises.

> ## ⛔ DO NOT RESTORE THE OLD HOST KEYS FROM THE BACKUP
>
> Earlier revisions of this step pulled `ssh_host_*` out of
> `/storage2/backup-2026-07-30/etc/ssh/` "to keep known_hosts working". **That is
> now wrong and it is the exact failure it was written to prevent.**
>
> `c8870ee` (2026-08-05) REGENERATED this host's identity, because the rebuild
> boots the NixOS ISO directly and never boots openSUSE — so the old keys are
> unrecoverable, and the sops recipient in `.sops.yaml` was re-derived from a
> NEW keypair pre-generated on the Mac.
>
> Ship the old key and the age recipient no longer matches: sops cannot decrypt,
> `edgar-password` never renders, and with a brand-new account both `root` and
> `edgar` are created **locked** on a machine an hour away. That is the pelargir
> lockout, reproduced exactly.
>
> The authoritative key is `~/Development/secrets/minas-tirith/ssh_host_ed25519_key`.

```bash
mkdir -p /tmp/extra/etc/ssh /tmp/extra/etc/secrets/initrd

# The PRE-GENERATED host key — this is the sops age identity (see .sops.yaml).
cp ~/Development/secrets/minas-tirith/ssh_host_ed25519_key     /tmp/extra/etc/ssh/
cp ~/Development/secrets/minas-tirith/ssh_host_ed25519_key.pub /tmp/extra/etc/ssh/

# Non-empty check — a truncated copy fails exactly like a missing one.
[ -s /tmp/extra/etc/ssh/ssh_host_ed25519_key ] || echo "STOP: host key is EMPTY"

# No RSA key is shipped. NixOS generates one on first boot; only the ed25519 key
# has a sops role, so restoring RSA buys nothing.

# Dedicated initrd host key — NOT the production one. The initrd sits on the
# unencrypted ESP, so anything in it is readable by whoever holds the disk.
ssh-keygen -t ed25519 -N "" -C "minas-tirith-initrd" \
  -f /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key

chmod 600 /tmp/extra/etc/ssh/ssh_host_ed25519_key /tmp/extra/etc/ssh/ssh_host_rsa_key \
          /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key
chmod 644 /tmp/extra/etc/ssh/*.pub

# Verify the host key matches the sops recipient in .sops.yaml
ssh-to-age -i /tmp/extra/etc/ssh/ssh_host_ed25519_key.pub
#   must print: <HOST-AGE-RECIPIENT — see .sops.yaml>
```

LUKS passphrase, passed separately so it never touches the repo:

```bash
printf '%s' 'YOUR-LUKS-PASSPHRASE' > /tmp/disko-password   # matches disko.nix passwordFile

# ⚠️ `--disk-encryption-keys REMOTE LOCAL` — the FIRST argument is a DESTINATION
# PATH WRITTEN AS ROOT on the target. `--disk-encryption-keys /dev/sda ./key`
# would literally `cat` your key file over a 14 TB pool member, before disko even
# runs. Both arguments below are /tmp/disko-password on purpose. Check this line
# character by character before pressing enter.
```

---

## 3. Release the pools — BEFORE the kexec

> **If you are taking the straight-to-NixOS path (§4b), openSUSE never boots and this
> section cannot run at all — go to §3b.**

**Ordering is not negotiable.** Once the installer is running in RAM the old system is gone and
can no longer export anything; the pools would keep an active ownership claim and the new system
would need `zpool import -f`, reintroducing the forced-import footgun `zfs.nix` exists to avoid.

> **DONE 2026-07-30 12:05** — all 37 containers are already down (verified: 0 running,
> nothing holding /storage2 but the kernel mount, 49 named volumes intact). Only the
> `zpool export` half of this step remains, immediately before the kexec.

⚠️ **There are SIX stacks, and two of them are NOT under `~/git/docker/`.** An earlier
version of this step looped over `infra media cloud books immich` inside `~/git/docker`
and silently missed both — which would have left containers running, holding the pools,
making `zpool export` fail at the worst moment.

```bash
# `edgar` logs into FISH, which cannot parse the bash below. Enter bash explicitly:
ssh -t minas 'exec env BASH_NO_FISH=1 bash -il'
  set -euo pipefail
  # leaf stacks first
  for d in ~/git/docker/media ~/git/docker/books ~/git/docker/cloud \
           ~/git/docker/immich ~/git/gameservers; do
    (cd "$d" && sudo docker compose down)          # NO -v: named volumes must survive
  done

  # then infra. BOTH of these use compose project name `infra` (see collision note below)
  (cd ~/PinCollector/infra   && sudo docker compose down)
  (cd ~/git/docker/infra     && sudo docker compose down)   # traefik lives here — last

  sudo docker ps -q | wc -l        # MUST be 0
  sudo docker volume ls -q | wc -l # MUST still be 49
```

**Project-name collision — `infra` means two different stacks.** `~/git/docker/infra`
(traefik2, host-hostnames) and `~/PinCollector/infra` (minio, api, model-service,
postgres) both default to the compose project name `infra` and share the
`infra_default` network. Consequences, both observed on 2026-07-30:
- `compose down` in one directory does not reliably stop the other's containers —
  `infra-model-service-1` survived both and had to be removed by hand.
- `infra_default` reported *"Resource is still in use"* on both `down` runs.
- **Do NOT use `--remove-orphans` in either directory** — from one stack's perspective
  the other stack's containers *are* orphans, and it will delete them.

After restore, give one of them an explicit `name:` in its compose file (or
`COMPOSE_PROJECT_NAME`) so the two stop colliding.

```bash
  sudo systemctl stop docker.socket docker.service
  sudo zpool export storage
  sudo zpool export storage2
  zpool list          # expect: no pools available
```

### 3b. If openSUSE is ALREADY GONE — the recovery path (used for real 2026-08-06)

The step above assumes the old system is still running. Under the **straight-to-NixOS**
procedure (§4b: boot the ISO via BMC virtual media) it is not, and there is then no
system left that can export its own pools. `zpool import` will show both pools with:

```
status: The pool was last accessed by another system.
action: ... can be imported using its name or numeric identifier and the '-f' flag.
```

That claim is fatal for first boot, not cosmetic: `zfs.nix` sets
`forceImportAll = false` **on purpose**, so `zfs-import-storage.service` runs a plain
`zpool import` and **fails** against a claimed pool.

**Do not "fix" it by setting `forceImportAll = true`.** That permanently defeats the
hostid ownership check on every boot, which is the whole thing `zfs.nix` is avoiding.

Clear the claim from the installer instead — an import/export cycle is ZFS's own
documented remedy, and it reaches an end state identical to exporting from openSUSE:

```bash
# -N = import WITHOUT mounting any dataset. Nothing writes to your data; this only
# rewrites the pool label's ownership record, which is exactly the point.
zpool import -f -N storage
zpool import -f -N storage2
mount | grep -E " /storage| /storage2" || echo "confirmed: nothing mounted"

zpool export storage
zpool export storage2

# VERIFY the claim is gone. Anything else and first boot will fail:
zpool import 2>&1 | grep -q 'last accessed by another system' \
  && echo "❌ CLAIM STILL PRESENT" || echo "✅ cleared — will import with NO -f"
```

Run this **before** the HBA-removal gate in §5 (the gate makes the disks invisible,
so the pools have to be released first). A residual
`status: Some supported features are not enabled` afterwards is benign — that is a
`zpool upgrade` suggestion, not an ownership claim.

---

## 4. Phase 1 — kexec only

> **This whole section applies ONLY when kexec-ing out of a running openSUSE. The
> 2026-08-06 rebuild did not — see §4b, and do not pass `--phases kexec` on that path.**

`modprobe.blacklist=aacraid` does NOT work here: nixos-anywhere kexecs its own image with its own
cmdline. Hence the phased run.

```bash
# --build-on remote is REQUIRED FROM THIS MAC, and this was verified, not assumed:
#     $ nix build --impure --expr 'with import <nixpkgs> { system = "x86_64-linux"; }; ...'
#     error: a 'x86_64-linux' ... is required to build, but I am a 'aarch64-darwin'
# There is no linux-builder, no /etc/nix/machines, and extra-platforms lists only
# x86_64-darwin. nixos-anywhere defaults to --build-on auto, which is DOCUMENTED to
# fall back to remote — but the target has 128 GB RAM and a 16-core CPU and builds
# it perfectly well, so state it explicitly rather than depending on autodetection
# for the one command that cannot be retried after step 6.
# (Idea taken from a friend's nix-config, which does the same in scripts/nixos-anywhere.sh.)
#
# --ssh-port AND --post-kexec-ssh-port are BOTH required. nixos-anywhere resets
# to port 22 after the kexec unless told otherwise, and port 22 is not reachable
# from outside — the NAT forward is 2222. Omitting the second flag loses the
# installer immediately after kexec, with the old system already unbootable.
# Pin the revision: this is the tool that erases the root filesystem.
nix run github:nix-community/nixos-anywhere/1.13.0 -- \
  --flake .#minas-tirith \
  --phases kexec \
  --build-on remote \
  --ssh-port 2222 --post-kexec-ssh-port 2222 \
  --disk-encryption-keys /tmp/disko-password /tmp/disko-password \
  --extra-files /tmp/extra \
  root@minas.saldivar.io
```

If kexec fails: the old system is still on disk and will boot normally on reset. Safe abort point.

---

## 4b. Straight-to-NixOS via BMC virtual media — THE PATH ACTUALLY USED (2026-08-06)

`c8870ee` moved this host to a **straight-to-NixOS** rebuild: mount the NixOS ISO through
the MegaRAC web UI and boot it directly, rather than kexec-ing out of a running openSUSE.
If you take this path, §4 above **does not apply at all** — there is no kexec phase, and
`--phases kexec` must not be passed.

Consequences, each of which bit during the real run:

- **openSUSE never boots**, so §3 cannot export the pools → use **§3b**.
- The installer generates its **own ephemeral SSH host key**, so `10.0.1.6` presents a
  key matching neither the old openSUSE key nor the pre-generated production key. That
  is expected, not a MITM. Identify the machine by disk models and pool layout before
  trusting it, and re-pin `known_hosts` per stage (installer → initrd → booted system,
  three different keys).
- `--build-on remote` still applies: the target has 32 threads and 125 GB RAM.

The installer boots with **root having no SSH key**, so install one before dispatching:

```bash
# from the Mac; the installer root password is whatever the ISO/console was set to
ssh-copy-id -o StrictHostKeyChecking=no root@10.0.1.6
ssh root@10.0.1.6 'echo OK'
```

**Address the box at `root@10.0.1.6` port 22 over WireGuard, not `minas.saldivar.io:2222`.**
The direct path was verified working for the entire real install and removes the
double-NAT forward, `--ssh-port` and `--post-kexec-ssh-port` from the equation. Keep
`minas.saldivar.io:2222` as the fallback if WG is down.

### ⛔ 4c. BUILD THE CLOSURE **BEFORE** DISKO — learned the hard way

**This step did not exist on 2026-08-06 and its absence cost the whole install.**

`nixos-anywhere --phases disko,install,reboot` partitions the disk **first** and builds
the system closure **after**. So a build failure lands *after* the root filesystem is
already gone — the box sits formatted, with no bootable OS, an hour away.

That is exactly what happened: `tailscale 1.98.9` in the pinned nixpkgs had a stale
`vendorHash`, its `go-modules` FOD is not in `cache.nixos.org` so it could not be
substituted past, and the mismatch failed the entire closure through `system-path`.
disko had already completed.

Prove the closure builds while the old system is still recoverable:

```bash
# on the TARGET (it has the CPU and RAM), before any destructive phase:
nix build --no-link --print-out-paths \
  "git+file:///path/to/nixos-config#nixosConfigurations.minas-tirith.config.system.build.toplevel"
```

Any failure here is free. The same failure after §6 is not.

If a stale `vendorHash` does appear, **do not simply paste in the observed hash** —
that accepts whatever the build host fetched. Check whether upstream already fixed it
on the same release branch (`nixos-26.05` shipped tailscale 1.98.10 with a published
hash) and bump the pin instead, so the value is upstream-verified. Re-run the §1 wipe-list
eval afterwards: a nixpkgs change re-evaluates disko too, and that gate must never be
assumed to carry over.

---

## 5. Phase 2 — THE GATE. Remove the HBA and prove the disks are gone.

In the installer (`ssh minas-install`, i.e. port 2222 — NOT a bare `root@<ip>`):

> ## ⛔ READ THIS BEFORE STEP 5
>
> An independent cold review of this procedure concluded: **do not run it remotely
> with the nine data drives still attached.** Not because the config is wrong — it
> is correctly scoped to the NVMe — but because *every* software fence here is
> fail-open in some scenario, and the consequence is 98 TB with no backup.
>
> **The strongest available fence is physical: power off and unplug the Adaptec
> HBA.** Nine drives on one card; pulling it makes them electrically unreachable
> and no software mistake can reach them.
>
> **DECISION 2026-07-30: the HBA will NOT be disconnected.** The machine is an hour
> away and a trip was judged not worth it. That is a deliberate, informed choice —
> recorded here so it is not mistaken for an oversight. It means the gate below is
> the *primary* protection rather than a backup for it, so **do not skip or
> shortcut any of its five checks**, and stop on the first `GATE FAILED`.

An earlier version of this gate was **fail-open and reported false success** —
verified by execution, not theory. It hardcoded `0000:2e:00.0`, and:
- `[ -e /sys/.../0000:2e:00.0 ] && echo 1 > .../remove` **silently skips** the
  removal if the installer enumerates the HBA at a different address (the generic
  kexec image does not inherit `pci=realloc=off`);
- `test ! -e /sys/.../0000:2e:00.0 && echo "PCI function GONE"` then **prints
  success precisely because that address is absent** — while nine drives sit at a
  different address;
- `cmd && echo` does **not** abort under `set -e`, so failed checks merely printed
  nothing and execution continued.

The version below discovers the HBA dynamically and **aborts on every failure**.

```bash
set -euo pipefail
die() { echo "❌ GATE FAILED: $*" >&2; exit 1; }

# 1. DISCOVER — never trust a hardcoded PCI address in the installer kernel.
mapfile -t BOUND < <(ls -d /sys/bus/pci/drivers/aacraid/0000:* 2>/dev/null || true)
echo "aacraid-bound functions: ${BOUND[*]:-none}"
for f in "${BOUND[@]:-}"; do
  [ -n "$f" ] || continue
  echo "${f##*/}" > /sys/bus/pci/drivers/aacraid/unbind
done
udevadm settle

modprobe -r aacraid 2>/dev/null || true
udevadm settle

# 2. HOT-REMOVE every RAID-class (0x0104) PCI function still present.
for d in /sys/bus/pci/devices/*/; do
  cls=$(cat "$d/class" 2>/dev/null || echo "")
  case "$cls" in 0x0104*) echo "hot-removing ${d%/}"; echo 1 > "$d/remove" || true ;; esac
done
udevadm settle

# 3. PROOFS — each one ABORTS. No `&& echo` anywhere.
lsblk -dn -o NAME,SIZE,MODEL

n_sd=$(lsblk -dn -o NAME | grep -c '^sd' || true)
[ "$n_sd" -eq 0 ] || die "$n_sd /dev/sd* device(s) still visible"

n_ata=$(ls /dev/disk/by-id/ 2>/dev/null | grep -c '^ata-' || true)
[ "$n_ata" -eq 0 ] || die "$n_ata ata-* by-id link(s) still present"

! lsmod | grep -q '^aacraid ' || die "aacraid module still loaded"

zpool import 2>&1 | grep -q 'no pools available' || die "zpool import still sees pools"

T=/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R
[ -e "$T" ] || die "install target $T does not exist"
REAL=$(readlink -f "$T"); echo "target resolves to: $REAL"
case "$REAL" in /dev/nvme*) ;; *) die "target resolves to $REAL — NOT an NVMe" ;; esac

# 4. The target must not itself be part of any existing array.
#
# ⚠️ FIXED 2026-08-06. This was written as:
#     blkid "$REAL" | grep -qiE '...' && die "..."
# Under `set -e` that construct returns NON-ZERO on the SUCCESS path — when grep
# finds nothing (the good case), the whole && list exits 1. As the last statement
# in the gate it made a passing gate look like a failing one, and the reflex fix
# ("just add || true") would have silently disarmed the check entirely.
# An explicit if-block makes pass and fail unambiguous in both directions.
if blkid "$REAL" 2>/dev/null | grep -qiE 'zfs_member|linux_raid|LVM2'; then
  die "install target looks like an existing array member"
fi

echo "✅ ALL GATE CHECKS PASSED — safe to run phase 3"
```

If any line prints `GATE FAILED`, **stop**. Do not "just re-run it" — understand why
first. The disko guards are eval-time protection against a bad *config edit*; they
cannot help when the wrong disk is physically present and correctly named.

---

**The point of no return is step 6.** Everything before it is reversible.

---

## 6. Phase 3 — partition, install, reboot

```bash
# ⚠️ NOTE: disko HAS an interactive confirmation that lists the exact devices it is
# about to wipe and requires you to type "yes" — but you will NOT see it here.
# nixos-anywhere builds `system.build.diskoScript`, which uses disko's
# `_legacyDestroy` ("Does not ask for confirmation! Deprecated in favor of
# _destroy"). The prompt lives in the newer `destroyFormatMount` attribute.
# That is why the eval-printed wipe list in step 1 is the gate that matters: it is
# the only place a human sees the device list before it is acted on.
nix run github:nix-community/nixos-anywhere/1.13.0 -- \
  --flake .#minas-tirith \
  --phases disko,install,reboot \
  --build-on remote \
  --ssh-port 2222 --post-kexec-ssh-port 2222 \
  --disk-encryption-keys /tmp/disko-password /tmp/disko-password \
  --extra-files /tmp/extra \
  root@minas.saldivar.io
```

Watch the reboot on SOL:

```bash
ipmitool -I lanplus -C 17 -H <BMC-IP> -U <BMC-USER> -a sol activate
```

---

## 7. First boot

Root is LUKS.

> **These aliases must already exist — see step 0.5, before phase 1.**

Unlock by **either**:

```bash
ssh minas-initrd                     # initrd SSH (external, via the NAT forward)
#   or from inside the LAN:  ssh root@10.0.1.6
  systemd-tty-ask-password-agent     # prompts for the passphrase
```

or type it at the SOL console. If the initrd never gets an address, `igb` is missing from
`boot.initrd.availableKernelModules` — an assertion in `boot.nix` should have caught that.

**Record the initrd fingerprint before you reboot**, so you can tell a real key change from
an expected one:

```bash
ssh-keygen -lf /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key.pub
```

---

## 8. Post-install verification — in this order

```bash
# Identity and sops
hostname                                   # minas-tirith

# ⛔ HARD GATE — DO NOT SKIP, AND DO NOT DEFER.
# mutableUsers = true protects every boot AFTER the first, because /etc/shadow is
# then preserved across a sops failure. It does NOT protect the first: a brand-new
# account still takes its password from the config, so if sops fails during THIS
# install both root and edgar are created locked at "!" and the only console you
# have is one nobody can log into. This is the single moment that must be proven.
sudo ls -l /run/secrets-for-users/         # edgar-password MUST be present and non-empty
sudo test -s /run/secrets-for-users/edgar-password || echo "STOP: sops did not decrypt"
sudo grep -E '^(root|edgar):' /etc/shadow | cut -d: -f1,2 | sed 's/:.*\$/: <hash present>/'
#   BOTH must show a hash. A bare "!" means locked — fix sops BEFORE rebooting.

su - edgar                                 # must actually succeed
# and prove the console path specifically, not just SSH:
#   log in as edgar on the SOL console before you trust the machine unattended

# Pools: `boot.zfs.extraPools` ALREADY IMPORTS THEM AT BOOT (zfs-import-storage.service,
# zfs-import-storage2.service). Do NOT run `zpool import storage` here as a gate —
# on a healthy boot it fails with "a pool with that name already exists", which reads
# as a failure at 1am when it is actually the success case.
sudo zpool list                            # EXPECT: both already listed
sudo zpool status                          # both ONLINE, and the hostid warning GONE
systemctl status zfs-import-storage.service zfs-import-storage2.service   # both active/exited 0

# Only if a pool is genuinely ABSENT from `zpool list`:
#   sudo zpool import storage      # plain, NO -f. If it demands -f, step 3's export was missed.

# Hardware still clean
sudo ras-mc-ctl --summary
ipmitool ... sensor get VCCM               # ~1.20V

# Backup destination must be its OWN dataset — snapshots are per-dataset, and
# without this the nightly backup would land in the storage2 root dataset where
# the rotation would snapshot the wrong thing. Nothing in the config can create
# it (disko is forbidden from touching zpools here), so it is done once, by hand.
# The backup unit aborts loudly with this exact command if it is missing.
sudo zfs create -o mountpoint=/storage2/backup storage2/backup
sudo zfs list storage2/backup                    # confirm before trusting the timer
```

### ⛔ HARD GATE — prove the heartbeat actually reaches you

Checking that the *timer* is scheduled proves nothing: the script deliberately exits 0
even when curl fails (a dead endpoint must never wedge the box), so a missing secret, a
stale or deleted check URL, a DNS/egress block, or a disabled integration **all** leave
the timer looking perfectly green while nothing is monitored. A brand-new Healthchecks
check that never receives a ping just sits in "New" and will not alert on the outage it
was created for. This is the only outward signal this machine has.

> **At THIS point the heartbeat will legitimately report UNHEALTHY** — there is no
> backup stamp yet and zero containers are running. So the up→down→up test cannot
> be performed here; every ping would be a `/fail` and prove nothing. Split it:
> **now** prove DELIVERY, and **after the restore** prove the TRANSITION.

**Now — prove the ping physically reaches healthchecks.io:**

```bash
sudo systemctl start healthcheck-ping.service
sudo journalctl -u healthcheck-ping -n 30 --no-pager
#   "WARNING: ... did not deliver"  => BROKEN (bad URL, DNS, egress, or secret)
#   no WARNING                      => the request left the box
```
Then confirm on healthchecks.io that the check registered a ping **just now** (it will
show as Down with the UNHEALTHY body — that is expected and correct at this stage).
If nothing arrived, stop and fix it: this is the machine's only outward signal.

**Watchdog — verify, do not assume.** `sp5100_tco` may lose the hardware to the BMC,
in which case systemd runs with **no watchdog, silently** (see `hardware-health.nix`).

```bash
ls -l /dev/watchdog*                                    # must exist
wdctl 2>/dev/null | head                                # which driver claimed it
sudo journalctl -b | grep -i watchdog | head

sudo systemctl start backup-root-data.service    # first run: expect a daily- snapshot
sudo zfs list -t snapshot -r storage2/backup
```

**Do not start containers until the pools are imported and verified.**

> ### 🚫 DO NOT DELETE `/storage2/backup-2026-07-30` UNTIL SERVICES ARE VERIFIED
>
> It is 298 GB and it is the only copy of every container config, database and
> bind mount. It costs nothing to keep and everything to lose. Delete it only
> after the restore is complete AND you have logged into Plex, Jellyfin, Immich
> and Nextcloud and seen real data — not merely "the container is running".
>
> The nightly `backup-root-data` job writes somewhere else entirely
> (`/storage2/backup/minas-tirith` + snapshots), so keeping this one costs you
> nothing but disk.

---

## 9. Rollback

| Fails at | Recovery |
|---|---|
| Pre-flight | Nothing changed. |
| kexec (step 4) | Old system intact on disk — power reset boots it. |
| Gate (step 5) | Abort, reboot, nothing written. |
| disko OK, **closure build fails** (step 6) | **Happened 2026-08-06 (tailscale vendorHash).** Root is gone but the installer is still live in RAM, so the box stays reachable — fix the build and re-run with `--phases install,reboot` (disko is already done and `/mnt` is still mounted; do not re-format). **Do not let it reboot** — there is no OS on disk. §4c exists to catch this before disko. |
| disko/install (step 6) | **Root is gone.** Pools untouched and backup intact. Recover by mounting a rescue/NixOS ISO via BMC virtual media and re-running the install — provided virtual media was tested first. |
| First boot | SOL console + initrd SSH. Old kernel is gone; systemd-boot has only the new generation. |

**The point of no return is step 6.** Everything before it is reversible.

---
## 9b. What actually happened — 2026-08-06 (install COMPLETE)

Recorded so the next reader trusts the corrections above rather than rediscovering them.

| | |
|---|---|
| Path taken | Straight-to-NixOS, ISO via BMC virtual media (§4b). **No kexec.** |
| Transport | `root@10.0.1.6:22` over WireGuard (route 2), not the `:2222` forward |
| Pools | openSUSE was already gone → cleared the claim via §3b `import -f -N` + `export` |
| Result | Both pools imported on first boot with **no `-f`**; hostid warning gone for good |
| Host key | Pre-generated key shipped per §2; sops decrypted, `root` and `edgar` both got real hashes |
| Failure | tailscale 1.98.9 stale `vendorHash` failed the closure **after** disko (§4c) |
| Fix | Bumped `nixpkgs` within `nixos-26.05` → `445d861` (tailscale 1.98.10, published hash). Commit `72f55d5` |
| Final | `minas-tirith`, NixOS 26.05.20260806.445d861, kernel 6.18.42 |
| Verified | sops ✅ · both pools ONLINE no `-f` ✅ · 298 GB backup intact ✅ · heartbeat delivered ✅ · watchdog `SP5100 TCO` armed ✅ · 0 MCEs ✅ · tailscale `tag:fleet`, no key expiry ✅ · k3s agent `Ready` on pelargir ✅ · 0 failed units ✅ |

Two gotchas worth keeping in mind for any repeat:

- **The `uptime` field lies in the NixOS installer.** It reported "up 84 days" while
  `/proc/uptime` said 704 seconds. Trust `/proc/uptime`; the tty idle column is what
  bleeds into that display.
- **Three different host keys answer on the same address** across installer → initrd →
  booted system. Verify by fingerprint at each stage instead of assuming a MITM.

---

## 10. Known-outstanding

- Container restore (separate plan) — ~429 GB of bind mounts + 49 volumes
- memtest86+ (boot entry exists) — 2 passes minimum, more when convenient
- Replace the NVMe with a PLP model; add a UPS
- `/dev/sdc`: 24 pending + 9 uncorrectable sectors, inside the raidz2
- Delete the temporary BMC automation account
- Test BMC virtual media BEFORE the install (see Rescue reality)
