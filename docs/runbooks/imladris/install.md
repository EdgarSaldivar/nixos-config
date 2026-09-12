# imladris install

Raspberry Pi 5 archive appliance: microSD boot, four-bay USB NVMe enclosure,
mergerfs pool, SMB and Jellyfin. Read [`AGENTS.md`](../../../AGENTS.md) before
deploying anything in this fleet.

This host is deliberately not a k3s node and holds no boot-critical fleet
dependency. Do not give it Tang, Home Assistant, or a cluster role.

## 0. What you need before starting

| Item | Why |
|---|---|
| Raspberry Pi 5, 8 GB | The host |
| Official 27 W USB-C PSU | A 5 V/3 A supply boots but caps downstream USB at 600 mA |
| **Active cooler or fan case** | Four A76 cores on a trickplay pass will thermal-throttle without one |
| microSD, High Endurance class | Root lives here; endurance matters more than peak speed |
| The four-bay enclosure, self-powered | The Pi cannot power four NVMe drives |
| A USB-C cable rated for 10 Gbps or better | See section 2 |
| A 1–2 m USB 2.0 extension for pelargir's Sonoff | See section 1 |

## 1. Physical placement

⛔ **Do this before the enclosure is powered near pelargir.**

A USB 3 link produces broadband noise centred on 2.4 GHz, and Zigbee transmits
at roughly 0–10 dBm against Wi-Fi's 20. On 2026-09-10 this enclosure, connected
to pelargir and **idle**, made every zigbee2mqtt command fail with
`MAC_CHANNEL_ACCESS_FAILURE`; unplugging it restored control within seconds. No
I/O was required — a USB 3 link signals in U0 regardless of traffic.

- Put pelargir's Sonoff coordinator on a **1–2 m USB 2.0 extension**, in open
  air, away from any enclosure, its cable, and the Pi itself. Do this regardless
  of where storage ends up: that stick was already showing `MAC_NO_ACK` errors
  hours before the enclosure existed.
- Site imladris and its enclosure **physically away from pelargir**. A separate
  host does not by itself solve radio interference — RF cares about centimetres,
  not about which machine owns the USB port.
- Route the enclosure's cable away from the coordinator. Prefer a shielded cable.

Verify afterwards by running the section 2 load test while watching
`kubectl -n home logs deployment/zigbee2mqtt` on pelargir for
`MAC_CHANNEL_ACCESS_FAILURE`.

## 2. Commission the enclosure

Do all of this **before** any data is written and before the host is installed.

### 2.1 Prove the link trains and holds SuperSpeed

```sh
for s in /sys/bus/usb/devices/*/; do
  [ -f "$s/idVendor" ] || continue
  [ "$(cat $s/idVendor)" = "174c" ] && echo "$(basename $s) $(cat $s/speed) Mbps"
done
```

Expect `5000`. A reading of `480` means the link fell back to USB 2.0 — roughly
40 MB/s, four times *slower* than the Pi's own gigabit NIC. On this enclosure
that was caused by a cable not fully seated at one end; it presented as a
SuperSpeed link that trained and then dropped after a few seconds. Reseat both
ends, then try a different cable, then the other USB3 port. Both USB3 ports are
on independent controllers on a Pi 5.

### 2.2 Load test

`dd` alone understates the link because `iflag=direct` issues one synchronous
read at a time and never fills the UAS queue. It is still the right stability
test:

```sh
for d in sdb sdc sdd sde; do
  dd if=/dev/$d of=/dev/null bs=4M count=5000 iflag=direct &
done; wait
dmesg -T | grep -iE "reset|uas|abort|disconnect"
```

Clean `dmesg` is the pass condition.

**Acceptance baseline, measured on this Pi and this enclosure 2026-09-11** — not
estimates, and not carried over from another host:

| Measurement | Result |
|---|---|
| Single drive, 20 GB at a 200 GB offset | 384 MB/s |
| Four-way concurrent, 20 GB each at a 100 GB offset | 43.5 MB/s each = **174 MB/s aggregate** |
| Cold-boot enumeration, all four bays | **1.16 s** from kernel start |
| Hot power-cycle recovery | Clean disconnect, all four bays back in 15 s |
| Kernel errors across ~180 GB of reads | **Zero** |
| Drive temperatures under sustained load | 41–50 °C |
| Throttling / under-voltage (`vcgencmd get_throttled`) | `0x0` |

