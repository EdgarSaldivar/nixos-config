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
  # ===========================================================================
  #  THE REAL FENCE. Read before touching anything below.
  # ===========================================================================
  # The `assertions` at the bottom of this file are NOT sufficient on their own,
  # and believing otherwise was a genuine near-miss. NixOS assertions are only
  # evaluated as part of `system.build.toplevel` — but nixos-anywhere's disko
  # phase builds and runs `system.build.diskoScript`, which does not consult them
  # at all. Demonstrated on 2026-07-30: with rootDisk pointed at a 14 TB raidz2
  # member, `toplevel` correctly REFUSED while `diskoScript` BUILT SUCCESSFULLY.
  # The assertions were guarding the one path that never partitions anything.
  #
  # `throw` inside this let-binding fires during evaluation of `disko.devices`
  # itself, so it is unavoidable: diskoScript cannot be built, let alone run.
  # NOTE for future edits: this guard can still be defeated by a HIGHER-PRIORITY
  # module (`lib.mkForce`) overriding disko.devices from elsewhere in the module
  # tree, and by nixos-anywhere escape hatches that bypass flake evaluation
  # entirely (`--store-paths <script>`, `--kexec <tarball>`). Neither is used by
  # the documented procedure. If you ever add a mkForce over disko.devices on this
  # host, this fence is gone and you are back to trusting the runbook.
  guardRootDisk =
    d:
    if !(lib.hasPrefix "/dev/disk/by-id/nvme-" d) then
      throw ''
        minas-tirith/disko.nix: root disk must be a /dev/disk/by-id/nvme-* path.
          got: ${d}
        Nine of this machine's ten drives are live ZFS pool members (~98 TB)
        behind the Adaptec HBA at 0000:2e:00.0. Bare /dev/sdX, /dev/nvme0n1 and
        by-id ata-* paths are all forbidden: sd* names move between boots and
        this board renumbers PCI devices (hence pci=realloc=off).
      ''
    else if lib.hasInfix "/dev/sd" d then
      throw "minas-tirith/disko.nix: refusing a /dev/sd* path (${d}) — every one is a ZFS pool member."
    else
      d;

  # The one and only disk that may be touched on this host.
  rootDisk = guardRootDisk "/dev/disk/by-id/nvme-Samsung_SSD_980_1TB_S64ANS0RA05335R";

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
                  # ⚠️  EXPLICIT SECURITY TRADEOFF, not an oversight.
                  # This is what lets TRIM reach the NVMe through dm-crypt.
                  # WITHOUT it the weekly fstrim (see ./hardware-health.nix) is a silent
                  # NO-OP and the drive never learns which blocks are free —
                  # meaningful on a device already at 33% wear. The cost is that
                  # free-space patterns become observable on the raw device,
                  # leaking roughly how much is used and where. Accepted: the
                  # threat model here is a stolen or RMA'd disk, not an attacker
                  # with repeated physical access doing differential analysis.
                  allowDiscards = true;
                };
                content = {
                  type = "filesystem";
                  format = "ext4";
                  mountpoint = "/";

                  # Tuned for a DRAM-less consumer SSD with no power-loss
                  # protection, carrying SQLite + PostgreSQL workloads.
                  #   -m 1           1% reserve instead of 5%: ~9 GB back on 931 GB
                  #   lazy_*_init=0  initialise up front rather than trickling
                  #                  writes onto a worn drive after install
                  #
                  # NOTE: an explicit `-O has_journal,extent,dir_index,64bit,
                  # metadata_csum` list was REMOVED here deliberately. Passing -O
                  # replaces the normal default-feature selection rather than
                  # adding to it, so a hand-maintained list silently freezes the
                  # filesystem at whatever was current when it was written and
                  # misses newer e2fsprogs defaults. Every feature in that list is
                  # already a default on modern mkfs.ext4, so it bought nothing and
                  # could only fall behind. Verify what was actually created:
                  #     dumpe2fs -h /dev/mapper/cr_root | grep -i 'features'
                  # (ext4 checksums METADATA only — never file data. That is the
                  # accepted tradeoff of a single-disk root; the compensating
                  # controls are the ZFS-backed nightly backup and DB integrity
                  # checks. See docs/runbooks/minas-tirith/backup-restore.md.)
                  extraArgs = [
                    "-L"
                    "nixos-root"
                    "-m"
                    "1"
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
  #
  # All four were EXERCISED on 2026-07-30, not merely written — each was made to
  # fire by temporarily breaking the config and confirming evaluation refused:
  #   1. second disk `/dev/sda` added        -> rejected (assertions 1 and 3)
  #   2. rootDisk = "/dev/sda"               -> rejected (assertion 2)
  #   3. rootDisk = a by-id ata-WDC_* member -> rejected (assertion 2)
  #   4. disko.devices.zpool.storage declared-> rejected (assertion 4)
  # Case 3 is the one worth keeping: it is a *stable by-id path*, so a check that
  # only banned bare /dev/sdX would have let a 14 TB pool member through.
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
      assertion =
        !(lib.any (dev: lib.hasInfix "/dev/sd" dev) (
          lib.mapAttrsToList (_: d: d.device) config.disko.devices.disk
        ));
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
