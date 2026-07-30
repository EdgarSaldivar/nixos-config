# ============================================================================
#  minas-tirith (raz-server) — ROOT DISK LAYOUT
# ============================================================================
#
#  ⚠️  READ THIS BEFORE EDITING  ⚠️
#
#  This machine has TEN disks. NINE of them are live ZFS pool members holding
#  ~98 TB of irreplaceable data:
#
#    storage   raidz2  7 x 14TB  ata-WDC_*                (sdc,sdd,sde,sdf,sdg,sdh,sdi)
#    storage2  raidz1  2 x 8TB   ata-Samsung_SSD_870_QVO_*  (sda,sdb)
#
#  ALL NINE hang off a single Adaptec Series 7 SAS HBA at PCI 0000:2e:00.0
#  (driver `aacraid`). They are NOT on the AMD FCH SATA controllers.
#
#  The ONLY disk this file may ever reference is the NVMe root device, by its
#  full stable by-id path. Never a bare /dev/sdX or /dev/nvme0n1 — this board
#  has a documented PCI renumbering quirk (hence pci=realloc=off), so short
#  kernel names move between boots.
#
#  Install-time fence — READ CAREFULLY, the obvious approach does not work.
#
#  `modprobe.blacklist=aacraid` on a kernel cmdline does NOT protect you here:
#  nixos-anywhere kexecs its own installer image with its own cmdline, so the
#  blacklist never takes effect and all nine pool disks ARE visible while disko
#  runs. Verified in review. Use a PHASED install instead:
#
#    Step 0 (old system, BEFORE anything else — see ordering note below):
#      docker compose down across all stacks   # release the pools
#      zpool export storage && zpool export storage2
#
#    Step 1  nixos-anywhere --phases kexec ...
#            (stops after booting the installer into RAM)
#
#    Step 2  in the installer, remove the HBA driver and PROVE the disks are gone:
#              rmmod aacraid            # or: echo 0000:2e:00.0 > /sys/bus/pci/drivers/aacraid/unbind
#              lsblk -o NAME,SIZE,MODEL            -> only nvme0n1
#              ls /dev/disk/by-id/ | grep -c '^ata' -> 0
#              zpool import                        -> "no pools available to import"
#            Do not proceed unless all three are as above.
#
#    Step 3  nixos-anywhere --phases disko,install,reboot ...
#
#  The only true hard fence is physical: all nine disks are on ONE PCIe card, so
#  pulling that single Adaptec card removes them completely. If someone can reach
#  the machine, that is strictly safer than any software step above.
#
#  Pool handover ORDERING (getting this wrong costs you the no-force import):
#    The export MUST happen before the kexec, not after. Once the installer is
#    running in RAM the old system is gone and can no longer export anything; the
#    pools would still carry an active ownership claim and the new system would
#    need `zpool import -f`, reintroducing exactly the forced-import footgun that
#    ./zfs.nix exists to avoid.
# ============================================================================
{ config, lib, ... }:
let
  # The one and only disk that may be touched on this host.
  rootDisk = "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R";

  # Written into the ephemeral installer only — NEVER committed to the repo.
  #   printf '%s' 'your-passphrase' > /tmp/disko-password
  passwordFile = "/tmp/disko-password";
