# Minas fan control

## Configured behavior

`hosts/nixos/minas-tirith/fan-curve.nix` installs a persistent root systemd controller for the RTX 2080 identified by UUID `GPU-8fc251bf-f300-03e1-98eb-d2929b62c23e`. NVML sets both GPU fans to the same duty. It starts at 100%, uses a 50% floor, and takes the highest request from GPU, `MB Temp`, `X570 Temp`, and the hottest of seven SATA disks. The thresholds are in the module's JSON settings: GPU 55/65/75 °C, motherboard 45/50/55 °C, X570 65/75/85 °C, and HDD 40/45/50 °C request 65/80/100%, respectively.

Increases are immediate. Each downward stage requires **all** inputs at least 3 °C below that stage's thresholds for 300 monotonic seconds. The command then decreases by at most five percentage points per minute. Missing, stale, or invalid data commands 100% and resets the cooling timer. Board readings expire after 30 seconds; HDD readings after 180 seconds. A command, NVML, or controller failure requests 100% again; service stop also runs a fresh `--hold-full` process. The controller never restores NVIDIA automatic fan policy.

The `minas-bmc-fans-full` companion is wanted, rather than required, so its failure does not block GPU control. It sets vendor manual mode using raw `0x3a 0xd8` with sixteen `0x01` bytes and requests 100% using `0x3a 0xd6` with sixteen `0x64` bytes. It retries readback through `0x3a 0xda` until the first eight duty bytes are `64` hex. The last eight readback bytes are unused (`00`). There is no BMC, CPU, AIO, or radiator variable curve. `FAN2` may show different RPM at the same duty; RPM alone does not establish physical header mapping. A future radiator curve requires verified fan header and pump mapping first.

The disk inputs use the seven `/dev/disk/by-id/ata-*` identities listed in `fan-curve.nix`; each is read with `smartctl -a -j -d sat`. SMART health exit bits may coexist with a usable temperature, but open or command failure and missing temperature force full speed. Collector threads bound SMART and IPMI calls so slow disk queries do not block the 10 second NVML loop. The unit restarts after failure and has a 45 second watchdog. It needs NVIDIA device access, `/dev/ipmi0`, and the seven disk devices; the units do not restrict those devices.

## Inspect

```sh
systemctl status minas-bmc-fans-full.service minas-gpu-fan-curve.service
journalctl -u minas-bmc-fans-full.service -u minas-gpu-fan-curve.service -b
cat /run/minas-gpu-fan-curve/status.json
```

The atomic status JSON contains each sensor's value, age or error, the computed request, held stage, commanded duty, reported fan percentages, timestamp, and reason. The journal logs changes and at least once per minute. An absent status file means no successful control iteration has written one. Check the service and journal before trusting an old file.

A read-only targeted check of actual sensors and demand, with no fan writes, is:

```sh
sudo /nix/store/<controller>/bin/minas-gpu-fans --config /nix/store/<config>.json --once
```

Use the controller and config paths from `systemctl cat minas-gpu-fan-curve.service` (`MINAS_FAN_CONFIG` names the JSON). For immediate GPU recovery, the explicit command is `sudo /nix/store/<controller>/bin/minas-gpu-fans --config /nix/store/<config>.json --hold-full`; it sets both fans to 100% and does not restore automatic mode. For BMC recovery use the same paths with `--bmc-full`. Check the journal and status afterward. If NVML is unavailable, the GPU command cannot be guaranteed; the BMC companion still requests fixed 100% duty.

## Targeted install and recovery

Stage all fan files in git before building because flakes ignore untracked files. Run `nix flake check` and `bash scripts/closure-equiv.sh .`; expect only the Minas host closure to change. Copy the committed tree to Minas and use an **absolute** flake path. To activate only the fan units without switching the host configuration, build on Minas:

```sh
sudo nix build --no-update-lock-file --no-write-lock-file \
  '/absolute/source#nixosConfigurations.minas-tirith.config.systemd.units."minas-gpu-fan-curve.service".unit' \
  --out-link /nix/var/nix/gcroots/minas-gpu-fan-curve
```

The unit name contains a dot: quote it as `systemd.units."minas-gpu-fan-curve.service".unit` inside the flake selector. Build the matching `minas-bmc-fans-full.service` unit into its own GC root the same way. Inspect the generated unit's `ExecStart` and run that command with `--once` before installation; this reads sensors without changing fans.

Install each generated `<unit-output>/<unit-name>` as a symlink under `/usr/local/lib/systemd/system/`, with a matching `multi-user.target.wants/<unit-name>` symlink to `../<unit-name>`. This administrator unit directory persists across boots and avoids writing into NixOS's immutable `/etc/systemd/system`. Keep both GC roots so the controller closures survive collection. Verify the units with `systemd-analyze verify`, run `systemctl daemon-reload`, and start only the two fan units. Confirm their `FragmentPath`, active state, watchdog notifications, status JSON, GPU fan readback, BMC duty readback, and membership in `systemctl show -p Wants multi-user.target`.

A later authorized full NixOS activation installs the same declared units normally. Once those unit paths are active, remove the temporary administrator symlinks, target wants links, and GC roots. On a controller fault, inspect its journal and use `--hold-full` while repairing it. Stop and restart paths request 100%; never restore NVIDIA automatic policy as recovery.
