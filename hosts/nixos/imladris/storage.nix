# imladris — the archive pool, and the gate that proves it before anything uses it.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  # ---------------------------------------------------------------------------
  # Pool membership. ONE source of truth for mounts, verification and the check.
  # ---------------------------------------------------------------------------
  # ⛔ IDENTIFIED BY ext4 LABEL, NOT BY /dev/disk/by-id. This deviates from the
  # rule every other host in this repo follows, and the deviation is forced:
  #
  #   The ASM2464 bridge reports ONE fake USB serial (AAAABBBB0007) for all four
  #   bays, so `usb-ASMT_ASM246X_AAAABBBB0007-0:N` names the SLOT, not the disk.
  #   Move a drive between bays and its by-id path follows the bay. The usual
  #   safety property of a serial-qualified path — that it names one specific
  #   physical device — simply does not hold through this enclosure.
  #
  # Labels are chosen by us at mkfs time and stored in the filesystem superblock,
  # so they travel WITH the drive across bays, hosts and reboots. That also means
  # this config can be written before the disks are formatted, instead of landing
  # incomplete and waiting on a second commit for UUIDs that do not exist yet.
  #
  # A label is weaker evidence than a serial, though — it can be duplicated by
  # accident. So every label is paired with the NVMe serial that must be behind
  # it, and imladris-storage-verify below refuses to let anything start until
  # each pairing is proven through `smartctl -d sntasmedia`. Labels locate;
  # serials authenticate.
  #
  # ⚠️ Bays 0:2 and 0:3 are BOTH "Samsung SSD 970 EVO Plus 2TB" and their serials
  # share the first twelve characters — S6S2NS0T6298·54Y versus S6S2NS0T6298·36M.
  # Any comparison here must be exact and whole-string. Never prefix-match.
  members = [
    {
      # ⚠️ MOVED OFF THE CRUCIAL 2026-09-11. This lived on 2345E8844F5D until the
      # bay-2 reformat made relocating it free, and the move was measured rather
      # than assumed. 4K random through this enclosure:
      #
      #                        Crucial QLC      Samsung TLC
      #   read  QD1            6,066 IOPS       6,731 IOPS
      #   write QD1 + fsync    2,606 IOPS       2,707 IOPS
      #   read  QD32           7,462 IOPS      35,000 IOPS
      #
      # At QD1 — which is essentially all SQLite does — they are within 10%, so
      # the database was never the argument. Two other things were. The 4.7x gap
      # at QD32 is what a Jellyfin scan running while Samba serves actually
      # looks like. And the Crucial is DRAM-less QLC whose SLC cache empties
      # under sustained ingest: measured at 56 MB/s and 98% busy while absorbing
      # the archive copy, which is the pool's write bottleneck. Keeping the
      # database off that spindle means DB writes cannot queue behind bulk
      # ingest on the same device.
      label = "imladris-state";
      mountpoint = "/var/lib/imladris";
      serial = "S6S2NS0T629854Y"; # Samsung 970 EVO Plus 2TB, bay 0:2
      pool = false;
    }
    {
      label = "imladris-d1";
      mountpoint = "/mnt/pool/d1";
      serial = "2345E8844F5D"; # Crucial CT2000P3PSSD8
      pool = true;
    }

    {
      # Samsung 970 EVO Plus 2TB. This was nardol's single-NVMe migration
      # rollback — ESP + LUKS — and was held out of the pool until that
      # migration's acceptance closed. Released by the operator 2026-09-11 on
      # the strength of what had by then been proven on nardol: Tang unlock
      # performed with the USB keyfile removed, recovery passphrase verified in
      # slot 0 on both volumes, and both LUKS headers backed up off-host.
      label = "imladris-d2";
      mountpoint = "/mnt/pool/d2";
      serial = "S6S2NS0T629836M";
      pool = true;
    }

    {
      # Samsung 970 EVO Plus 2TB, bay 0:2 — the same physical drive as
      # imladris-state above, partitioned 100G state + 1.7T pool exactly as the
      # Crucial was.
      #
      # This drive held the 427 GB that existed NOWHERE ELSE —
      # downloads/genre/models/sd1/sd2, none of which appeared on the 4 TB drive,
      # so it was very nearly treated as disposable. Released for reformatting
      # 2026-09-11 only after the copy was verified three ways: 14,753 files and
      # 456,164,249,830 bytes identical on both sides, and an rsync dry-run
      # reporting 0 files left to transfer.
      label = "imladris-d3";
      mountpoint = "/mnt/pool/d3";
      serial = "S6S2NS0T629854Y";
      pool = true;
    }

    {
      # Crucial CT4000P3PSSD8, bay 0:0 — the 4 TB, and the last drive to join.
      #
      # It held the 1.4 TiB rescued from the APFS volume, which is why it joined
      # LAST rather than first despite being the largest: the pool had to be big
      # enough to absorb its contents before it could be reformatted. Released
      # 2026-09-12 after the copy was verified three ways — 9,128 files and
      # 1,523,054,875,658 bytes identical, and an rsync dry-run reporting 0 files
      # left to transfer.
      #
      # ⚠️ The most-worn drive in the enclosure: 10% endurance consumed across
      # 126 TB written and 17,777 power-on hours, against 0-1% on the other
      # three. It is also DRAM-less QLC. That combination is why the create
      # policy is pfrd rather than a "most free space" policy — mfs would aim
      # nearly every new write at this drive, since it is both the largest and
      # the emptiest, concentrating ingest on the most-worn and slowest-to-write
      # member of the pool.
      label = "imladris-d4";
      mountpoint = "/mnt/pool/d4";
      serial = "2336E873EE7A";
      pool = true;
    }

  ];

  poolMembers = lib.filter (m: m.pool) members;
  archiveRoot = "/srv/archive";

  # systemd's path-to-unit escaping. No component here contains a literal "-",
  # so "/" -> "-" is the whole transformation; osgiliath needed \x2d because its
  # path did contain one.
  mountUnit = path: (lib.concatStringsSep "-" (lib.tail (lib.splitString "/" path))) + ".mount";
  memberMountUnits = map (m: mountUnit m.mountpoint) members;
  unionMountUnit = mountUnit archiveRoot;

  verifyStorage = pkgs.writeShellScript "imladris-storage-verify" ''
    set -eu

    fail() { echo "ABORT: $*" >&2; exit 1; }

    ${lib.concatMapStringsSep "\n" (m: ''
            # ── ${m.label} → ${m.mountpoint} ──
            dev="$(${pkgs.util-linux}/bin/blkid -L ${lib.escapeShellArg m.label} || true)"
            [ -n "$dev" ] || fail "no filesystem carries label ${m.label}"

            mounted="$(${pkgs.util-linux}/bin/findmnt --raw --noheadings \
              --mountpoint ${lib.escapeShellArg m.mountpoint} --output SOURCE || true)"
            [ -n "$mounted" ] || fail "${m.mountpoint} is not a mountpoint"

            if [ "$(${pkgs.coreutils}/bin/readlink -f "$mounted")" \
               != "$(${pkgs.coreutils}/bin/readlink -f "$dev")" ]; then
              fail "${m.mountpoint} is backed by $mounted, but label ${m.label} is $dev"
            fi

            # The drive behind the label, proven rather than assumed. `smartctl --scan`
            # auto-detects these as -d sat and -d sat then fails with "unsupported scsi
            # opcode"; the ASM2464 needs ASMedia's vendor passthrough. Measured on this
            # enclosure 2026-09-11.
            parent="$(${pkgs.util-linux}/bin/lsblk -no PKNAME "$dev" | ${pkgs.gnused}/bin/sed -n 1p)"
            [ -n "$parent" ] || fail "cannot resolve the parent disk of $dev"

            serial="$(${pkgs.smartmontools}/bin/smartctl -d sntasmedia -i "/dev/$parent" \
              | ${pkgs.gnugrep}/bin/grep -i '^Serial Number:' \
              | ${pkgs.gnused}/bin/sed 's/^[^:]*:[[:space:]]*//' \
              | ${pkgs.coreutils}/bin/tr -d '[:space:]' || true)"
            [ -n "$serial" ] || fail "no NVMe serial readable for /dev/$parent (label ${m.label})"

            # Whole-string comparison. Bays 0:2 and 0:3 are the same model and share a
            # twelve-character serial prefix, so a prefix match would accept the wrong
            # drive — and one of those two is nardol's only rollback.
            if [ "$serial" != ${lib.escapeShellArg m.serial} ]; then
              fail "label ${m.label} sits on serial $serial, expected ${m.serial}"
            fi

            echo "ok: ${m.label} -> ${m.mountpoint} on /dev/$parent (${m.serial})"
      ${lib.optionalString m.pool ''
        # Permissions on the MOUNTED root, set here because this service is the
        # only thing that knows the filesystem is both mounted and the right one —
        # the distinction systemd-tmpfiles cannot make, which is what broke the
        # share (see the tmpfiles block below). Samba forces group `media` with a
        # 0775 directory mask, and edgar is in that group.
        ${pkgs.coreutils}/bin/chown root:media ${lib.escapeShellArg m.mountpoint}
        ${pkgs.coreutils}/bin/chmod 0775 ${lib.escapeShellArg m.mountpoint}
      ''}    '') members}

    echo "imladris: all ${toString (lib.length members)} storage members verified"
  '';