Read the offsets literally: reading the same region twice measures the drives'
own DRAM cache, not the link. An early attempt here produced a four-way
"aggregate" of 584 MB/s — physically impossible across a 5 Gbps link, which caps
at 500 MB/s after 8b/10b encoding — because the test re-read cached regions and,
worse, had picked up a USB stick that had taken an enclosure device letter after
a reboot. Resolve devices by NVMe serial and read untouched offsets.

The four-way figure being under half the single-drive figure is a small-ARM-host
characteristic, not an enclosure fault: the same enclosure on a Mac over
USB4/Thunderbolt does PCIe tunnelling and reads ~843 MB/s from a single bay.
Either number comfortably exceeds this host's 1 GbE NIC, so the link is never the
constraint in service.

Also power-cycle the enclosure, pull and reinsert the cable, and cold-boot the
Pi two or three times, checking `dmesg` after each.

Do **not** pre-emptively set `usb-storage.quirks=174c:2464:u`. That forces
bulk-only transport and would hurt concurrent local work. Apply it only if this
test actually produces aborts.

### 2.3 Prove SMART passthrough

⛔ **The pool has no parity and no redundancy by deliberate choice, so SMART is
the entire early-warning story.** Auto-detection does not work through this
bridge:

```sh
smartctl --scan                    # reports "-d sat" for every bay
smartctl -i -d sat /dev/sdb        # fails: unsupported scsi opcode
smartctl -i -d sntasmedia /dev/sdb # works: full NVMe identity and health
```

NVMe never crosses the USB link — the ASM2464 translates it to SCSI — so real
health needs ASMedia's vendor passthrough. `fleet.diskHealth.deviceOverrides` in
[`hosts/nixos/imladris/default.nix`](../../../hosts/nixos/imladris/default.nix)
encodes this. Confirm all four bays answer before continuing.

Treat "collector succeeded but reported zero disks" as a failure, not as green.

### 2.4 Confirm discard behaviour

`services.fstrim.enable` is on. Whether discard actually reaches the drives
through this bridge is a property of the bridge, not of the NVMe devices.
Confirm with `lsblk -D` once a pool filesystem exists; a bridge without support
makes `fstrim` skip the filesystem rather than silently pretend to succeed.

## 3. Identify every drive before touching anything

⛔ **`/dev/sdX` and `/dev/disk/by-id` are both unsafe for destructive decisions
on this enclosure.** The bridge reports one fake serial for all four bays, so
`usb-ASMT_ASM246X_AAAABBBB0007-0:N` names the **slot**, not the disk. Move a
drive between bays and its by-id path follows the bay.

The only trustworthy identifier is the real NVMe serial:

```sh
for d in sdb sdc sdd sde; do
  echo -n "$d: "
  smartctl -d sntasmedia -i /dev/$d | grep -iE '^(Model Number|Serial Number)'
done
```

Contents observed 2026-09-11:

| Bay | Drive | Serial | Holds | Disposition |
|---|---|---|---|---|
| 0:0 | Crucial CT4000P3PSSD8 | `2336E873EE7A` | Proxmox `pve` LVM | Disposable — first pool member |
| 0:1 | Crucial CT2000P3PSSD8 | `2345E8844F5D` | EFI + APFS | **Copy off first.** Read it on the Mac; Linux APFS drivers are not trustworthy for a sole copy |
| 0:2 | Samsung 970 EVO Plus 2TB | `S6S2NS0T629854Y` | exFAT "Samsung 2tb" | **Confirmed empty** 2026-09-11 — 13 MB of `.fseventsd`/`.Spotlight-V100` only. Disposable |
| 0:3 | Samsung 970 EVO Plus 2TB | `S6S2NS0T629836M` | ESP + LUKS | ⛔ **nardol's migration rollback — do not touch** |

