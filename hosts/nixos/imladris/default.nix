# imladris — Raspberry Pi 5 archive appliance.
#
# Named for Rivendell, the house where the records of Middle-earth were kept and
# copied. That is this machine's whole job: hold a personal photo/video archive
# on a four-bay USB NVMe enclosure, serve it to the Mac over SMB and to clients
# over Jellyfin, and be boring.
#
# ⛔ WHY THIS HOST EXISTS AT ALL, stated plainly so it is not "consolidated"
# back onto pelargir later by someone who sees an idle Pi with 3.6 GiB free:
#
#   Resources were never the argument. Jellyfin with scans and trickplay
#   restrained is a few hundred MiB, and pelargir has room. The argument is
#   fault containment, and it is not hypothetical — attaching this exact
#   enclosure to pelargir on 2026-09-10 produced two unrelated host-level
#   failures within the hour:
#
#     1. The enclosure's SuperSpeed link desensed the Sonoff Zigbee coordinator
#        plugged into the same Pi. Every zigbee2mqtt command failed with
#        MAC_CHANNEL_ACCESS_FAILURE, with the enclosure IDLE — no I/O required,
#        a USB 3 link signals in U0 regardless. All lighting control was dead
#        until the enclosure was physically unplugged, at which point it
#        recovered within seconds.
#     2. Plugging it in caused pelargir to auto-activate roughly twenty foreign
#        Proxmox LVM volumes carried on the old Crucial (see ./boot.nix).
#
#   pelargir is the SOLE k3s control plane, runs Home Assistant, and is the Tang
#   server that decrypts nardol. Storage is also the one part of this fleet that
#   gets physically handled — drive swaps, bay moves, cable reseats, enclosure
#   power cycles. Those two facts do not belong on the same machine.
#
# This host is deliberately NOT a k3s node, NOT a Tang server, and carries no
# boot-critical fleet dependency. Nothing else may acquire one here.
#
# ⚠️ A SEPARATE HOST DOES NOT BY ITSELF SOLVE THE RF PROBLEM. Radio interference
# cares about centimetres, not about which machine owns the USB port. The Sonoff
# still needs a 1–2 m USB 2.0 extension, and this box should sit physically away
# from pelargir. Both fixes are required; neither substitutes for the other.
{ inputs, lib, ... }:
{
  # Same wrapper contract as pelargir: the board modules only evaluate under
  # nixos-raspberrypi's own `nixosSystem`, which flake.nix supplies via mkNixos's
  # `builder`. See lib/mkHost.nix for why `_module.args` cannot do this.
  imports = with inputs.nixos-raspberrypi.nixosModules; [
    # Pi 5 firmware, vendor kernel, initrd hardware support, loader.
    #
    # Bluetooth is deliberately NOT imported, unlike pelargir. That host needs
    # the BlueZ stack for Home Assistant; this one has no radio role, and an
    # unused 2.4 GHz transmitter next to a USB 3 array is the opposite of what
    # this machine is for.
    raspberry-pi-5.base

    ./disko.nix
    ./boot.nix
    ./system.nix
    ./storage.nix
    ./media.nix
    ./voice.nix
    ./terracompute-ops.nix
    ./terracompute-l2tp.nix

    # ✅ IMPORTED 2026-09-11, closing the one-way-in gap.
    #
    # This line was commented out from the host's creation until now. sops-nix
    # derives the age identity from the SSH ed25519 host key, so
    # `secrets/imladris.yaml` could not exist until that key did, and importing
    # the module before the file exists fails Nix path resolution at EVALUATION
    # time — breaking `nix flake check` for the whole repository, not just this
    # host. That is why it was a comment rather than a mkIf.
    #
    # The recipient is derived from the key imladris generated during its own
    # install (see .sops.yaml), so there is no off-host copy of it. edgar now has
    # two independent routes in: key-only SSH with passwordless sudo, and a real
    # console password for when sshd, networking or the tailnet is broken — the
    # second of which is what pelargir lacked on 2026-08-04, where recovery meant
    # physically unseating the NVMe.
    ./secrets.nix

    ../../../modules/nixos/fleet/disk-health.nix
    ../../../users/edgar/default.nix
  ];

  networking = {
    hostName = "imladris";

    # lan0 is the replacement USB Ethernet adapter, matched below by its
    # hardware MAC. The Pi's built-in NIC remains present as eth0 even when its
    # cable is disconnected, so reusing eth0 here would make interface naming
    # depend on probe order. Nothing here is addressed by a hard-coded IP, so a
    # rescue or replacement router needs no edit to this file.
    useDHCP = false;
    interfaces.lan0.useDHCP = true;
  };

  # The Realtek USB adapter replaces the Pi's built-in Ethernet path. Match the
  # adapter by its immutable MAC instead of its USB-derived kernel name (enu1),
  # then give every DHCP and firewall rule one stable interface name.
  systemd.network.links."10-imladris-usb-lan" = {
    matchConfig.MACAddress = "00:e0:4c:68:0d:8c";
    linkConfig.Name = "lan0";
  };

  fleet.diskHealth = {
    enable = true;
    hostId = "imladris";

    # ⛔ WITHOUT THESE FOUR ENTRIES THIS HOST MONITORS NOTHING, SILENTLY.
    #
    # Measured on the live enclosure 2026-09-11. `smartctl --scan` auto-detects
    # every bay as `-d sat`, and `-d sat` then fails outright:
    #
    #   Read Device Identity failed: scsi error unsupported scsi opcode
    #
    # NVMe does not cross the USB link at all — the ASM2464 bridge translates it
    # to SCSI, so real health needs ASMedia's vendor passthrough (`sntasmedia`),
    # which returns full identity and the SMART log. Scrutiny's default scan
    # therefore reports zero usable devices while succeeding.
    #
    # That matters more here than anywhere else in the fleet: this pool has NO
    # parity and NO redundancy by deliberate choice, so SMART telemetry is the
    # entire early-warning story. A collector that runs clean and sees nothing is
    # the worst available outcome. checks/fleet-disk-health.nix requires these.
    #
    # Addressed by BAY, not by /dev/sdX: the bridge reports one fake serial
    # (AAAABBBB0007) for all four bays, so the path encodes the slot rather than
    # the drive. That is correct for monitoring — Scrutiny records the real
    # serial it reads back, so a drive moved between bays shows up as a changed
    # serial rather than being silently mistaken for its neighbour.
    #
    # ⛔ THESE PATHS MUST BE ENTIRELY LOWERCASE, and that is not a style rule.
    #
    # Scrutiny's collector lowercases each configured device path before it
    # execs smartctl. Measured 2026-09-11, with the natural by-id spelling
    # configured:
    #
    #   configured: /dev/disk/by-id/usb-ASMT_ASM246X_AAAABBBB0007-0:0
    #   executed:   smartctl --info --json --device sntasmedia \
    #                 /dev/disk/by-id/usb-asmt_asm246x_aaaabbbb0007-0:0
    #
    # Linux paths are case-sensitive, so every open failed, every device came
    # back model="" serial="", and the collector logged
    # "has no scrutiny UUID; skipping" then "Sending 0/8 detected devices" —
    # and still EXITED ZERO. Precisely the clean-run-sees-nothing outcome the
    # block above calls the worst available one, arrived at by a different road.
    #
    # ./default.nix's udev rule below creates these aliases. They are lowercase
    # by construction, so the collector's rewrite is a no-op. /dev/disk/by-path
    # would also survive it, but that spelling encodes the USB port as well as
    # the bay — moving the enclosure cable to the Pi's other USB3 socket would
    # silently break monitoring again. The alias keys on bridge identity, so it
    # follows the enclosure to whichever port it is plugged into.
    deviceOverrides = [
      {
        device = "/dev/imladris/bay0";
        type = "sntasmedia";
      }
      {
        device = "/dev/imladris/bay1";
        type = "sntasmedia";
      }
      {
        device = "/dev/imladris/bay2";
        type = "sntasmedia";
      }
      {
        device = "/dev/imladris/bay3";
        type = "sntasmedia";
      }
    ];
  };

  # ---------------------------------------------------------------------------
  # Lowercase, port-independent aliases for the four enclosure bays.
  # ---------------------------------------------------------------------------
  # Consumed by fleet.diskHealth.deviceOverrides above; see the ⛔ note there for
  # why the spelling has to be lowercase. ID_SERIAL is what udev builds the
  # canonical by-id name from, so matching it gives exactly the by-id semantics
  # (bay identity, independent of /dev/sdX enumeration order) under a name the
  # collector will not mangle.
  #
  # The trailing "-0:N" is the SCSI LUN — the bay. The AAAABBBB0007 in the middle
  # is the bridge's fake serial and is identical for all four, which is why these
  # rules must discriminate on the full ID_SERIAL rather than ID_SERIAL_SHORT.
  #
  # Numbered 99 so it runs after udev's own 60-persistent-storage.rules, which is
  # what sets ID_SERIAL in the first place.
  services.udev.extraRules = ''
    SUBSYSTEM=="block", KERNEL=="sd*", ENV{DEVTYPE}=="disk", ENV{ID_SERIAL}=="ASMT_ASM246X_AAAABBBB0007-0:0", SYMLINK+="imladris/bay0"
    SUBSYSTEM=="block", KERNEL=="sd*", ENV{DEVTYPE}=="disk", ENV{ID_SERIAL}=="ASMT_ASM246X_AAAABBBB0007-0:1", SYMLINK+="imladris/bay1"
    SUBSYSTEM=="block", KERNEL=="sd*", ENV{DEVTYPE}=="disk", ENV{ID_SERIAL}=="ASMT_ASM246X_AAAABBBB0007-0:2", SYMLINK+="imladris/bay2"
    SUBSYSTEM=="block", KERNEL=="sd*", ENV{DEVTYPE}=="disk", ENV{ID_SERIAL}=="ASMT_ASM246X_AAAABBBB0007-0:3", SYMLINK+="imladris/bay3"
  '';

  # ---------------------------------------------------------------------------
  # smartd is deliberately OFF here, and it is not ours to begin with.
  # ---------------------------------------------------------------------------
  # nixpkgs' services/monitoring/scrutiny.nix enables services.smartd whenever
  # the collector is enabled, so fleet.diskHealth pulls it in implicitly. On
  # every other host that is harmless — their disks are direct-attached and
  # smartd's auto-detection works.
  #
  # It cannot work here. smartd has no equivalent of the collector's
  # deviceOverrides, so it probes each bay as `-d sat` and gets
  # "not ATA, no IDENTIFY DEVICE Structure" from all four, then exits 17
  # ("No devices to monitor"). That failure made `nixos-rebuild switch` return
  # exit 4 on every deploy — noise that would train the operator to ignore a
  # non-zero rebuild, which is the actually dangerous outcome.
  #
  # Nothing is lost by turning it off: Scrutiny is this fleet's disk-health
  # mechanism and reports to minas-tirith, whereas smartd here has no delivery
  # path at all — no MTA, no notification hook. minas-tirith/zfs.nix makes the
  # same argument about ZED: local logs on a remote box nobody reads are not
  # monitoring.
  services.smartd.enable = lib.mkForce false;

  # Key-only SSH plus passwordless sudo, matching nardol and minas-tirith. The
  # second way in — a real console password — now exists via ./secrets.nix.
  #
  # Kept passwordless by operator decision on 2026-09-11, taken together with
  # the console password rather than separately: the console password is purely
  # the recovery route, and anyone holding the SSH key can already reach the
  # archive. Requiring it for sudo would also risk locking the host out if the
  # sops secret ever failed to decrypt at boot.
  security.sudo.wheelNeedsPassword = false;

  # Never change this after the first build; it pins state-migration behaviour
  # and is not a version to keep current.
  system.stateVersion = "26.05";
}
