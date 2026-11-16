# edoras install

edoras is installed from nardol with its NVMe in a USB enclosure, then moved
into the PC at the friend's house. It joins the tailnet on first power-on, so
the on-site work is hardware and firmware only. Everything here follows the
master-only rule in [`AGENTS.md`](../../../AGENTS.md): the change that declares
the host must be merged before any step below runs.

## 0. Hard stops

- `secrets/edoras.yaml` still decrypts to `PLACEHOLDER` for any value (section 1).
- The host key at `~/Development/secrets/edoras/ssh_host_ed25519_key.pub` does
  not derive to the `host_edoras` recipient in [`.sops.yaml`](../../../.sops.yaml).
- The device about to be handed to disko is not positively the Intel 660p NVMe
  (in its HIKSEMI enclosure) by size, model and serial (section 3). nardol also carries its own NVMe and four
  USB pool disks holding data; disko destroys whatever it is given.
- There is no wired ethernet where the PC will sit. edoras has no Wi-Fi
  configuration and cannot be reached without a cable.

## 1. Secrets gate (on the Mac)

The Tailscale auth key must be one-time, pre-approved and tagged `tag:fleet`.
Put it into the sops file straight from the clipboard, never through a terminal
or a chat:

```sh
cd ~/Development/nixos-config
ssh-to-age -private-key -i ~/.ssh/id_ed25519 -o /tmp/age.key
pbpaste | tr -d '\r\n' | SOPS_AGE_KEY_FILE=/tmp/age.key \
  sops set --value-stdin secrets/edoras.yaml '["tailscale_auth_key"]'
shred -u /tmp/age.key
```

Then decrypt through the consumer's path, edoras's own host key, and confirm no
value is a placeholder without printing any of them:

```sh
ssh-to-age -private-key -i ~/Development/secrets/edoras/ssh_host_ed25519_key -o /tmp/edoras.key
SOPS_AGE_KEY_FILE=/tmp/edoras.key sops -d --output-type json secrets/edoras.yaml |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: v != "PLACEHOLDER" for k, v in d.items()}); print("tskey prefix:", d["tailscale_auth_key"].startswith("tskey-auth-"))'
shred -u /tmp/edoras.key
```

## 2. Ship master to nardol and build

```sh
git fetch origin
git archive origin/master | ssh nardol 'rm -rf /tmp/edoras-src && mkdir /tmp/edoras-src && tar -x -C /tmp/edoras-src'
ssh nardol 'cd /tmp/edoras-src && nix build --no-link --print-out-paths .#nixosConfigurations.edoras.config.system.build.toplevel'
```

## 3. Identify the target, read-only

Plug the enclosure into a rear USB 3 port on nardol. Inside the enclosure the
by-id name is the bridge's, not the NVMe's; the pinned name in
[`disko.nix`](../../../hosts/nixos/edoras/disko.nix) only exists once the drive
is in the PC. So match the USB device by everything the bridge passes through:

```sh
ssh nardol 'lsblk -o NAME,SIZE,MODEL,SERIAL,TRAN,FSTYPE,LABEL,MOUNTPOINTS'
ssh nardol 'sudo smartctl -i -d sntasmedia /dev/sdX'   # the HIKSEMI enclosure is an ASMedia bridge
```

Proceed only if exactly one device is the 1 TB `PSSD` on `usb`, the NVMe
identity behind it is `INTEL SSDPEKNW010T8` with the serial in `disko.nix`, and
nothing on it is mounted.

## 4. Install

Stage the host key in tmpfs on nardol, then install. `--disk root` points disko
at the USB device for this run only; the committed config keeps the in-PC name.

⛔ The staging directory must be **0755**, not 0700. `disko-install` copies it
with `cp -a`, which keeps its mode, so it becomes the installed `/etc/ssh`.
sshd reads `/etc/ssh/authorized_keys.d/<user>` with the user's own
privileges, so a 0700 `/etc/ssh` silently refuses every key. The first install
did exactly this, and it took a console login to fix.

```sh
ssh nardol 'sudo install -d -m 0755 /run/edoras-hostkeys/etc/ssh'
cat ~/Development/secrets/edoras/ssh_host_ed25519_key |
  ssh nardol 'sudo install -m 0600 /dev/stdin /run/edoras-hostkeys/etc/ssh/ssh_host_ed25519_key'
cat ~/Development/secrets/edoras/ssh_host_ed25519_key.pub |
  ssh nardol 'sudo install -m 0644 /dev/stdin /run/edoras-hostkeys/etc/ssh/ssh_host_ed25519_key.pub'
```

disko formats through `/dev/disk/by-partlabel/disk-root-ESP` and
`disk-root-root`. Confirm nothing on nardol already carries those labels, and
dry-run first:

```sh
ssh nardol 'ls /dev/disk/by-partlabel/ | grep -xE "disk-root-(ESP|root)" && echo STOP || echo clear'
ssh nardol 'cd /tmp/edoras-src && sudo nix run --inputs-from . disko#disko-install -- --dry-run \
  --flake .#edoras --disk root /dev/disk/by-id/usb-HIKSEMI_PSSD_<serial>-0:0 \
  --extra-files /run/edoras-hostkeys/etc/ssh /etc/ssh'
```

Then run it for real, the same command without `--dry-run`, as a transient
unit so an SSH drop cannot kill it halfway:

```sh
ssh nardol 'cd /tmp/edoras-src && sudo systemd-run --unit=edoras-install --working-directory=/tmp/edoras-src \
  /bin/sh -c "PATH=/run/current-system/sw/bin:\$PATH nix run --inputs-from . disko#disko-install -- \
  --flake .#edoras --disk root /dev/disk/by-id/usb-HIKSEMI_PSSD_<serial>-0:0 \
  --extra-files /run/edoras-hostkeys/etc/ssh /etc/ssh > /var/tmp/edoras-install.log 2>&1"'
```

Verify before unplugging, then remove the staged key:

```sh
ssh nardol 'sudo mount -o ro /dev/disk/by-partlabel/disk-root-root /mnt && sudo stat -c "%a %n" /mnt/etc/ssh && sudo ssh-keygen -lf /mnt/etc/ssh/ssh_host_ed25519_key.pub && sudo umount /mnt'
ssh nardol 'sudo mount -o ro /dev/disk/by-partlabel/disk-root-ESP /mnt && sudo ls /mnt/EFI/BOOT /mnt/loader/entries && sudo umount /mnt'
ssh nardol 'sudo rm -rf /run/edoras-hostkeys && sync && echo 1 | sudo tee /sys/block/sdX/device/delete'
```

`/etc/ssh` must be `755`, the fingerprint must match the Mac's copy, and
`EFI/BOOT/BOOTX64.EFI` must exist. Without an NVRAM entry the firmware boots
that file.

## 5. On site

The board is a Gigabyte Z590 AORUS ULTRA with a 10th-gen i9-10850K; the
inventory is in [`hardware-configuration.nix`](../../../hosts/nixos/edoras/hardware-configuration.nix).

1. Fit the NVMe in **M2M_SB**. M2A_CPU, the socket by the CPU, works only with
   11th-gen CPUs; M2P_SB also works but shares bandwidth with SATA port 1.
2. Fit the **3090 in PCIEX16** and the **A4000 in PCIEX4**, the bottom slot.
   Populating PCIEX8 drops the 3090 to x8, and a single-slot blower directly
   under a three-slot card starves its fans. x4 is ample for the A4000's work.
3. Power: the A4000 takes one 6-pin. Give each 3090 socket its own cable; if
   there are not enough, put a split cable's second head on the A4000, never on
   a second 3090 socket. Never use a cable or PSU port marked CPU/EPS.
4. Connect ethernet.
5. In the firmware setup (Gigabyte names):
   - **Boot → Secure Boot:** disabled (systemd-boot is unsigned);
   - **Boot → CSM Support:** disabled, so it boots UEFI only;
   - **Settings → IO Ports → Above 4G Decoding:** enabled, for two large-VRAM cards;
   - **Settings → Platform Power → AC BACK:** Always On;
   - the Intel NVMe first in boot order.
6. Plug the PC into the smart plug, then power on.

If it does not appear on the tailnet within five minutes, log in at the console
as `edgar` with the password in `~/Development/secrets/edoras/console_password`
and run `networkctl` and `journalctl -u tailscaled-autoconnect -b`.

## 6. First boot, from anywhere

Pin the host key on first contact rather than trusting whatever answers:

```sh
echo "edoras $(cut -d' ' -f1,2 ~/Development/secrets/edoras/ssh_host_ed25519_key.pub)" > /tmp/edoras_known_hosts
ssh -o UserKnownHostsFile=/tmp/edoras_known_hosts -o StrictHostKeyChecking=yes edoras 'hostname'
tailscale status | grep edoras          # tagged tag:fleet
ssh edoras 'nvidia-smi -L'              # both GPUs, with UUIDs
ssh edoras 'sudo wdctl; ls -l /dev/watchdog*'
ssh edoras 'journalctl -k -b --grep=gpu_unlock'   # an "unlocked" line for the 3090, 0000:01:00.0
ssh edoras 'sudo vram-temp'             # a plausible GDDR6X reading; needs iomem=relaxed
ssh edoras 'sudo nixos-generate-config --no-filesystems --show-hardware-config'
```

Record any hardware change in `hardware-configuration.nix`, including the GPU
UUIDs, as a follow-up change.

## 7. Burn-in

Both cards at full load together for at least 20 minutes is what proves the
PSU. Run it in two stages, the 3090 capped at 300 W for 5 minutes and then both
at their stock limits for 20, as transient units so an SSH drop does not end it
mid-run.

nixpkgs evaluates `gpu-burn` only in a CUDA-enabled package set:

```sh
ssh edoras 'nix build --impure --no-link --print-out-paths --expr \
  "with import (builtins.getFlake \"nixpkgs\") { config = { allowUnfree = true; cudaSupport = true; cudaCapabilities = [ \"8.6\" ]; }; }; gpu-burn"'
```

`gpu_burn` takes the duration as its last argument (`gpu_burn 1200`); `-d`
selects doubles and is not a duration. Log every 10 s:

```sh
nvidia-smi --query-gpu=timestamp,index,power.draw,power.limit,temperature.gpu,clocks.sm,fan.speed,clocks_event_reasons.active,pcie.link.gen.current,pcie.link.width.current --format=csv -l 10
sudo vram-temp                          # the 3090's GDDR6X junction; see hosts/nixos/edoras/vram-temp.nix
```

Abort on each card's own limits, which `nvidia-smi -q -d TEMPERATURE` reports,
not on one shared number: the A4000's blower targets 90 C by design (maximum
operating 98 C), while the 3090 targets 83 C (maximum 93 C). Abort the 3090's
memory at about 104 C; GDDR6X throttles near 110 C.

Being held at the power limit (`clocks_event_reasons` `0x4`) is normal.
Thermal slowdown, any `Xid` in `journalctl -k`, a gpu-burn error count above
zero, or the host dropping off the network is a hardware finding to resolve
before any workload moves here.