⚠️ Bays 0:2 and 0:3 are the same model and their serials share the first twelve
characters: `S6S2NS0T6298`·`54Y` versus `S6S2NS0T6298`·`36M`. Compare whole
strings. One of the two is nardol's only rollback.

Bay 0:3 stays untouched until the acceptance in
[`../nardol/single-nvme-migration.md`](../nardol/single-nvme-migration.md)
completes. That means a cold boot **with the USB key stick removed**, proving
Tang actually performs the unlock, plus the drills in
[`../nardol/unlock-operations.md`](../nardol/unlock-operations.md). A boot that
unlocks in under the `keyFileTimeout` did not use Tang.

Put a physical label on bay 0:3.

## 4. Secrets

The host age identity derives from the machine's SSH host key, so the key must
exist before there is anything to decrypt.

```sh
# On dol-amroth
ssh-keygen -t ed25519 -N "" -C imladris -f ~/Development/secrets/imladris/ssh_host_ed25519_key
ssh-to-age -i ~/Development/secrets/imladris/ssh_host_ed25519_key.pub
```

1. Add the derived recipient to [`.sops.yaml`](../../../.sops.yaml) as
   `&host_imladris`, plus a `creation_rule` for `secrets/imladris.yaml` naming
   `*admin_edgar` and `*host_imladris`.
2. Create `secrets/imladris.yaml` with `edgar_password_hash` (from
   `mkpasswd -m yescrypt`), `samba_password`, and `restic_password`.
3. Uncomment the `./secrets.nix` import in
   [`hosts/nixos/imladris/default.nix`](../../../hosts/nixos/imladris/default.nix).

⛔ Until step 3 lands, edgar has key-only SSH and passwordless sudo but **no
console password** — one way in, not two. That is the state that made pelargir
unadministrable on 2026-08-04. Do not consider this host finished without it.

## 5. Prepare the install target

1. Read the microSD's stable path on the installer: `ls -l /dev/disk/by-id/ | grep mmc-`
2. Replace `expectedDisk` in
   [`hosts/nixos/imladris/disko.nix`](../../../hosts/nixos/imladris/disko.nix).
   It ships as an obviously invalid placeholder so an unedited config fails
   closed rather than erasing a plausible guess.
3. `git add` the change — flakes only see tracked files.

disko may report exactly one target, the microSD.
`checks/imladris-disko-targets.nix` fails the build if the enclosure appears.

## 6. Format the first pool member by hand

⛔ **disko must never touch the enclosure.** Bay 0:0 is partitioned manually,
once. Confirm the serial immediately before each destructive command:

```sh
smartctl -d sntasmedia -i /dev/sdX | grep -i 'serial'   # must read 2336E873EE7A
```

Create two partitions on that drive — a state partition for Jellyfin's database
and the rest as the first pool branch — and label them exactly:

| Label | Size | Mountpoint | Purpose |
|---|---|---|---|
| `imladris-state` | ~100 GiB | `/var/lib/imladris` | Jellyfin database, config, cache. **Not** a pool branch |
| `imladris-d1` | remainder | `/mnt/pool/d1` | First mergerfs branch |

⛔ **Create every pool filesystem with `-m 0`.**

```sh
mkfs.ext4 -m 0 -L imladris-d1 /dev/disk/by-partlabel/...
```

ext4 reserves 5% of the filesystem for root by default. Across this pool that
hides roughly 200 GB on the 4 TB drive and 100 GB on each 2 TB drive — about half
a terabyte of capacity that **mergerfs cannot even see**, because its placement
policies count space available to unprivileged users.

The reservation exists to stop a full disk from locking out root daemons and to
limit fragmentation on a busy system filesystem. Neither applies to a dedicated,
append-mostly media volume with no root processes writing to it. `minfreespace`
in [`storage.nix`](../../../hosts/nixos/imladris/storage.nix) does the job the
reservation would otherwise do, and does it where mergerfs can act on it.

Labels are the mount identity because by-id cannot be trusted here; the serial
pairing in
[`hosts/nixos/imladris/storage.nix`](../../../hosts/nixos/imladris/storage.nix)
is what authenticates them. `wipefs` the old Proxmox signatures deliberately
before creating the new table, so nothing re-activates itself.

