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

```sh
ssh nardol 'sudo install -d -m 0700 /run/edoras-hostkeys/etc/ssh'
cat ~/Development/secrets/edoras/ssh_host_ed25519_key |
  ssh nardol 'sudo install -m 0600 /dev/stdin /run/edoras-hostkeys/etc/ssh/ssh_host_ed25519_key'
cat ~/Development/secrets/edoras/ssh_host_ed25519_key.pub |
  ssh nardol 'sudo install -m 0644 /dev/stdin /run/edoras-hostkeys/etc/ssh/ssh_host_ed25519_key.pub'

ssh nardol 'cd /tmp/edoras-src && sudo nix run --inputs-from . disko#disko-install -- \
  --flake .#edoras --disk root /dev/sdX \
  --extra-files /run/edoras-hostkeys/etc/ssh /etc/ssh'
```

Verify before unplugging, then remove the staged key:

```sh
ssh nardol 'sudo mount /dev/sdX2 /mnt && sudo ssh-keygen -lf /mnt/etc/ssh/ssh_host_ed25519_key.pub && sudo umount /mnt'
ssh nardol 'sudo mount /dev/sdX1 /mnt && sudo ls /mnt/EFI/BOOT /mnt/loader/entries && sudo umount /mnt'
ssh nardol 'sudo rm -rf /run/edoras-hostkeys && sync && echo 1 | sudo tee /sys/block/sdX/device/delete'
```

The fingerprint must match the Mac's copy, and `EFI/BOOT/BOOTX64.EFI` must
exist. Without an NVRAM entry the firmware boots that file.

## 5. On site

1. Fit the NVMe in the board's CPU-attached M.2 slot. Fit both GPUs: the 3090
   in the first x16 slot, the A4000 in the next. The PSU must be 850 W or more.
2. Connect ethernet.
3. In the firmware setup:
   - Secure Boot **off** (systemd-boot is unsigned);
   - CSM / legacy boot off, so it boots UEFI only;
   - the NVMe first in boot order;
   - restore on AC power loss: **Power On**.
4. Plug the PC into the smart plug, then power on.

If it does not appear on the tailnet within five minutes, log in at the console
as `edgar` with the password in `~/Development/secrets/edoras/console_password`
and run `networkctl` and `journalctl -u tailscaled-autoconnect -b`.

## 6. First boot, from anywhere

```sh
tailscale status | grep edoras          # tagged tag:fleet
ssh edoras 'nvidia-smi -L'              # both GPUs, with UUIDs
ssh edoras 'wdctl; ls -l /dev/watchdog*'
ssh edoras 'sudo nixos-generate-config --no-filesystems --show-hardware-config'
```

Commit the probed hardware configuration, the NIC's MAC in `system.nix`, and
both GPU UUIDs as a follow-up change.

## 7. Burn-in

Run both cards at full load together for at least 20 minutes, which is what
proves the PSU, and watch power, clocks and temperature:

```sh
ssh edoras 'nix shell nixpkgs#gpu-burn -c gpu_burn -d 1200' &
ssh edoras 'nvidia-smi dmon -s pucvt -d 10'
ssh edoras 'nvidia-smi -q | grep -A2 -E "PCIe Generation|Link Width"'
```

Any `Xid` in `journalctl -k`, a reboot, or a card throttling on power rather
than temperature is a hardware finding to resolve before any workload moves here.
