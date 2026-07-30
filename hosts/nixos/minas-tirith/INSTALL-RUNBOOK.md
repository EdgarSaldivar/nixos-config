# minas-tirith — install runbook

Replacing openSUSE Tumbleweed on `raz-server` with NixOS 26.05, in place, remotely,
without touching nine live ZFS pool disks.

**Read `disko.nix` first.** Nine of this machine's ten drives hold ~98 TB.

---

## 0. Facts this runbook depends on

| | |
|---|---|
| Host | `10.0.1.6` (becomes `minas-tirith`) |
| BMC | `10.0.1.88`, user `agent` — SOL works, **virtual media NOT configured** |
| Root disk (ONLY disk to touch) | `/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R` |
| Pool disks — NEVER touch | 9 × behind Adaptec HBA `0000:2e:00.0`, driver `aacraid` |
| Backup | `/storage2/backup-2026-07-30` (file-count verified) |
| Config inventory | `/storage2/safety/inventory/` |
| LUKS header backups | `/root/`, `/storage2/safety/`, and offsite on the Mac |

**Rescue reality:** SOL gives console. Virtual media does not work. If the box ends up
unbootable with no OS on disk, recovery needs the friend physically present. Budget for that
before starting.

---

## 1. Pre-flight gates — ALL must pass

```bash
# Hardware is stable (memory now at 2666 = AMD spec for 4x dual-rank)
ipmitool -I lanplus -C 17 -H 10.0.1.88 -U agent -a sensor get VCCM   # expect ~1.20V, ok
ssh raz 'sudo dmesg | grep -c "Machine Check:"'                       # expect 0
ssh raz 'sudo btrfs device stats / | grep corruption'                 # expect 40, NOT climbing

# Backup still intact
ssh raz 'sudo find /storage2/backup-2026-07-30 -type f | wc -l'

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
scp raz:/storage2/backup-2026-07-30/etc/ssh/ssh_host_ed25519_key      /tmp/extra/etc/ssh/
scp raz:/storage2/backup-2026-07-30/etc/ssh/ssh_host_ed25519_key.pub  /tmp/extra/etc/ssh/
scp raz:/storage2/backup-2026-07-30/etc/ssh/ssh_host_rsa_key          /tmp/extra/etc/ssh/
scp raz:/storage2/backup-2026-07-30/etc/ssh/ssh_host_rsa_key.pub      /tmp/extra/etc/ssh/

# Dedicated initrd host key — NOT the production one. The initrd sits on the
# unencrypted ESP, so anything in it is readable by whoever holds the disk.
ssh-keygen -t ed25519 -N "" -C "minas-tirith-initrd" \
  -f /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key

chmod 600 /tmp/extra/etc/ssh/ssh_host_*_key /tmp/extra/etc/secrets/initrd/ssh_host_ed25519_key

# Verify the host key matches the sops recipient in .sops.yaml
ssh-to-age -i /tmp/extra/etc/ssh/ssh_host_ed25519_key.pub
#   must print: age1n2eqyyehze4wqg270xlqvpqczqn72hwg67a45s0acd9j9rmvtapqlt03da
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

```bash
ssh raz
  cd ~/git/docker
  for d in infra media cloud books immich; do (cd $d && sudo docker compose down); done
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
  root@10.0.1.6
```

If kexec fails: the old system is still on disk and will boot normally on reset. Safe abort point.

---

## 5. Phase 2 — THE GATE. Remove the HBA and prove the disks are gone.

In the installer (`ssh root@10.0.1.6`):

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
  root@10.0.1.6
```

Watch the reboot on SOL:

```bash
ipmitool -I lanplus -C 17 -H 10.0.1.88 -U agent -a sol activate
```

---

## 7. First boot

Root is LUKS. Unlock by **either**:

```bash
ssh -p 2222 root@10.0.1.6            # initrd SSH
  systemd-tty-ask-password-agent     # prompts for the passphrase
```

or type it at the SOL console. If the initrd never gets an address, `igb` is missing from
`boot.initrd.availableKernelModules` — an assertion in `boot.nix` should have caught that.

---

## 8. Post-install verification — in this order

```bash
# Identity and sops
hostname                                   # minas-tirith
sudo ls -l /run/secrets-for-users/         # edgar-password present => sops worked
su - edgar                                 # console password works => not locked out

# Pools: plain import, NO -f. If it demands -f, the export in step 3 was missed.
sudo zpool import storage
sudo zpool import storage2
sudo zpool status                          # both ONLINE, and the hostid warning GONE

# Hardware still clean
sudo ras-mc-ctl --summary
ipmitool ... sensor get VCCM               # ~1.20V

# Boot resilience — prove BEFORE trusting it
sudo systemctl status healthcheck-ping.timer
```

**Do not start containers until the pools are imported and verified.**

---

## 9. Rollback

| Fails at | Recovery |
|---|---|
| Pre-flight | Nothing changed. |
| kexec (step 4) | Old system intact on disk — power reset boots it. |
| Gate (step 5) | Abort, reboot, nothing written. |
| disko/install (step 6) | **Root is gone.** Pools untouched and backup intact, but recovery needs a working install path — friend on site, since virtual media doesn't work. |
| First boot | SOL console + initrd SSH. Old kernel is gone; systemd-boot has only the new generation. |

**The point of no return is step 6.** Everything before it is reversible.

---

## 10. Known-outstanding

- Container restore (separate plan) — ~429 GB of bind mounts + 49 volumes
- memtest86+ (boot entry exists) — 2 passes minimum, more when convenient
- Replace the NVMe with a PLP model; add a UPS
- `/dev/sdc`: 24 pending + 9 uncorrectable sectors, inside the raidz2
- Delete the temporary BMC `agent` account