## 7. Install

Follow the same rsync-then-build-on-host sequence the rest of the fleet uses;
`nixos-rebuild` cannot run from the Mac. Place the pre-generated
`ssh_host_ed25519_key` into `/etc/ssh` before first boot so sops decrypts
immediately.

Tailscale needs one interactive `sudo tailscale up` on first activation.

## 8. Commission the services

1. Set the Samba password — it is a separate database from `/etc/shadow`:
   `smbpasswd -a edgar`
2. Create the archive's directory structure under `/srv/archive`. The share is
   read-write by operator decision — see [`media.nix`](../../../hosts/nixos/imladris/media.nix)
   for the risk that accepts while the archive has only one copy.
3. In Jellyfin, create the library as **Home Videos and Photos**, then turn off:
   trickplay (or set keyframe-only), chapter image extraction, and every
   metadata downloader. Disable transcoding per-user so an incompatible client
   fails loudly instead of asking a Pi 5 to software-encode.

⚠️ "Direct play only" is a configuration to enforce, not an intention. Fiber
solves bandwidth, not codec compatibility — image-based subtitles or a browser
codec gap will still request a transcode.

## 9. Acceptance

- `systemctl status imladris-storage-verify` succeeds and names each label with
  its expected serial.
- `findmnt /srv/archive` shows `fuse.mergerfs`, and `/var/lib/imladris` is a
  separate ext4 mount that is **not** a branch of it.
- Stopping the enclosure and rebooting leaves Samba and Jellyfin **not started**
  rather than serving an empty directory. This is the most important test here:
  a mount that fails open would write the archive onto the microSD.
- Scrutiny on minas-tirith shows four imladris devices with their real serials.
- Finder sees `imladris` and mounts both shares; `archive` is read-only.
- A Jellyfin client direct-plays without the server transcoding.

## 8a. Traps this install actually hit

Every one of these cost real time on 2026-09-11 and will recur for anyone
repeating the procedure.

### `nixos-install` exits 0 having done nothing

Debian's `sudo` has `secure_path` that does **not** include
`/nix/var/nix/profiles/default/bin`, so `nix` is not found — and the script
swallows the error and returns success. Always:

```sh
sudo env "PATH=/nix/var/nix/profiles/default/bin:$PATH" nixos-install --flake ...
```

Check the real exit status via `${PIPESTATUS[0]}`, not the pipeline's.

### The Pi vendor kernel is not in any binary cache

At the pinned `nixos-raspberrypi` revision, `linux_rpi-bcm2712-6.18.39` returns
**HTTP 404 from both** `nixos-raspberrypi.cachix.org` and `cache.nixos.org`.
Without intervention the Pi compiles it locally — hours of `make -j4`.

pelargir already has it. Copy it, and note the derivation has **three outputs**:

```sh
# on the Mac, which can reach both hosts (the rescue OS has no private key)
nix copy --no-check-sigs --from ssh-ng://pelargir --to ssh-ng://edgar@<pi> \
  /nix/store/<hash>-linux_rpi-bcm2712-<ver>
nix copy --no-check-sigs --from ssh-ng://pelargir --to ssh-ng://edgar@<pi> \
  /nix/store/<hash>-linux_rpi-bcm2712-<ver>-modules
```

Copying only `out` is **not enough** — Nix still rebuilds the derivation to
produce `modules`, which means a full kernel compile anyway. `dev` is neither
needed nor present (pelargir garbage-collects it after its own install).

The general form is `nix copy '<drv>^*'` to take every output at once.

**Better strategy for next time:** build the whole system on pelargir — same
architecture, same framework pin — and copy the finished closure:

```sh
# on pelargir
SYSTEM=$(nix build --no-link --print-out-paths \
  .#nixosConfigurations.<host>.config.system.build.toplevel)
# on the target
nix copy --no-check-sigs --from ssh-ng://pelargir "$SYSTEM"
sudo nixos-install --root /mnt --system "$SYSTEM"
```

