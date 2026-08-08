# Disk layout for nardol.
#
# SAFETY: this intentionally declares the Samsung 970 EVO Plus AND the WD
# SN850X. Both are erased by disko. The Crucial P3 Plus is deliberately absent
# and must never be widened into the destructive set by accident.
#
# Devices are referenced by their full serial-qualified by-id path, never by an
# nvmeXnY name. PCIe enumeration order is not stable on this machine.
{
  config,
  lib,
  ...
}:
let
  expectedRootDisk = "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M";
  expectedFastDisk = "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539";

  # This guard is evaluated as part of disko.devices itself, including when
  # building diskoScript. NixOS assertions alone do not protect that path.
  guardDisk =
    role: expected: disk:
    if disk != expected then
      throw ''
        nardol/disko.nix: refusing unexpected ${role} target ${disk}
        The only permitted ${role} target is ${expected}.
        The Crucial P3 Plus must remain outside disko.
      ''
    else
      disk;

  rootDisk =
    guardDisk "root" expectedRootDisk
      "/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_2TB_S6S2NS0T629836M";
  fastDisk =
    guardDisk "fast-data" expectedFastDisk
      "/dev/disk/by-id/nvme-WD_BLACK_SN850X_4000GB_24160W802539";

  # Created only on the ephemeral installer and passed to nixos-anywhere. The
  # passphrase itself must never be committed or placed in the Nix store.
  passwordFile = "/tmp/nardol-disko-password";
in
{
  disko.devices.disk = {
    root = {
      device = rootDisk;
      type = "disk";
      content = {
        type = "gpt";
        partitions = {
          # The Samsung owns Nardol's only ESP; every managed filesystem is
          # otherwise inside LUKS2.
          ESP = {
            priority = 1;
            name = "ESP";
            label = "nardol-esp";
            type = "EF00";
            size = "1G";
            content = {
              type = "filesystem";
              format = "vfat";
              mountpoint = "/boot";
              mountOptions = [ "umask=0077" ];
            };
          };

          root = {
            priority = 2;
            name = "root";
            label = "nardol-root-luks";
            size = "100%";
            content = {
              type = "luks";
              name = "nardol-root";
              inherit passwordFile;
              extraFormatArgs = [
                "--type"
                "luks2"
              ];
              settings = {
                # Required for periodic fstrim to reach the NVMe through
                # dm-crypt. This leaks allocation/free-space patterns, but not
                # file content; that tradeoff is accepted for these SSDs.
                allowDiscards = true;
              };
              content = {
                type = "filesystem";
                format = "ext4";
                mountpoint = "/";
                extraArgs = [
                  "-L"
                  "nardol-root"
                  "-m"
                  "1"
                ];
                # Use periodic fstrim instead of continuous discard.
                mountOptions = [
                  "noatime"
                  "commit=5"
                  "errors=remount-ro"
                  "nodiscard"
                ];
              };
            };
          };
        };
      };
    };

    # High-endurance, high-performance state for Docker/Wolf now and for
    # databases or k3s later. Keeping it at /srv avoids committing the host to a
    # workload-specific top-level layout.
    fast = {
      device = fastDisk;
      type = "disk";
      content = {
        type = "gpt";
        partitions.fast = {
          priority = 1;
          name = "fast";
          label = "nardol-fast-luks";
          size = "100%";
          content = {
            type = "luks";
            name = "nardol-fast";
            inherit passwordFile;
            extraFormatArgs = [
              "--type"
              "luks2"
            ];
            settings.allowDiscards = true;
            content = {
              type = "filesystem";
              format = "ext4";
              mountpoint = "/srv";
              extraArgs = [
                "-L"
                "nardol-fast"
                "-m"
                "1"
              ];
              mountOptions = [
                "noatime"
                "commit=5"
                "errors=remount-ro"
                "nodiscard"
              ];
            };
          };
        };
      };
    };
  };

  # Defense in depth for normal NixOS evaluation. The flake also carries the
  # same invariant so every nix flake check catches an accidental expansion.
  assertions = [
    {
      assertion =
        builtins.attrNames config.disko.devices.disk == [
          "fast"
          "root"
        ];
      message = "nardol: disko must declare exactly the root and fast disks.";
    }
    {
      assertion = config.disko.devices.disk.root.device == expectedRootDisk;
      message = "nardol: disko root target is not the approved Samsung 970 EVO Plus.";
    }
    {
      assertion = config.disko.devices.disk.fast.device == expectedFastDisk;
      message = "nardol: disko fast target is not the approved WD_BLACK SN850X.";
    }
    {
      assertion = (config.disko.devices.zpool or { }) == { };
      message = "nardol: no zpool may be managed by disko.";
    }
    {
      assertion = lib.all (disk: lib.hasPrefix "/dev/disk/by-id/nvme-" disk.device) (
        builtins.attrValues config.disko.devices.disk
      );
      message = "nardol: every install target must use a stable NVMe by-id path.";
    }
  ];
}
