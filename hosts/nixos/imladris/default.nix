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
{ inputs, ... }:
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

    # ⛔ NOT YET IMPORTED — ./secrets.nix.
    #
    # sops-nix derives this host's age identity from its SSH ed25519 host key,
    # so `secrets/imladris.yaml` cannot exist until that key does. Importing the
    # module before the file exists makes `nix flake check` fail on a missing
    # path for everyone, not just on this host. Commissioning step 4 of
    # docs/runbooks/imladris/install.md creates the key, adds the recipient to
    # .sops.yaml, creates the file, and uncomments this line.
    #
    # Until then edgar has key-only SSH and passwordless sudo (see ./system.nix),
    # and there is NO console password. That is one way in, not two — the exact
    # trap pelargir walked into on 2026-08-04. Close it during commissioning.
    # ./secrets.nix

    ../../../modules/nixos/fleet/disk-health.nix
    ../../../users/edgar/default.nix
  ];

  networking = {
    hostName = "imladris";

    # eth0 takes its stable LAN address from a router reservation, matching
    # pelargir. Nothing here is addressed by a hard-coded IP, so a rescue or
    # replacement router needs no edit to this file.
    useDHCP = false;
    interfaces.eth0.useDHCP = true;
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
    # (AAAABBBB0007) for all four bays, so by-id encodes the slot rather than the
    # drive. That is correct for monitoring — Scrutiny records the real serial it
    # reads back, so a drive moved between bays shows up as a changed serial
    # rather than being silently mistaken for its neighbour.
    deviceOverrides = [
      {
        device = "/dev/disk/by-id/usb-ASMT_ASM246X_AAAABBBB0007-0:0";
        type = "sntasmedia";
      }
      {
        device = "/dev/disk/by-id/usb-ASMT_ASM246X_AAAABBBB0007-0:1";
        type = "sntasmedia";
      }
      {
        device = "/dev/disk/by-id/usb-ASMT_ASM246X_AAAABBBB0007-0:2";
        type = "sntasmedia";
      }
      {
        device = "/dev/disk/by-id/usb-ASMT_ASM246X_AAAABBBB0007-0:3";
        type = "sntasmedia";
      }
    ];
  };

  # Key-only SSH plus passwordless sudo, matching nardol and minas-tirith. The
  # second way in — a real console password — arrives with ./secrets.nix.
  security.sudo.wheelNeedsPassword = false;

  # Never change this after the first build; it pins state-migration behaviour
  # and is not a version to keep current.
  system.stateVersion = "26.05";
}