Use `--max-jobs 0` to make Nix **refuse** local builds and name what is missing,
rather than discovering a compile by watching `ps`.

### Killing a runaway build

`systemctl restart nix-daemon` does **not** stop in-flight builds, and neither
does killing `nixos-install`. Builds run as the `nixbld*` users:

```sh
for u in $(getent passwd | grep ^nixbld | cut -d: -f1); do sudo pkill -9 -u "$u"; done
```

An orphaned build holds the derivation lock, so a fresh `nixos-install` sits
waiting on it and appears to hang with no progress.

### No GPT tooling in the base system

Base NixOS ships util-linux's `sfdisk`/`fdisk` but **no `gptfdisk`, no `parted`,
no `partprobe`**. The config now installs them, but a rescue environment will not
have them. `sfdisk` is sufficient:

```sh
sudo sfdisk /dev/sdX <<'EOF'
label: gpt
size=100GiB, name="imladris-state", type=linux
name="imladris-d1", type=linux
EOF
sudo udevadm settle
```

### USB boot becomes ambiguous with the enclosure attached

With `BOOT_ORDER=0xf14` (USB first) the Pi will happily consider the enclosure's
drives, several of which carry bootable-looking partitions. Attaching the
enclosure caused it to fall through to the microSD. Detach the enclosure when you
need the USB install environment specifically.

## 9a. Operating the pool

### Never move files from the union to a member path

```sh
mv /srv/archive/foo /mnt/pool/d1/foo      # ⛔ NEVER
```

This is a documented mergerfs hazard, not a stylistic preference. The union and
the branch are different filesystems, so the move becomes copy-then-delete — and
the delete is issued *through the union*, which can remove the destination you
just created. Read from member paths freely for recovery and inspection; do not
mutate through them.

Creating the same relative path on two branches produces shadowing rather than
two visible files, which is the other reason out-of-band writes are a bad habit.

### Reading the archive from the Mac without the Pi

macOS has no ext4 support, and mergerfs does not run on macOS — so plugging the
enclosure into the Mac normally yields four volumes it declines to mount, and
never the union.