in
{
  disko.devices = {
    disk = {
      # Attribute name is deliberately `root` and there is deliberately only
      # one entry. The assertions below fail the build if that changes.
      root = {
        device = rootDisk;
        type = "disk";
        content = {
          type = "gpt";
          partitions = {
            # 2G rather than the old 512M: systemd-boot keeps every generation's
            # kernel+initrd on the ESP, and the old 512M ESP was already tight.
            ESP = {
              priority = 1;
              name = "ESP";
              size = "2G";
              type = "EF00";
              content = {
                type = "filesystem";
                format = "vfat";
                mountpoint = "/boot";
                mountOptions = [ "umask=0077" ];
              };
            };

            # LUKS2 + ext4.
            #
            # Deliberately NOT btrfs: the previous root was btrfs on this exact
            # drive and died with `parent transid verify failed` on both DUP
            # metadata copies — lost writes from 100 unclean shutdowns on a
            # consumer NVMe with no power-loss protection. Single-device DUP
            # metadata gave zero protection because both copies shared the same
            # failed write path. ext4 is simpler and makes no promises it cannot
            # keep here. Snapshots are covered by NixOS generations + backups.
            #
            # Deliberately NOT ZFS-on-root either: ZFS's resilience here comes
            # from redundancy across independent devices, which a single root
            # disk does not have. Revisit if a second NVMe is added as a mirror.
            #
            # LUKS2 (disko default) with argon2id is safe because /boot is the
            # unencrypted ESP read by systemd-boot — no GRUB PBKDF2 constraint.
            luks = {
              priority = 2;
              size = "100%";
              content = {
                type = "luks";
                name = "cr_root";
                inherit passwordFile;
                settings = {
                  allowDiscards = true;
                };
                content = {
                  type = "filesystem";
                  format = "ext4";
                  mountpoint = "/";

                  # Tuned for a DRAM-less consumer SSD with no power-loss
                  # protection, carrying SQLite + PostgreSQL workloads.
                  #   metadata_csum  detect metadata corruption (ext4 does not
                  #                  checksum DATA — accepted tradeoff, mitigated
                  #                  by backups + DB integrity checks)
                  #   -m 1           1% reserve instead of 5%: ~9 GB back on 931 GB
                  #   lazy_*_init=0  initialise up front rather than trickling
                  #                  writes onto a worn drive after install
                  extraArgs = [
                    "-L"
                    "nixos-root"
                    "-m"
                    "1"
                    "-O"
                    "has_journal,extent,dir_index,64bit,metadata_csum"
                    "-E"
                    "lazy_itable_init=0,lazy_journal_init=0"
                  ];

                  # barrier=1 is the default and MUST stay: nobarrier on a drive
                  # with no PLP is how you get the lost writes that destroyed the
                  # previous root. commit=5 bounds the window of dirty data.
                  # nodiscard + weekly fstrim rather than continuous discard,
                  # which adds latency and write amplification on QLC/DRAM-less.
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
    };
  };

  # ---------------------------------------------------------------------------
  # Eval-time guards.
  #
  # These are NOT a fence. They only catch a future *edit* that widens the blast
  # radius. Two limits, both real:
  #   - `assertions` are only enforced when this is evaluated as part of a NixOS
  #     system (system.build.toplevel). A standalone `disko --mode disko <file>`
  #     run bypasses them entirely — and would additionally choke on the unknown
  #     `assertions` option. Always drive disko via the flake's nixosConfiguration
  #     (nixos-anywhere does), never against this file directly.
  #   - What actually protects the pools is Step 2 of the phased install above:
  #     removing aacraid and *proving* the disks are absent before disko runs.
  # ---------------------------------------------------------------------------
  assertions = [
    {
      assertion = builtins.attrNames config.disko.devices.disk == [ "root" ];
      message = ''
        minas-tirith/disko.nix: exactly one disk ("root") may be declared.
        Found: ${lib.concatStringsSep ", " (builtins.attrNames config.disko.devices.disk)}
        Nine of this machine's disks are live ZFS pool members. Do not add them.
      '';
    }
    {
      # Structural, not circular: requires the target to be a stable by-id NVMe
      # path regardless of what `rootDisk` above is edited to. (The previous
      # `device == rootDisk` check was tautological — it compared the value to
      # the very binding it was assigned from.)
      assertion = lib.hasPrefix "/dev/disk/by-id/nvme-" config.disko.devices.disk.root.device;
      message = ''
        minas-tirith/disko.nix: root must be a /dev/disk/by-id/nvme-* path.
          got: ${config.disko.devices.disk.root.device}
        Bare /dev/sdX and /dev/nvme0n1 are forbidden: sda..sdi are ZFS pool
        members, and this board renumbers PCI devices (pci=realloc=off).
      '';
    }
    {
      # Checks every declared device, not just `root`, so an added disk entry
      # cannot smuggle in a pool member. (Serialising the whole disko tree is
      # not possible — disko.devices contains functions and toJSON rejects it.)
      assertion = !(
        lib.any (dev: lib.hasInfix "/dev/sd" dev) (
          lib.mapAttrsToList (_: d: d.device) config.disko.devices.disk
        )
      );
      message = ''
        minas-tirith/disko.nix: a "/dev/sd*" path appears in disko.devices.disk.
        Every /dev/sd* on this host is a live ZFS pool member behind the Adaptec
        HBA at 0000:2e:00.0. Refusing to build.
      '';
    }
    {
      # disko's `zpool` type can CREATE and DESTROY pools. `storage` and
      # `storage2` already exist and must only ever be *imported* (see ./zfs.nix).
      # Declaring any zpool here would put 98 TB inside disko's blast radius.
      assertion = !(config.disko.devices ? zpool) || config.disko.devices.zpool == { };
      message = ''
        minas-tirith/disko.nix: disko.devices.zpool is declared. This host's two
        pools (storage, storage2) already exist and must never be managed by
        disko — they are imported by ./zfs.nix. Remove the zpool declaration.
      '';
    }
  ];
}