in
{
  # ---------------------------------------------------------------------------
  # Member filesystems.
  # ---------------------------------------------------------------------------
  # `nofail` is deliberately ABSENT, exactly as on osgiliath's Frigate mount. A
  # missing or broken member must fail local-fs.target rather than leaving an
  # empty directory that looks writable while writes actually land on the
  # microSD. That is the failure this whole host is shaped to avoid.
  fileSystems = lib.mkMerge (
    (map (m: {
      ${m.mountpoint} = {
        device = "/dev/disk/by-label/${m.label}";
        fsType = "ext4";
        options = [
          "noatime"
          "nodev"
          "nosuid"
          "errors=remount-ro"
          # The enclosure enumerates over USB and is not present in initrd. A
          # bounded timeout turns "not there yet" into a clean failure instead of
          # a boot that hangs waiting forever on a device that may be unplugged.
          "x-systemd.device-timeout=30s"
        ];
      };
    }) members)
    ++ [
      {
        # ── The union. One addressable path; that was the whole requirement. ──
        #
        # Branches are listed EXPLICITLY rather than globbed as /mnt/pool/*. A
        # glob silently tolerates a branch that failed to mount and serves a
        # partial pool as if it were complete. Explicit branches plus the verify
        # gate below mean a missing member stops the host rather than quietly
        # shrinking the archive.
        ${archiveRoot} = {
          device = lib.concatMapStringsSep ":" (m: m.mountpoint) poolMembers;
          fsType = "fuse.mergerfs";
          options = [
            # ⛔ pfrd, NOT epmfs. This was epmfs and that was a real bug.
            #
            # epmfs = "existing path, most free space": it considers ONLY branches
            # where the parent directory already exists. Any top-level library
            # directory created through the union therefore lands on exactly one
            # branch — whichever had most free space at the time, i.e. the 4 TB
            # Crucial. From then on every path created beneath it sees only that
            # one branch as eligible, and essentially all new writes pin there.
            # Adding a drive later would not help, because epmfs does not
            # replicate existing directory trees onto a new branch.
            #
            # That drive is also the OLDEST and MOST WORN in the enclosure: 10%
            # endurance consumed and ~17,772 power-on hours, against 0–1% on the
            # two Samsungs. So the failure mode was "quietly send every new write
            # to the most tired disk while three healthier ones sit idle with free
            # space". Adding a drive later would not have helped either, because
            # epmfs would not replicate `_ingest` onto the new branch.
            #
            # pfrd spreads creation randomly with probability proportional to free
            # space. The 4 TB still takes the largest share — appropriately, since
            # it has the most room — without being deterministically hammered.
            #
            # Cost, stated plainly: files in one directory may now sit on
            # different drives, so losing a member costs a scatter rather than
            # whole coherent folders. The checksum manifest is what makes "what
            # did I lose?" answerable instead, and it answers it better.
            "category.create=pfrd"

            # Must exceed the largest single file. mergerfs picks a branch at
            # CREATE time, when it cannot know how large the file will become,
            # and it never splits a file across branches. moveonenospc only
            # reacts after a write has already returned ENOSPC, and relocating a
            # nearly-complete large file is an expensive way to recover.
            "minfreespace=100G"
            # Explicit policy rather than the version-dependent boolean shorthand.
            "moveonenospc=pfrd"

            # Current upstream guidance for kernel >= 6.6 with mergerfs >= 2.41,
            # and this host runs 6.18. cache.files=partial duplicates page-cache
            # data through FUSE and can be slower; dropcacheonclose only makes
            # sense alongside enabled file caching.
            "cache.files=off"
            "dropcacheonclose=false"

            # ⚠️ Load-bearing for Jellyfin, not a tuning knob.
            #
            # Library scanners consult directory mtimes to decide what changed.
            # The default func.getattr=ff returns the FIRST branch's instance of a
            # directory, which may be older than an instance on another branch
            # holding the newer files — so an incremental scan silently misses
            # them. `newest` returns the most recently modified instance.
            "func.getattr=newest"

            # streams_xattr in ./media.nix stores Apple metadata in extended
            # attributes, which must reach the underlying ext4. This is the
            # mergerfs default; stated explicitly to document the dependency.
            "xattr=passthrough"

            # ── Metadata caching. This is where the real risk lives. ──────────
            #
            # Streaming is never the problem: one file sits on one branch, that
            # path measured 384 MB/s, and the NIC caps at ~118 MB/s. Directory
            # ENUMERATION is the problem, because SMB2 QueryDirectory needs far
            # more than readdir() supplies — Samba collects stat, DOS attributes
            # and xattrs per entry, and every one of those crosses FUSE.
            #
            # The closest ARM evidence upstream has is a 5,000-file directory
            # listing in 0.01s directly against ~1.6s through uncached mergerfs.
            # That report used an older mergerfs and kernel with unclear cache
            # settings, and longer timeouts largely fixed it — but it is the
            # failure mode to design against.
            #
            # These are safe here specifically because nothing writes to the
            # branches out of band: Samba and Jellyfin both go through the union,
            # and the runbook forbids mutating member paths directly. If that
            # ever stops being true, these timeouts become staleness bugs.
            "cache.readdir=true"
            "cache.entry=30"
            "cache.attr=30"

            # One combined reader/processor thread per core; 0 means "match the
            # core count", so four here. The separate processing pool stays
            # disabled — upstream warns extra parallelism can REDUCE total
            # throughput, and four A76 cores are not a machine to over-thread.
            "read-thread-count=0"
            "process-thread-count=-1"

            # mergerfs' own default, stated explicitly because the ceiling moved:
            # pre-6.13 kernels capped this at 256 pages (1 MiB); 6.13+ allows far
            # more and upstream reports benefits peaking near 4 MiB. This host
            # runs 6.18, so 4M is worth benchmarking — see the runbook. Left at
            # 1M until measured rather than guessed.
            "fuse-msg-size=1M"

            # Samba and Jellyfin run as their own users and must traverse this.
            "allow_other"
            "fsname=imladris-archive"

            # `use_ino` deliberately absent: removed upstream in mergerfs 2.35,
            # which now always manages inode values itself. The default
            # inodecalc=hybrid-hash is the correct choice for Samba and Jellyfin.

            # Nothing may assemble the union until every member is proven.
            "x-systemd.requires=imladris-storage-verify.service"
            "x-systemd.after=imladris-storage-verify.service"
          ];
        };
      }
    ]
  );

  # ---------------------------------------------------------------------------
  # The gate.
  # ---------------------------------------------------------------------------
  # Modelled on osgiliath's frigate-storage-verify. Every consumer of the archive
  # — Samba, Jellyfin, and any future backup job — must carry:
  #   requires = [ "imladris-storage.target" ];
  #   after    = [ "imladris-storage.target" ];
  systemd.targets.imladris-storage = {
    description = "Verified imladris archive storage";
    wantedBy = [ "multi-user.target" ];
    requires = [ "imladris-storage-verify.service" ];
    after = [
      "imladris-storage-verify.service"
      unionMountUnit
    ];

    # ⛔ BindsTo, not just Requires. The verify service is a ONE-SHOT: it proves
    # every member is present and sitting on the right NVMe serial at boot, and
    # then it is finished. That says nothing about the next six months.
    #
    # This enclosure is USB. A cable knock, an enclosure power blip, a bridge
    # reset or a hand-typed umount can remove a member at ANY time while the host
    # keeps running. With Requires alone the target would stay active, mergerfs
    # would keep serving a union with a hole in it, and Samba and Jellyfin would
    # carry on exporting a silently incomplete archive.
    #
    # BindsTo propagates the STOP: lose any member mount and this target goes
    # down, taking the union and every consumer with it. Failing loudly and
    # completely is the correct behaviour for an archive with one copy.
    # ⚠️ The UNION is included deliberately, not just the members.
    #
    # Binding only the members let this target go active while the mergerfs
    # mount had failed — and Samba duly started and exported the bare, empty
    # mountpoint. The members being healthy is not the same as the archive being
    # assembled, and consumers care about the latter. Observed 2026-09-11.
    bindsTo = memberMountUnits ++ [ unionMountUnit ];
  };

  systemd.services.imladris-storage-verify = {
    description = "Verify every imladris storage member by label and NVMe serial";
    requires = memberMountUnits;
    after = memberMountUnits;
    before = [
      "imladris-storage.target"
      # The union mount must not assemble until identity is proven.
      unionMountUnit
      # ⛔ DefaultDependencies=false below is what makes this orderable at all.
      #
      # ${archiveRoot} is a fileSystems entry, so systemd places it in
      # local-fs.target. It waits on THIS service. But a service gets
      # After=sysinit.target by default, and sysinit.target comes after
      # local-fs.target — so the mount waited on a service that waited on the
      # target the mount belongs to. systemd detected the cycle and broke it by
      # deleting local-fs.target, which failed the union mount with
      # result 'dependency' while every member mounted fine. Observed on the
      # first real boot, 2026-09-11.
      "local-fs.target"
    ];
    unitConfig = {
      DefaultDependencies = false;
    };
    conflicts = [ "shutdown.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = verifyStorage;
    };
  };

  # The union's mountpoint and the pool branch directories must exist before
  # their mount units run.
  #
  # ⛔ THESE MODES WERE 0000 UNTIL 2026-09-11, AND THAT MADE THE SHARE UNUSABLE.
  #
  # The reasoning behind 0000 was that a mountpoint's own permissions are
  # invisible while something is mounted over it, so 0000 costs nothing in
  # normal operation and becomes a wall if a member fails to mount. The first
  # half of that is true for READS. It is false for systemd-tmpfiles, which
  # resolves the path and chmods whatever is actually there — the MOUNTED
  # filesystem's root inode, not the hidden directory underneath.
  #
  # So every `systemd-tmpfiles --create`, meaning every boot and every
  # `nixos-rebuild switch`, chmodded the live ext4 roots to 0000. Measured:
  # /mnt/pool/d2 went 0755 -> 0 across one tmpfiles run. And because a chmod on
  # a mergerfs root PROPAGATES TO EVERY BRANCH, the ${archiveRoot} rule alone
  # was enough to zero all of them; the per-branch rules merely compounded it.
  # Result: edgar could not create a single file in the union (0/10 attempts),
  # so the read-write Samba share was dead on arrival.
  #
  # The modes below are therefore the permissions the share actually needs, and
  # tmpfiles now MAINTAINS them instead of destroying them — the propagation
  # that used to be the bug is what keeps the branches correct.
  #
  # What was given up, and why that is acceptable: a branch that fails to mount
  # now leaves a group-writable empty directory rather than an unwritable one.
  # Nothing reaches it. ${archiveRoot} is `x-systemd.requires=` the verify
  # service, the verify service `requires` every member mount and fails if any
  # mountpoint is not a mountpoint, and imladris-storage.target bindsTo both —
  # so a failed member stops the union, Samba and Jellyfin rather than exporting
  # a bare directory onto the microSD. The Requires chain is the real guard; the
  # 0000 was belt-and-braces that turned out to cut the belt.
  #
  # Harder guard available if ever wanted: make each branch a SUBDIRECTORY of
  # its mount (/mnt/pool/dN/archive). That path exists only on the real drive,
  # so a failed mount makes the branch vanish and mergerfs refuses to mount at
  # all. It costs a data move on d1, so it is noted rather than done.
  systemd.tmpfiles.rules = [
    "d /mnt/pool 0755 root root -"
    "d ${archiveRoot} 0775 root media -"
  ];

  assertions = [
    {
      assertion = poolMembers != [ ];
      message = "imladris: the archive pool must have at least one member.";
    }
    {
      # Two members sharing a label would make blkid -L ambiguous and the verify
      # script's identity proof meaningless.
      assertion =
        let
          labels = map (m: m.label) members;
        in
        lib.length (lib.unique labels) == lib.length labels;
      message = "imladris: storage member labels must be unique.";
    }
    {
      # Jellyfin's database must never be inside the union: SQLite over FUSE is a
      # locking hazard, and the union is the thing that gets drives added and
      # removed. ./media.nix depends on this holding.
      assertion = !(lib.any (m: m.pool && m.mountpoint == "/var/lib/imladris") members);
      message = "imladris: /var/lib/imladris must not be a mergerfs branch.";
    }
  ];
}
