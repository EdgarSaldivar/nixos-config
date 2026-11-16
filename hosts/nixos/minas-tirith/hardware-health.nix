# minas-tirith — watchdog and hardware health.
#
# Split out of system.nix on 2026-08-16; contents unchanged. The watchdog and the
# SMART/MCE surfaces belong together: both are "is this machine still sane"
# instrumentation, and both were absent on the box this replaced.
{ config, pkgs, ... }:
{
  # ---------------------------------------------------------------------------
  # Hardware watchdog
  # ---------------------------------------------------------------------------
  # This machine has hung hard before (2026-07-30 `OS Critical Stop`, and a
  # kernel that reached the GRUB prompt and sat there). It is remote, and BMC
  # power-cycling requires a human to notice first. With systemd petting a
  # watchdog, a hung kernel reboots itself instead of waiting to be discovered.
  #
  # ⚠️  UNVERIFIED ON THIS BOARD. `sp5100_tco` is the AMD chipset watchdog and is
  # the usual answer on AM4, but on server boards the BMC frequently owns that
  # hardware and the driver then refuses to bind — in which case systemd finds no
  # /dev/watchdog and runs with NO WATCHDOG AT ALL, silently. The alternative here
  # is the AST2500's own IPMI watchdog (`ipmi_watchdog`).
  #
  # Both modules are loaded and the heartbeat asserts a device actually appeared
  # (see ./monitoring.nix). VERIFY AFTER INSTALL:
  #   ls -l /dev/watchdog*
  #   wdctl                       # shows which driver claimed it
  #   journalctl -b | grep -i watchdog
  boot.kernelModules = [
    "sp5100_tco"
    "ipmi_watchdog"
  ];
  # (`systemd.watchdog.{runtimeTime,rebootTime}` were renamed in 26.05.)
  systemd.settings.Manager = {
    RuntimeWatchdogSec = "30s";
    RebootWatchdogSec = "10min";
  };

  # ---------------------------------------------------------------------------
  # Observability — the old box had none, which is why an outage was found by
  # hand and filesystem damage sat unnoticed between monthly scrubs.
  # ---------------------------------------------------------------------------
  # rasdaemon persists and decodes machine checks. The old host took a fatal
  # uncorrectable MCE (Bank 5 / Execution Unit, PCC set) on 2026-07-28 with
  # nothing recording it.
  #
  # ⚠️  rasdaemon does NOT record the fatal ones on this box. A fatal MCE
  # panics the kernel, survives the reset in the MCA bank registers, and is
  # printed by the next kernel ~2 s into boot, before rasdaemon starts. The
  # heartbeat reads those from the boot journal instead (check 3b in
  # ./scripts/healthcheck-ping.sh).
  hardware.rasdaemon.enable = true;

  # ---------------------------------------------------------------------------
  # Faulty CPU core: keep it offline
  # ---------------------------------------------------------------------------
  # Physical core 1 of this 5950X (logical CPUs 1 and 17, APIC IDs 2 and 3)
  # crashed the machine with fatal uncorrectable machine checks on 2026-11-07
  # (CPU 17) and 2026-11-15 (CPU 1). Both were byte-identical: Bank 5 /
  # Execution Unit, extended code 9 ("scheduling queue parity"), status
  # baa0000000090150, UC + PCC set. All ten corrected/deferred MCEs rasdaemon
  # recorded from 2026-09-08 onward also came from CPU 1. Core 1 is tied for
  # the top preferred-boost rank (acpi_cppc highest_perf 236), so it runs at
  # the highest clocks and voltage, and the scheduler sends single-threaded
  # work there first.
  #
  # Linux cannot keep a CPU from coming up at boot, so offline it as early as
  # possible in stage 2, before any service starts. CPUs are matched by
  # topology core_id rather than by CPU number, so both threads are caught
  # even if SMT is toggled in BIOS. An offline CPU loses its topology
  # directory, so a re-run finds nothing to do; that is success only if
  # something is already offline.
  #
  # ⛔ REMOVE THIS WHEN THE CPU IS REPLACED. This chip exposes no PPIN, so the
  # unit cannot tell a new CPU from this one and would fence off a healthy
  # core. The heartbeat (check 3c) reports if this unit fails.
  systemd.services.minas-offline-faulty-core = {
    description = "Keep faulty CPU core 1 offline (fatal Bank 5 machine checks)";
    wantedBy = [ "sysinit.target" ];
    before = [
      "sysinit.target"
      "shutdown.target"
    ];
    conflicts = [ "shutdown.target" ];
    unitConfig.DefaultDependencies = false;
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    environment.FAULTY_CORE_IDS = "1";
    script = ''
      found=0
      for top in /sys/devices/system/cpu/cpu[0-9]*/topology; do
        [ -r "$top/core_id" ] || continue
        dir=''${top%/topology}
        cpu=''${dir##*/}
        core=$(cat "$top/core_id")
        for want in $FAULTY_CORE_IDS; do
          [ "$core" = "$want" ] || continue
          found=$((found + 1))
          if [ "$cpu" = cpu0 ]; then
            echo "refusing to offline cpu0 (core $core): it cannot be hot-unplugged" >&2
            exit 1
          fi
          echo 0 > "$dir/online"
          echo "offlined $cpu (core $core)"
        done
      done
      offline=$(cat /sys/devices/system/cpu/offline)
      if [ "$found" -eq 0 ] && [ -z "$offline" ]; then
        echo "no online CPU has core_id in '$FAULTY_CORE_IDS' and none are offline" >&2
        exit 1
      fi
      echo "offline CPUs: ''${offline:-none}"
    '';
  };

  # smartd for the NVMe and the nine HBA disks. /dev/sdc has had 24 pending +
  # 9 offline-uncorrectable sectors and is inside the raidz2.
  services.smartd = {
    enable = true;
    autodetect = true;
  };

  # Weekly trim rather than the continuous `discard` mount option: on a
  # DRAM-less QLC-adjacent drive, inline discard adds latency and write
  # amplification. Pairs with `nodiscard` in ./disko.nix.
  services.fstrim = {
    enable = true;
    interval = "weekly";
  };

  # ext4 does not checksum file DATA (only metadata), so silent data corruption
  # would not announce itself the way btrfs did. The compensating controls are
  # backups and database integrity checks — this is the accepted tradeoff of
  # keeping all service data on a single root device.
  #
  # Nightly snapshot of the mutable service data onto the redundant, checksummed
  # ZFS pool. Cheap insurance for the thing ext4 cannot tell us about.
}
