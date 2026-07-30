# minas-tirith (raz-server) — system, identity and networking.
{ config, pkgs, ... }:
{
  # ---------------------------------------------------------------------------
  # Identity
  # ---------------------------------------------------------------------------
  # Renamed from raz-server -> minas-tirith as of the NixOS rebuild. Verified
  # safe: nothing in the five docker-compose stacks references the old name (only
  # /etc/hostname and a historical git author string did), and the
  # host-hostnames helper (dvdarias/docker-hoster) only writes *container* names
  # into /etc/hosts. The public DNS name minas.saldivar.io already matches.
  #
  # The ZFS pool labels still record hostname 'raz-server'; they will record this
  # name after the clean export/import handover described in ./zfs.nix.
  networking.hostName = "minas-tirith";
  networking.domain = "saldivar.io";

  # Required for ZFS. The value itself is arbitrary as long as it never changes
  # — after the clean export/import handover (see ./zfs.nix) the pools record
  # whatever this host presents. This is the value the old install used, kept
  # for continuity.
  networking.hostId = "0149f5f3";

  time.timeZone = "America/Los_Angeles";
  system.stateVersion = "26.05";
  nixpkgs.config.allowUnfree = true;

  # ---------------------------------------------------------------------------
  # Networking — systemd-networkd, matched by MAC not by interface name.
  # ---------------------------------------------------------------------------
  # This board renumbers PCI devices (see pci=realloc=off in
  # hardware-configuration.nix) and a rename has broken networking on Edgar's
  # hardware before. Matching on MAC means the config survives enp38s0 becoming
  # something else.
  #
  # Static rather than DHCP: the old install relied on a DHCP reservation on a
  # LAN whose owner reconfigures things. A server should not depend on that.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.enable = true;

  systemd.network.networks."10-lan" = {
    matchConfig.MACAddress = "a8:a1:59:c0:4e:73"; # was enp38s0
    address = [ "10.0.1.6/20" ];
    routes = [
      { Gateway = "10.0.0.1"; }
      # WireGuard client subnet. Without this route, VPN return traffic is
      # blackholed — this is also why Docker's address pool is pinned away from
      # 192.168.x (see ./docker.nix).
      {
        Destination = "192.168.4.0/24";
        Gateway = "10.0.0.1";
      }
    ];
    networkConfig = {
      DNS = [ "10.0.0.1" ];
      IPv6AcceptRA = true;
    };
    linkConfig.RequiredForOnline = "routable";
  };

  # Second onboard NIC (a8:a1:59:c0:4e:72) is unused and unplugged. Declared so
  # boot does not wait on it.
  systemd.network.networks."20-lan-unused" = {
    matchConfig.MACAddress = "a8:a1:59:c0:4e:72";
    linkConfig.ActivationPolicy = "down";
    linkConfig.RequiredForOnline = "no";
  };

  # ---------------------------------------------------------------------------
  # Access
  # ---------------------------------------------------------------------------
  services.openssh = {
    enable = true;
    settings = {
      # The old box was taking SSH brute-force on an internet-facing forward
      # with password auth enabled. Not repeating that.
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # NOTE: restore the old SSH host keys from the backup after install if you
  # want existing known_hosts entries and automation to keep working:
  #   /storage2/backup-2026-07-30/etc/ssh/ssh_host_*
  # Otherwise every client sees a host key change on first connect.

  security.sudo = {
    enable = true;
    wheelNeedsPassword = false;
  };

  # Console login password is provided via sops — see ./secrets.nix.
  # (mutableUsers = false + key-only sshd would otherwise leave the SOL console
  # showing a login prompt nobody can satisfy.)


  # ---------------------------------------------------------------------------
  # Nix itself
  # ---------------------------------------------------------------------------
  # Without this the host cannot use flake commands on itself — which matters,
  # because `nixos-rebuild --flake` from the machine is the recovery path when
  # deploying from the Mac isn't possible.
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  # A server accumulating generations on a 931 GB root that was already ~660 GB
  # full will eventually wedge on disk space. systemd-boot keeps 10 generations
  # (see ./boot.nix); this bounds the store.
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 30d";
  };
  nix.settings.auto-optimise-store = true;

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
  hardware.rasdaemon.enable = true;

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
  systemd.services.backup-root-data = {
    description = "Back up mutable service data from root to ZFS storage2";
    serviceConfig = {
      Type = "oneshot";
      Nice = 10;
      IOSchedulingClass = "idle";
      # systemd DISABLES the start timeout for Type=oneshot by default. Without
      # this, a wedged rsync or a hung ZFS call sits in `activating` forever,
      # OnFailure never fires, no marker is written — and because the previous
      # run's stamp is still recent, the heartbeat reports healthy for up to 48
      # hours while no backup is actually running. A hang has to become a
      # failure for any of the failure plumbing to mean anything.
      # 6h is generous: the full ~429 GB run measured well under an hour.
      TimeoutStartSec = "6h";
    };
    script = ''
      set -euo pipefail

      # REFUSE to run unless storage2 is genuinely a mounted ZFS filesystem.
      # Without this check, a boot where the pool failed to import would send
      # ~429 GB straight onto the root NVMe (931 GB total, ~660 GB already used)
      # and fill it — turning a recoverable pool problem into a dead root.
      if ! ${pkgs.util-linux}/bin/findmnt -no FSTYPE /storage2 | grep -qx zfs; then
        echo "ABORT: /storage2 is not a mounted ZFS filesystem; refusing to write to root" >&2
        exit 1
      fi

      # The destination must be its own DATASET, because that is what can be
      # snapshotted independently. Created once by hand (see INSTALL-RUNBOOK);
      # disko is forbidden from touching zpools on this host, so nothing in this
      # config can create it. Fail loudly rather than silently writing into the
      # parent dataset, where the snapshots below would cover the wrong thing.
      if ! ${pkgs.zfs}/bin/zfs list -H -o name storage2/backup >/dev/null 2>&1; then
        echo "ABORT: dataset storage2/backup does not exist. Create it once with:" >&2
        echo "  zfs create -o mountpoint=/storage2/backup storage2/backup" >&2
        exit 1
      fi

      # EXISTENCE IS NOT ENOUGH — this guard was decorative without the checks
      # below. `zfs list storage2/backup` succeeds even when that dataset is
      # UNMOUNTED or mounted somewhere else. In that state rsync happily writes
      # to /storage2/backup/... inside the PARENT dataset, while
      # `zfs snapshot storage2/backup@...` snapshots the empty child. Every
      # command returns 0, the stamp is written, the failure marker is cleared —
      # and the snapshots contain nothing. Later mounting the child would then
      # hide the parent-resident files entirely.
      if [ "$(${pkgs.zfs}/bin/zfs get -H -o value mounted storage2/backup)" != "yes" ]; then
        echo "ABORT: storage2/backup exists but is NOT MOUNTED; snapshots would be empty" >&2
        exit 1
      fi
      if [ "$(${pkgs.zfs}/bin/zfs get -H -o value mountpoint storage2/backup)" != "/storage2/backup" ]; then
        echo "ABORT: storage2/backup is mounted somewhere unexpected" >&2
        exit 1
      fi

      dest=/storage2/backup/minas-tirith
      mkdir -p "$dest"

      # Final proof: the destination path really resolves INTO that dataset.
      src=$(${pkgs.util-linux}/bin/findmnt -no SOURCE -T "$dest")
      if [ "$src" != "storage2/backup" ]; then
        echo "ABORT: $dest resolves to dataset '$src', not storage2/backup —" >&2
        echo "       rsync would write outside the dataset being snapshotted" >&2
        exit 1
      fi

      # Only back up sources that EXIST. /opt and /srv hold service data on the
      # openSUSE box but will not exist on a fresh NixOS install until the
      # restore runs — and under `set -e` a missing source makes rsync exit
      # non-zero, so the backup would fail every single night between install and
      # restore. Silently, because a failing timer notifies nobody. The whole
      # point of this unit is to be running before it is needed.
      sources=""
      for s in /etc /home /usr/local /opt /srv /var/lib/docker/volumes; do
        if [ ! -e "$s" ]; then
          echo "skipping $s (does not exist yet)"
        elif [ -d "$s" ] && [ -z "$(ls -A "$s" 2>/dev/null)" ]; then
          # An EMPTY source combined with --delete would erase that directory's
          # entire backup. This is the classic rsync footgun and it is silent:
          # if a bind mount fails, or a pool member is missing, or a restore is
          # half-done, the source reads as empty and the good backup is deleted
          # in its place. Refuse. A stale backup beats no backup.
          echo "SKIPPING $s: it exists but is EMPTY — refusing to --delete its backup" >&2
        else
          sources="$sources $s"
        fi
      done

      if [ -z "$sources" ]; then
        echo "ABORT: no usable backup sources" >&2
        exit 1
      fi

      # ---------------------------------------------------------------------
      # Application-consistent database dumps — BEFORE the file copy.
      # ---------------------------------------------------------------------
      # rsync of a LIVE PostgreSQL data directory is not a usable backup. It is
      # not a matter of degree: PostgreSQL requires the cluster to be stopped or
      # the copy taken through a coordinated snapshot/WAL procedure, otherwise
      # the result may simply refuse to start. ZFS snapshots do not fix this —
      # they faithfully preserve whatever bytes arrived, consistent or not, so
      # 22 snapshots of a torn cluster are 22 unusable copies.
      #
      # SQLite is more forgiving but has the same shape of problem: a live file
      # copy can interleave old and new pages. `.backup` uses SQLite's backup
      # API and is safe against concurrent writers.
      #
      # Dumps are written STRAIGHT TO THE POOL, never staged on root: the root
      # NVMe is already at 33% wear, and this keeps the write off it entirely.
      #
      # Deliberately a SIBLING of $dest, not a subdirectory of it. rsync runs
      # with --delete against $dest, and while a top-level directory absent from
      # the source list is not in fact deleted, that is a subtlety to depend on
      # nightly for the only application-consistent copy we have. Out of reach is
      # better than probably-safe. Still inside dataset storage2/backup, so the
      # snapshots below cover it.
      dumpdir=/storage2/backup/dumps
      mkdir -p "$dumpdir"
      degraded=""

      if ${pkgs.docker}/bin/docker info >/dev/null 2>&1; then
        # Discover Postgres containers by image rather than hardcoding names —
        # immich, nextcloud and pincollector each ship their own, and the set
        # changes as stacks come and go.
        for c in $(${pkgs.docker}/bin/docker ps --format '{{.Names}} {{.Image}}' \
                   | grep -iE 'postgres|pgvector|pgvecto' | cut -d' ' -f1); do
          u=$(${pkgs.docker}/bin/docker exec "$c" printenv POSTGRES_USER 2>/dev/null || echo postgres)
          if ${pkgs.docker}/bin/docker exec "$c" pg_dumpall -U "$u" 2>/dev/null \
             | ${pkgs.gzip}/bin/gzip -c > "$dumpdir/$c.sql.gz.tmp"; then
            # A dump that produced almost nothing is a FAILED dump wearing a
            # success exit code. Require plausible size before promoting it, so
            # a good dump from yesterday is never replaced by an empty one.
            if [ "$(${pkgs.coreutils}/bin/stat -c %s "$dumpdir/$c.sql.gz.tmp")" -gt 1024 ]; then
              mv "$dumpdir/$c.sql.gz.tmp" "$dumpdir/$c.sql.gz"
              echo "dumped $c"
            else
              rm -f "$dumpdir/$c.sql.gz.tmp"
              degraded="$degraded $c(empty)"
            fi
          else
            rm -f "$dumpdir/$c.sql.gz.tmp"
            degraded="$degraded $c(failed)"
          fi
        done
      else
        degraded="$degraded docker-unavailable"
      fi

      # SQLite databases worth a consistent copy. Best-effort and explicitly
      # listed: a blind `find` for *.db would sweep in hundreds of caches and
      # Plex's own DB needs Plex's SQLite build for some operations.
      for db in \
        /usr/local/etc/jellyfin/config/data/data/library.db \
        /usr/local/etc/komga/database.sqlite \
      ; do
        [ -f "$db" ] || continue
        n=$(echo "$db" | ${pkgs.gnused}/bin/sed 's|/|_|g')
        if ${pkgs.sqlite}/bin/sqlite3 "$db" ".backup '$dumpdir/$n'" 2>/dev/null; then
          echo "sqlite backup: $db"
        else
          degraded="$degraded $db(sqlite-failed)"
        fi
      done

      # Dump problems mark the backup DEGRADED rather than failing it: the file
      # copy below is still worth having, and failing here would discard it.
      # The heartbeat surfaces the marker (see ./monitoring.nix).
      if [ -n "$degraded" ]; then
        echo "DEGRADED dumps:$degraded" > /var/lib/backup-root-data.degraded
        echo "WARNING: database dumps degraded:$degraded" >&2
      else
        rm -f /var/lib/backup-root-data.degraded
      fi

      # shellcheck disable=SC2086
      ${pkgs.rsync}/bin/rsync -aHAX --delete --inplace \
        --exclude='*/Cache/***' --exclude='*/transcode/***' \
        $sources "$dest/"

      # ---------------------------------------------------------------------
      # Snapshot AFTER a successful rsync.
      #
      # Without this, the backup is a single MUTABLE MIRROR, not history: rsync
      # --delete faithfully reproduces tonight's damage over last night's good
      # copy, and a torn database or an accidental deletion propagates into the
      # only copy that exists. Worse, the rsync runs while containers are up, so
      # SQLite and PostgreSQL files can be captured mid-transaction and the run
      # still exits 0.
      #
      # Snapshots do not make any single copy transactionally consistent — only
      # stopping the service or dumping properly does that. What they do give is
      # many restore points, so a bad capture stops being fatal: roll back to a
      # night that was fine. That is the cheap 90% of the benefit, and it matches
      # the pattern storage2/pincollector already uses on this pool.
      # ---------------------------------------------------------------------
      ts=$(date -u +%Y%m%d-%H%M%S)
      ${pkgs.zfs}/bin/zfs snapshot "storage2/backup@daily-$ts"
      # Sundays additionally get a weekly, so long-range history survives the
      # 14-day daily window. Separate prefixes keep rotation trivial and safe.
      if [ "$(date -u +%u)" = "7" ]; then
        ${pkgs.zfs}/bin/zfs snapshot "storage2/backup@weekly-$ts"
      fi

      # Rotate: keep 14 daily + 8 weekly. Written without `head -n -N` and
      # without a pipeline into `zfs destroy`, because under `set -o pipefail` a
      # short read closing the pipe would fail the whole unit and mark a
      # successful backup as failed.
      #
      # Retention failure is reported but does NOT fail the unit. The previous
      # version piped into a `while` subshell, so the exit status depended on
      # WHICH destroy failed: a failure on an early snapshot followed by a
      # success exited 0, while a failure on the last one exited 1. That is
      # arbitrary. More importantly, the data copy already succeeded at this
      # point — failing the unit here would clear no data risk, would skip the
      # stamp, and would raise a backup-failed alert for what is really a
      # housekeeping problem. Old snapshots simply accumulate until it is fixed.
      prune() {
        prefix="$1"; keep="$2"; rc=0
        snaps=$(${pkgs.zfs}/bin/zfs list -H -o name -t snapshot -s creation -r storage2/backup 2>/dev/null \
                | grep "@$prefix-" || true)
        [ -z "$snaps" ] && return 0
        total=$(printf '%s\n' "$snaps" | grep -c . || true)
        [ "$total" -gt "$keep" ] || return 0
        old=$(printf '%s\n' "$snaps" | sed -n "1,$((total - keep))p")
        # No pipeline: the loop must run in THIS shell so rc survives it.
        for s in $old; do
          if ${pkgs.zfs}/bin/zfs destroy "$s"; then
            echo "pruned $s"
          else
            echo "WARNING: failed to destroy $s" >&2
            rc=1
          fi
        done
        return $rc
      }
      prune daily 14 || echo "WARNING: daily retention incomplete (backup itself OK)" >&2
      prune weekly 8 || echo "WARNING: weekly retention incomplete (backup itself OK)" >&2

      # Timestamp of last success, checked by the heartbeat (see ./monitoring.nix).
      # Clearing the failure marker is what actually ends the alert — a success
      # ping alone would not, because failure state has to outlive a single ping.
      date -u +%s > /var/lib/backup-root-data.stamp
      rm -f /var/lib/backup-root-data.failed
    '';
  };
  systemd.timers.backup-root-data = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = "daily";
      Persistent = true;
      RandomizedDelaySec = "30m";
    };
  };

  # A failing backup timer is invisible by default — systemd marks the unit
  # failed and nothing else happens.
  #
  # Pinging /fail here is NOT sufficient on its own: the 5-minute heartbeat pings
  # the same check, and a success ping CLEARS a previous failure. So a backup that
  # failed at 03:00 was quietly marked healthy again by 03:05, and a backup that
  # had never succeeded at all reported green indefinitely. The durable marker is
  # what actually holds the alert open — the heartbeat reads it every run and keeps
  # reporting UNHEALTHY until a backup succeeds and removes it.
  systemd.services."notify-failure@" = {
    description = "Report failure of %i to healthchecks";
    serviceConfig = {
      Type = "oneshot";
      # %i is expanded by systemd in unit-file DIRECTIVES only. NixOS's `script`
      # becomes a separate shell file, where a bare %i would stay literal — so
      # the instance name is handed in through Environment=, which does expand.
      Environment = "FAILED_UNIT=%i";
    };
    script = ''
      URL="$(cat ${config.sops.secrets.healthchecks-url.path})"
      if [ "$FAILED_UNIT" = "backup-root-data" ]; then
        printf '%s failed at %s\n' "$FAILED_UNIT" "$(date -u -Is)" \
          > /var/lib/backup-root-data.failed
      fi
      ${pkgs.curl}/bin/curl -fsS -m 20 \
        --data-raw "UNHEALTHY: systemd unit $FAILED_UNIT FAILED" "$URL/fail" || true
    '';
  };
  systemd.services.backup-root-data.onFailure = [ "notify-failure@backup-root-data.service" ];

  environment.systemPackages = with pkgs; [
    # storage / diagnostics
    zfs
    smartmontools
    nvme-cli
    cryptsetup
    lsof
    pciutils
    usbutils
    # BMC — ipmitool could not be installed on the old box because a devel-repo
    # glibc pin blocked it; freeipmi was the workaround. Both, here.
    ipmitool
    freeipmi
    # basics
    git
    wget
    curl
    tmux
    htop
    python3
    sops
    age
  ];
}
