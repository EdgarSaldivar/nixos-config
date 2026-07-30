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

# Restore the OLD host keys — keeps known_hosts working AND is what sops decrypts with
scp minas:/storage2/backup-2026-07-30/etc/ssh/ssh_host_ed25519_key      /tmp/extra/etc/ssh/
scp minas:/storage2/backup-2026-07-30/etc/ssh/ssh_host_ed25519_key.pub  /tmp/extra/etc/ssh/
scp minas:/storage2/backup-2026-07-30/etc/ssh/ssh_host_rsa_key          /tmp/extra/etc/ssh/
scp minas:/storage2/backup-2026-07-30/etc/ssh/ssh_host_rsa_key.pub      /tmp/extra/etc/ssh/

# Dedicated initrd host key — NOT the production one. The initrd sits on the
# unencrypted ESP, so anything in it is readable by whoever holds the disk.
ssh-keygen -t ed25519 -N "" -C "minas-tirith-initrd" \
  -f /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key

chmod 600 /tmp/extra/etc/ssh/ssh_host_*_key /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key

# Verify the host key matches the sops recipient in .sops.yaml
ssh-to-age -i /tmp/extra/etc/ssh/ssh_host_ed25519_key.pub
#   must print: <HOST-AGE-RECIPIENT — see .sops.yaml>
```

LUKS passphrase, passed separately so it never touches the repo:

```bash
printf '%s' 'YOUR-LUKS-PASSPHRASE' > /tmp/disko-password   # matches disko.nix passwordFile
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
ssh minas          # NOTE: the alias is `minas` (port 2222), NOT `raz`. Or: edgar@10.0.1.6
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
nix run github:nix-community/nixos-anywhere -- \
  --flake .#minas-tirith \
  --phases kexec \
  --disk-encryption-keys /tmp/disko-password /tmp/disko-password \
  --extra-files /tmp/extra \
  root@<HOST-IP>
```

If kexec fails: the old system is still on disk and will boot normally on reset. Safe abort point.

---

## 5. Phase 2 — THE GATE. Remove the HBA and prove the disks are gone.

In the installer (`ssh root@<HOST-IP>`):

```bash
rmmod aacraid   # or: echo 0000:2e:00.0 > /sys/bus/pci/drivers/aacraid/unbind

lsblk -o NAME,SIZE,MODEL              # MUST show only nvme0n1
ls /dev/disk/by-id/ | grep -c '^ata'  # MUST be 0
zpool import                          # MUST say "no pools available to import"
```

**All three, or stop.** This is what actually protects the pools — the disko assertions are
eval-time guards against config edits, not a fence.

---

## 6. Phase 3 — partition, install, reboot

```bash
nix run github:nix-community/nixos-anywhere -- \
  --flake .#minas-tirith \
  --phases disko,install,reboot \
  --disk-encryption-keys /tmp/disko-password /tmp/disko-password \
  --extra-files /tmp/extra \
  root@<HOST-IP>
```

Watch the reboot on SOL:

```bash
ipmitool -I lanplus -C 17 -H <BMC-IP> -U <BMC-USER> -a sol activate
```

---

## 7. First boot

Root is LUKS. Unlock by **either**:

```bash
ssh -p 2222 root@<HOST-IP>            # initrd SSH
  systemd-tty-ask-password-agent     # prompts for the passphrase
```

or type it at the SOL console. If the initrd never gets an address, `igb` is missing from
`boot.initrd.availableKernelModules` — an assertion in `boot.nix` should have caught that.

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

# Boot resilience — prove BEFORE trusting it
sudo systemctl status healthcheck-ping.timer
sudo systemctl start backup-root-data.service    # first run: expect a daily- snapshot
sudo zfs list -t snapshot -r storage2/backup
```

**Do not start containers until the pools are imported and verified.**

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