[anylinuxfs](https://github.com/nohajc/anylinuxfs) is the way around that. It
boots a libkrun microVM, mounts the partition with **Linux's own ext4 driver**,
and re-exports it to macOS over localhost NFS — no kernel extension and no
Reduced Security.

```sh
brew tap nohajc/anylinuxfs && brew trust nohajc/anylinuxfs
brew install anylinuxfs
anylinuxfs list
anylinuxfs mount /dev/diskN -o ro
```

⛔ **Read-only, and treat it as a recovery tool.** Its value is that if imladris
dies, the archive stays readable from the Mac without needing another Linux host.
It is not a write path: writing the only copy of irreplaceable data through a
third-party microVM bypasses `force group`, the create masks, and the
serial-verification gate — all of which live on the Linux side.

**Test it against a scratch ext4 partition before real data exists.** Once the
pool is populated, that window is gone. It also mounts one volume per VM, so the
union is not reconstructed automatically.

## 9b. Benchmark the union before trusting it

The mergerfs options in [`storage.nix`](../../../hosts/nixos/imladris/storage.nix)
are a defensible starting point, not a measured optimum. Three are worth testing
on this hardware once a pool exists, in this order.

### Measured FUSE overhead on this hardware, 2026-09-11

Run on the Pi against **tmpfs**, which isolates pure FUSE cost from storage
latency. mergerfs 2.40.2 (Debian) — production runs 2.41.1, which has readdir
improvements, so treat these as a pessimistic floor.

| Test | Direct | Through mergerfs | Ratio |
|---|---|---|---|
| Sequential read, 2 GB | 5.3 GB/s | 3.7 GB/s | 0.70× |
| readdir, names only | 1.2 µs/entry | 1.7 µs/entry | 1.4× |
| readdir + stat, cold | 2.8 µs/entry | 17.3 µs/entry | 6× |
| readdir + stat, warm | 2.8 µs/entry | 2.3 µs/entry | 0.8× |
| `func.getattr=ff` vs `newest` | 0.154 s | 0.163 s | 1.06× |

Conclusions:

- **Streaming is never FUSE-bound.** 3.7 GB/s is ~10× this enclosure's 384 MB/s
  and ~31× the 1 GbE NIC.
- **Cold metadata costs ~6× per entry, not the ~160× an arm64 report suggested.**
  The difference is `cache.readdir` / `cache.entry` / `cache.attr`; do not remove
  them without re-measuring.
- **Warm listing is effectively free** — the FUSE attribute cache answers without
  entering mergerfs.
- **`func.getattr=newest` costs 6%.** The Jellyfin scan correctness it buys is
  essentially free; keep it.

Caveat: tmpfs has no device latency, so absolute cold-scan times on ext4-over-USB
will be higher. The multiplier transfers; the wall clock does not.

### The one that could matter most: I/O passthrough

FUSE I/O passthrough is available on kernel 6.13+ with mergerfs 2.41+, and this
host runs 6.18. Upstream's own test measured native 1.7 GB/s, `cache.files=off`
0.8 GB/s, and passthrough 1.6 GB/s — roughly **95% of native versus 47%**.

```
passthrough.io=ro
cache.files=auto-full
```

⚠️ Not enabled by default here, deliberately. `passthrough.io=rw` **breaks
`moveonenospc`** — mergerfs cannot intercept a write error it never sees. `ro`
should avoid that for write-opened files, but verify ENOSPC behaviour explicitly
before relying on it. Passthrough also requires mergerfs to run as root and
forces page caching on.

Note that upstream's figures come from x86 against tmpfs, where FUSE overhead
dominates. This pool is USB-limited at 384 MB/s, so the real-world gain will be
much smaller — quite possibly invisible behind a 118 MB/s NIC.

### Worth a run each

| Test | Compare |
|---|---|
| `fuse-msg-size` | `1M` against `4M`, especially on a large directory |
| `func.readdir` | `seq` against `cor:4` — concurrent branch reads may not help when four bays share one USB link |

### Isolate before concluding

Upstream's recommended order, each measured separately: direct ext4 branch →
mergerfs over one branch → mergerfs over all four → `nullrw=true` (pure FUSE
cost, no I/O) → Samba over direct ext4 → Samba over mergerfs.

Use a file larger than RAM so the page cache cannot flatter the result, and test
cold and warm. Measure names-only and attributes separately, since they behave
very differently:

```sh
find DIR -maxdepth 1 -printf '%f\n'            # names only
find DIR -maxdepth 1 -printf '%f %s %T@\n'     # attributes — the expensive one
```

### When to drop the union

Concrete thresholds, so this is a decision and not a feeling:

- Sequential SMB reads cannot sustain ~100–105 MB/s while direct ext4 can
- Warm opening of a routinely-browsed folder stays above 1–2 seconds
- Cold opening regularly exceeds ~5 seconds and reorganising is unacceptable
- A no-change Jellyfin scan is consistently 2–3× the direct-mount version
- One core is persistently saturated by mergerfs during ordinary scans

The members are plain ext4 mounted at `/mnt/pool/dN`, so dropping the union means
pointing Samba and Jellyfin at four paths instead of one. **No reformatting.**
That reversibility is why benchmarking first is the right order.

### The architectural fix that beats any tuning

Avoid directories holding many thousands of entries. SMB2 `QueryDirectory`
collects stat, DOS attributes and xattrs **per entry**, and every one crosses
FUSE; Samba's case-insensitive fallback can also scan a whole directory after an
exact-name miss. Hierarchical organisation attacks that at the source, and no
cache setting substitutes for it.

## 10. Known outstanding work

- **The archive has exactly one copy.** restic to minas-tirith is declared in
  secrets but not configured, blocked on capacity — that host's `storage` pool
  was ~91% full as of 2026-09-11. Until a restore has been performed, treat
  everything here as disposable.
- **ext4 and mergerfs provide no end-to-end checksums.** Silent corruption will
  replicate into any backup. Keep a checksum manifest off-host.
- Bays 0:1, 0:2 and 0:3 join the pool only as section 3 clears them.
