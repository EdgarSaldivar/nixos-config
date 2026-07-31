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

**Every remote step depends on port 2222, and none of the commands below work without this.**
The only route from outside is `minas.saldivar.io:2222 → 10.0.1.6:22`. A bare
`root@<HOST-IP>` means port **22** and will not connect from outside the LAN.

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
```

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
```

Gate: **any new MCE, or corruption_errs above 40, stops the install.** Codex's warning applies —
a lower memory speed must not be used to mask a CPU that still faults.

---

## 2. Prepare the files nixos-anywhere ships

`--extra-files` contents must be exact: **sops depends on the SSH host keys**, and without them
the console password never materialises.

```bash
mkdir -p /tmp/extra/etc/ssh /tmp/extra/etc/secrets/initrd

# Restore the OLD host keys — keeps known_hosts working AND is what sops decrypts with.
#
# ⚠️  scp DOES NOT WORK HERE and this was verified the hard way on 2026-07-30:
# the PRIVATE keys are root-owned mode 0600, `edgar` cannot read them, and plain
# `scp minas:...` fails for exactly the two files that matter while silently
# succeeding for the two .pub files that do not. The install then proceeds with no
# private host key, sops cannot decrypt, and the console password never exists —
# discovered only once you need the console. Pull them through sudo:
for k in ssh_host_ed25519_key ssh_host_ed25519_key.pub \
         ssh_host_rsa_key     ssh_host_rsa_key.pub; do
  ssh minas "sudo cat /storage2/backup-2026-07-30/etc/ssh/$k" > "/tmp/extra/etc/ssh/$k" \
    || { echo "FAILED to fetch $k"; break; }
done

# Non-empty check: `sudo cat` of an unreadable file yields an EMPTY file, not an
# error, so verify size rather than exit status.
for k in ssh_host_ed25519_key ssh_host_rsa_key; do
  [ -s "/tmp/extra/etc/ssh/$k" ] || echo "STOP: /tmp/extra/etc/ssh/$k is EMPTY"
done

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

---

## 4. Phase 1 — kexec only

`modprobe.blacklist=aacraid` does NOT work here: nixos-anywhere kexecs its own image with its own
cmdline. Hence the phased run.

```bash
# --ssh-port AND --post-kexec-ssh-port are BOTH required. nixos-anywhere resets
# to port 22 after the kexec unless told otherwise, and port 22 is not reachable
# from outside — the NAT forward is 2222. Omitting the second flag loses the
# installer immediately after kexec, with the old system already unbootable.
# Pin the revision: this is the tool that erases the root filesystem.
nix run github:nix-community/nixos-anywhere/1.13.0 -- \
  --flake .#minas-tirith \
  --phases kexec \
  --ssh-port 2222 --post-kexec-ssh-port 2222 \
  --disk-encryption-keys /tmp/disko-password /tmp/disko-password \
  --extra-files /tmp/extra \
  root@minas.saldivar.io
```

If kexec fails: the old system is still on disk and will boot normally on reset. Safe abort point.

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
> and no software mistake can reach them. If that can be arranged, do it and the
> rest of this section becomes belt-and-braces.
>
> The gate below is what to do if it cannot.

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
blkid "$REAL" 2>/dev/null | grep -qiE 'zfs_member|linux_raid|LVM2' \
  && die "install target looks like an existing array member"

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
nix run github:nix-community/nixos-anywhere/1.13.0 -- \
  --flake .#minas-tirith \
  --phases disko,install,reboot \
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
in which case systemd runs with **no watchdog, silently** (see `system.nix`).

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
| disko/install (step 6) | **Root is gone.** Pools untouched and backup intact. Recover by mounting a rescue/NixOS ISO via BMC virtual media and re-running the install — provided virtual media was tested first. |
| First boot | SOL console + initrd SSH. Old kernel is gone; systemd-boot has only the new generation. |

**The point of no return is step 6.** Everything before it is reversible.

---
## 10. Known-outstanding

- Container restore (separate plan) — ~429 GB of bind mounts + 49 volumes
- memtest86+ (boot entry exists) — 2 passes minimum, more when convenient
- Replace the NVMe with a PLP model; add a UPS
- `/dev/sdc`: 24 pending + 9 uncorrectable sectors, inside the raidz2
- Delete the temporary BMC automation account
- Test BMC virtual media BEFORE the install (see Rescue reality)
