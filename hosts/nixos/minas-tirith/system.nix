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

  system.stateVersion = "26.05";

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

  # ⛔ THE LOCAL LAN MUST NOT BE ROUTED OVER TAILSCALE. Without this, replies to
  # LAN hosts leave via tailscale0 sourced from this node's 100.x address, the
  # peer discards them as coming from the wrong address, and the connection never
  # establishes — it presents as a total silent blackhole.
  #
  # This took down all 26 public hostnames on 2026-08-10. The friend's server at
  # 10.0.1.203 fronts the router's 80/443 forwards and reverse-proxies saldivar.io
  # to this host. Its SYNs arrived on eth0 addressed to our own MAC and were never
  # answered, because `ip route get 10.0.1.203` resolved to `dev tailscale0
  # table 52`. Tailscale installs policy rule 5270 (`from all lookup 52`), which
  # wins over the main table's own `10.0.0.0/20 dev eth0` link route.
  #
  # ⚠️ WHY IT LOOKED IMPOSSIBLE TO DIAGNOSE, recorded because it will mislead again:
  # a reverse-path/asymmetric-routing failure leaves NO evidence in the places you
  # look first. There is no conntrack entry, no iptables counter increments, and the
  # CNI hostPort DNAT rule never matches — so it reads as "the packet never arrived"
  # even though tcpdump shows it arriving with the correct destination MAC.
  #
  # ⚠️ Docker did not expose this: its published port and SNAT behaviour kept the
  # reply on the path the request came in on. A k3s `hostPort` is PREROUTING DNAT,
  # which leaves the reply to ordinary policy routing — so migrating traefik to k3s
  # is what made a long-latent routing conflict fatal.
  #
  # Priority 5000 places this ABOVE Tailscale's 5270. Destinations outside the LAN,
  # including all 100.64.0.0/10 tailnet traffic and the k3s control-plane link to
  # pelargir, are untouched.
  systemd.services.lan-route-priority = {
    description = "Prefer the LAN link over Tailscale for local 10.0.0.0/20 destinations";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # Idempotent: `ip rule add` duplicates silently on re-run, so delete first.
      ExecStart = pkgs.writeShellScript "lan-route-priority" ''
        ${pkgs.iproute2}/bin/ip rule del to 10.0.0.0/20 lookup main priority 5000 2>/dev/null || true
        ${pkgs.iproute2}/bin/ip rule add to 10.0.0.0/20 lookup main priority 5000
      '';
      ExecStop = pkgs.writeShellScript "lan-route-priority-stop" ''
        ${pkgs.iproute2}/bin/ip rule del to 10.0.0.0/20 lookup main priority 5000 2>/dev/null || true
      '';
    };
  };

  systemd.network.networks."10-lan" = {
    matchConfig.MACAddress = "a8:a1:59:c0:4e:73"; # was enp38s0
    address = [ "10.0.1.6/20" ];
    routes = [
      { Gateway = "10.0.0.1"; }
      # This subnet is retired for k3s transport, which now uses Tailscale, but
      # it is NOT stale yet: site A's router still terminates the WireGuard
      # lifeline to pelargir. Keep the return route until that peer is retired.
      # Docker must also remain pinned away from 192.168.x (see ./containers.nix).
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
    # EXPLICIT, not relying on the default. The only external route to this
    # machine is a NAT forward `minas.saldivar.io:2222 -> 10.0.1.6:22`, owned by
    # someone else's router. If this port ever silently changed, remote access
    # would vanish with no way to fix it remotely. It also has to match the
    # initrd sshd port (see ./boot.nix), which shares 22 so that the same single
    # forward reaches both the unlock prompt and the booted system.
    ports = [ 22 ];
    settings = {
      # The old box was taking SSH brute-force on an internet-facing forward
      # with password auth enabled. Not repeating that.
      KbdInteractiveAuthentication = false;
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
  # Intentional host-specific complement to the baseline's scheduled optimise:
  # compact duplicate store paths during writes on this storage-constrained host.
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
      # /var/lib/rancher/k3s/storage is the k3s local-path provisioner's PVC root
      # — the k8s equivalent of /var/lib/docker/volumes. Added 2026-08-06 as part
      # of P0.6: without it, the first service migrated to k3s would have its
      # state silently excluded from every backup, and the existing empty-source
      # guard below means listing it costs nothing while it does not yet exist.
      # /storage/immich-data added 2026-08-09. ⛔ `/storage` is deliberately NOT a
      # backup source — it holds ~98 TB of re-downloadable media — but immich's
      # 44 GB of PHOTOS are not re-downloadable, and they had no backup at all.
      # That is the same gap that got three nextcloud scopes rejected, except 44 GB
      # is small enough to fix properly instead of working around.
      # ⚠️ Its `pgdata` subdirectory is EXCLUDED below: it is a live PostgreSQL
      # cluster, and per the note further down, rsync of a running cluster is not a
      # usable backup. The database is captured by the dump loop instead
      # (immich-postgres14.sql.gz), which was VERIFIED to contain the pgvecto-rs
      # extension and both embedding tables at full row count.
      for s in /etc /home /usr/local /opt /srv /var/lib/docker/volumes /var/lib/rancher/k3s/storage /storage/immich-data; do
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
      svc_left_down=""

      if ${pkgs.docker}/bin/docker info >/dev/null 2>&1; then
        # Discover Postgres containers by image rather than hardcoding names —
        # immich, nextcloud and pincollector each ship their own, and the set
        # changes as stacks come and go.
        # ⛔ `|| true` is REQUIRED, not defensive noise. This script runs under
        # `set -euo pipefail`, and grep exits 1 when it matches NOTHING — which
        # makes the whole pipeline exit 1 and aborts the entire backup. "No
        # Postgres containers are running" is a normal state, not an error, and it
        # becomes the NORMAL state as these databases migrate to k3s.
        for c in $(${pkgs.docker}/bin/docker ps --format '{{.Names}} {{.Image}}' \
                   | grep -iE 'postgres|pgvector|pgvecto' | cut -d' ' -f1 || true); do
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
        # Discovery over RUNNING containers alone is not completeness: a stopped
        # or crashed Postgres yields no dump, no error, and therefore no degraded
        # marker — the backup reports success while that database's only
        # consistent copy silently ages. Check ALL containers and flag any
        # database that exists but was not dumped this run.
        for c in $(${pkgs.docker}/bin/docker ps -a --format '{{.Names}} {{.Image}}' \
                   | grep -iE 'postgres|pgvector|pgvecto' | cut -d' ' -f1 || true); do
          if [ ! -f "$dumpdir/$c.sql.gz" ]; then
            degraded="$degraded $c(never-dumped)"
          else
            age=$(( $(date -u +%s) - $(${pkgs.coreutils}/bin/stat -c %Y "$dumpdir/$c.sql.gz") ))
            [ "$age" -gt 172800 ] && degraded="$degraded $c(dump-stale-$((age/86400))d)"
          fi
        done
      elif ! ${pkgs.k3s}/bin/k3s crictl ps -q >/dev/null 2>&1; then
        # Only degrade when NEITHER runtime is reachable. Before this, the branch
        # read `else degraded=docker-unavailable`, which meant the moment docker
        # was stopped — the DEFINED END STATE of the k3s migration — every backup
        # would report degraded forever while silently dumping no databases at
        # all. A permanently-red signal is one nobody reads, so the migration
        # would have quietly removed database backups and the alert that says so.
        degraded="$degraded no-container-runtime"
      fi

      # ---------------------------------------------------------------------
      #  Postgres running under k3s (agent node: crictl, there is no kubectl)
      # ---------------------------------------------------------------------
      # Runs REGARDLESS of the docker branch above, because during the migration
      # a database may live in either runtime — or one in each.
      if ${pkgs.k3s}/bin/k3s crictl ps -q >/dev/null 2>&1; then
        for id in $(${pkgs.k3s}/bin/k3s crictl ps -o json 2>/dev/null \
                    | ${pkgs.gnugrep}/bin/grep -oE '"id": "[a-f0-9]{12,}"' \
                    | ${pkgs.gnused}/bin/sed 's/.*"\([a-f0-9]*\)"$/\1/' || true); do
          # ⛔ `|| true`, and here it was not hypothetical — this is the line that
          # took the backups down. Under `set -euo pipefail` grep exits 1 when it
          # matches nothing, so EVERY non-Postgres container aborted the entire
          # script before it reached the SQLite dumps or restic.
          #
          # It broke the instant the first k3s workload landed on minas: the run at
          # 2026-08-06 15:21 succeeded, komga became a Pod at ~23:51, and every run
          # from 2026-08-07 00:08 onward died here after ~5 seconds. A day of no
          # backups — and the loop whose whole purpose is noticing an un-dumped
          # database was itself what stopped.
          # ⛔ MATCH THE IMAGE REFERENCE, NOT THE WHOLE INSPECT BLOB. The blob contains
          # every environment variable, so any app holding `POSTGRES_HOST` or a
          # `postgresql://` DSN was detected as a Postgres server, and then failed
          # `pg_dumpall` because it is not one. That raised a PERMANENT degraded marker —
          # `k8s-nextcloud-nextcloud(k8s-failed)` appeared the moment nextcloud migrated —
          # and a health signal that is always red is one you stop reading, which is the
          # same failure as the stale docker count this repo already records.
          # MEASURED 2026-08-09, whole-blob vs image-ref match:
          #   nextcloud (app) POSTGRES_USER      -> no match   (false positive, fixed)
          #   tracearr        postgresql://...   -> no match   (false positive, fixed)
          #   readmeabook     POSTGRES_PASSWORD  -> no match   (false positive, fixed)
          #   nextcloud-db    POSTGRES_DB        -> postgres:14.5  (REAL, still matched)
          # ✅ Losing the three false positives is safe and INTENDED: readmeabook and
          # tracearr are both DECLARED below (see the entry list), which is exactly why
          # they are declared — the comment there already says readmeabook's image "has
          # never matched". nextcloud-db is discovery-only and still matches by image.
          img=$(${pkgs.k3s}/bin/k3s crictl inspect "$id" 2>/dev/null \
                | ${pkgs.gnugrep}/bin/grep -oE '"(image|imageRef)": "[^"]*"' \
                | ${pkgs.gnugrep}/bin/grep -oiE '(postgres|pgvector|pgvecto)[^"]*' | head -1 || true)
          [ -n "$img" ] || continue
          # ⛔ Name the dump by NAMESPACE + CONTAINER, not by the container alone.
          #
          # The first `"name"` in crictl inspect is the CONTAINER name, which for a
          # Postgres Pod is conventionally just `postgres`. Both nextcloud's and
          # immich's databases will land on k3s with that same container name, so
          # naming by it alone makes them BOTH write `postgres.sql.gz` — the second
          # dump silently overwriting the first, leaving one database with no
          # backup and nothing reporting a problem.
          #
          # Verified with a throwaway Pod before either database was migrated: the
          # dump really was written as `postgres.sql.gz`.
          #
          # Namespace disambiguates them, and the container name is STABLE across
          # restarts — unlike `io.kubernetes.pod.name`, which carries a per-Pod
          # random suffix and would produce a new filename on every restart,
          # orphaning the previous dump each time.
          kns=$(${pkgs.k3s}/bin/k3s crictl inspect "$id" 2>/dev/null \
                | ${pkgs.gnugrep}/bin/grep -oE '"io.kubernetes.pod.namespace": "[^"]+"' | head -1 \
                | ${pkgs.gnused}/bin/sed 's/.*"\([^"]*\)"$/\1/' || true)
          kctr=$(${pkgs.k3s}/bin/k3s crictl inspect "$id" 2>/dev/null \
                 | ${pkgs.gnugrep}/bin/grep -oE '"name": "[^"]+"' | head -1 \
                 | ${pkgs.gnused}/bin/sed 's/.*"\([^"]*\)"$/\1/' || true)
          if [ -n "$kns" ] && [ -n "$kctr" ]; then
            nm="k8s-$kns-$kctr"
          elif [ -n "$kctr" ]; then
            nm="k8s-$kctr"
          else
            nm="k8s-$id"
          fi
          u=$(${pkgs.k3s}/bin/k3s crictl exec "$id" printenv POSTGRES_USER 2>/dev/null || echo postgres)
          if ${pkgs.k3s}/bin/k3s crictl exec "$id" pg_dumpall -U "$u" 2>/dev/null \
             | ${pkgs.gzip}/bin/gzip -c > "$dumpdir/$nm.sql.gz.tmp"; then
            if [ "$(${pkgs.coreutils}/bin/stat -c %s "$dumpdir/$nm.sql.gz.tmp")" -gt 1024 ]; then
              mv "$dumpdir/$nm.sql.gz.tmp" "$dumpdir/$nm.sql.gz"
              echo "dumped $nm (k8s)"
            else
              rm -f "$dumpdir/$nm.sql.gz.tmp"; degraded="$degraded $nm(k8s-empty)"
            fi
          else
            rm -f "$dumpdir/$nm.sql.gz.tmp"; degraded="$degraded $nm(k8s-failed)"
          fi
        done
      fi

      # ---------------------------------------------------------------------
      #  Did any k8s database STOP being dumped?
      # ---------------------------------------------------------------------
      # The docker branch answers this by walking `docker ps -a`, which still
      # lists a stopped container — so "exists but was not dumped" is detectable.
      # THAT DOES NOT TRANSLATE. A deleted Pod leaves nothing to enumerate: a
      # Deployment scaled to 0, a failed migration, a namespace deleted, all
      # produce silence, and the loop above simply finds no Postgres and moves on
      # reporting success. One good dump could then age forever.
      #
      # So invert the question. Instead of "for each database, is there a fresh
      # dump?", ask "for each dump we already have, is it still fresh?". The
      # artifact outlives the container, which is exactly the property needed.
      #
      # Scoped to `k8s-*` deliberately. A blanket walk over every dump would flag
      # infra-postgres-1 — PinCollector, stopped ON PURPOSE and parked — turning
      # this into a permanently-red signal, which is the failure mode the docker
      # branch above already has comments about. An alert that is always on is an
      # alert nobody reads.
      #
      # This closes the gap recorded when acceptance criterion 6 was verified:
      # the restore test proved dumps are RESTORABLE, not that a database which
      # quietly stops being dumped gets noticed.
      for f in "$dumpdir"/k8s-*.sql.gz; do
        # The glob is literal when nothing matches; -e filters that out.
        [ -e "$f" ] || continue
        kn=$(${pkgs.coreutils}/bin/basename "$f" .sql.gz)
        kage=$(( $(date -u +%s) - $(${pkgs.coreutils}/bin/stat -c %Y "$f") ))
        [ "$kage" -gt 172800 ] \
          && degraded="$degraded $kn(dump-stale-$((kage/86400))d)"
      done

      # ---------------------------------------------------------------------
      #  DECLARED k8s Postgres dumps — for databases discovery cannot find
      # ---------------------------------------------------------------------
      # The loop above discovers Postgres by IMAGE NAME (`postgres|pgvector|pgvecto`).
      # That is right for a dedicated database Pod and useless for an application that
      # EMBEDS one: readmeabook runs Postgres 16, Redis and the app together under
      # supervisord from `ghcr.io/kikootwo/readmeabook`, which has never matched — so
      # its database had never been dumped even once, on docker or k3s, before
      # 2026-08-08.
      #
      # So declare it. Same rule as the quiesce markers: naming the expectation is what
      # makes ABSENCE loud, and discovery cannot report something it was never able to
      # see. Format is `namespace|container|pguser`.
      #
      # Named `k8s-<ns>-<container>` to match the discovery loop's convention, so the
      # staleness walk below covers it without special-casing.
      #
      # ⚠️ EXPECT A DUPLICATE LOG LINE for readmeabook, and do not "fix" it by deleting
      # this loop. On k3s the discovery loop ALSO matches it — but incidentally: it greps
      # the whole `crictl inspect` output, which contains the mount path
      # `/var/lib/postgresql/data`, not because the image name matches. That is a
      # coincidence of how the volume is mounted and it disappears the moment the mount
      # path changes, whereas the docker-side loop never matched it at all. So both run,
      # writing the same filename, and the second overwrite is identical content. One
      # redundant ~10 MB dump a night is the price of the guarantee.
      # ⛔ THE FOURTH FIELD IS THE DUMP FORMAT, and for tracearr it is not optional.
      #
      # tracearr's database is TIMESCALEDB. A `pg_dumpall` of it restores the base tables,
      # silently drops every hypertable and continuous aggregate, and STILL EXITS 0 —
      # measured: 48 "chunk not found" errors and a success exit code. A backup that looks
      # fine and is missing half the database is worse than none.
      #
      # `fc` therefore takes a per-database `pg_dump -Fc` instead, which IS restorable —
      # but only via the documented dance (extension pinned to the dump's version, then
      # timescaledb_pre_restore / pg_restore / timescaledb_post_restore). That procedure is
      # written up in RESTORE-RUNBOOK.md and was verified end to end, recovering all rows
      # plus 2 hypertables and 5 continuous aggregates.
      #
      # Format is `namespace|container|pguser|mode`, mode empty = pg_dumpall (the default
      # for everything else), `fc` = per-database custom format.
      for entry in "books|readmeabook|postgres|" "media|tracearr|tracearr|fc" ; do
        dns=''${entry%%|*}; rest=''${entry#*|}
        dctr=''${rest%%|*}; rest2=''${rest#*|}
        duser=''${rest2%%|*}; dmode=''${rest2#*|}
        nm="k8s-$dns-$dctr"
        # Find the RUNNING container id for that namespace+container name.
        did=$(${pkgs.k3s}/bin/k3s crictl ps --namespace "$dns" --name "$dctr" -q 2>/dev/null | head -1 || true)
        if [ -z "$did" ]; then
          echo "WARNING: declared k8s postgres $nm has no running container to dump" >&2
          degraded="$degraded $nm(k8s-declared-absent)"
          continue
        fi
        # `-Fc` output is already compressed, so it is NOT piped through gzip and keeps
        # a `.dump` extension — `pg_restore` needs the custom format intact, and a
        # double-compressed file would be silently unreadable by it.
        if [ "$dmode" = "fc" ]; then
          out="$dumpdir/$nm.dump"
          dumpcmd_ok=0
          ${pkgs.k3s}/bin/k3s crictl exec "$did" pg_dump -U "$duser" -Fc -d "$duser" > "$out.tmp" 2>/dev/null && dumpcmd_ok=1
          if [ "$dumpcmd_ok" = "1" ] && [ "$(${pkgs.coreutils}/bin/stat -c %s "$out.tmp")" -gt 1024 ]; then
            mv "$out.tmp" "$out"
            # ⛔ DELETE THE pg_dumpall COUNTERPART. The discovery loop above ALSO matches
            # this container — incidentally, on the `/data/postgres` mount path — and
            # writes `$nm.sql.gz`. For a TimescaleDB database that file is NOT
            # restorable: it drops every hypertable and continuous aggregate while psql
            # exits 0. Leaving it in the dump directory would be worse than having no
            # backup, because it looks exactly like every other service's artifact and a
            # future restore would reach for it first.
            rm -f "$dumpdir/$nm.sql.gz"
            echo "dumped $nm (k8s, declared, pg_dump -Fc for TimescaleDB)"
          else
            rm -f "$out.tmp"; degraded="$degraded $nm(k8s-fc-failed)"
          fi
          continue
        fi
        if ${pkgs.k3s}/bin/k3s crictl exec "$did" pg_dumpall -U "$duser" 2>/dev/null \
           | ${pkgs.gzip}/bin/gzip -c > "$dumpdir/$nm.sql.gz.tmp"; then
          # Same size floor the discovery loop uses. A pg_dumpall of an empty cluster is
          # still several hundred bytes of role statements, so this catches a dump that
          # produced nothing without rejecting a legitimate small database.
          if [ "$(${pkgs.coreutils}/bin/stat -c %s "$dumpdir/$nm.sql.gz.tmp")" -gt 1024 ]; then
            mv "$dumpdir/$nm.sql.gz.tmp" "$dumpdir/$nm.sql.gz"
            echo "dumped $nm (k8s, declared)"
          else
            rm -f "$dumpdir/$nm.sql.gz.tmp"; degraded="$degraded $nm(k8s-empty)"
          fi
        else
          rm -f "$dumpdir/$nm.sql.gz.tmp"; degraded="$degraded $nm(k8s-failed)"
        fi
      done

      # ⛔ …and the case the walk above CANNOT see: NEVER DUMPED EVEN ONCE.
      #
      # That walk inverts the question to "for each dump we have, is it fresh?",
      # which is exactly right for a database that stops being dumped — the artifact
      # outlives the Pod. But a database that was never dumped at all leaves NO
      # artifact, so there is nothing to walk and the loop reports success. A new k8s
      # Postgres that the crictl discovery above silently fails to match would be
      # invisible forever.
      #
      # The docker branch does not have this hole because `docker ps -a` enumerates
      # containers independently of whether they were dumped. There is no equivalent
      # enumeration here: minas is an agent with no API access, and crictl only shows
      # what is currently running.
      #
      # So DECLARE the expectation, exactly as the quiesce markers below do, and for
      # the same reason: inferring the expected set from what exists can never report
      # absence. Naming it makes absence loud.
      #
      # `books-readmeabook` is here because its dump is DECLARED above rather than
      # discovered — so if that declaration ever stops producing a file, this is what
      # says so. nextcloud and immich join it when they migrate.
      # ⚠️ tracearr's artifact is `k8s-media-tracearr.dump`, NOT `.sql.gz` — see the `fc`
      # mode above. The check below knows both shapes.
      for kexp in books-readmeabook media-tracearr; do
        if [ ! -f "$dumpdir/k8s-$kexp.sql.gz" ] && [ ! -f "$dumpdir/k8s-$kexp.dump" ]; then
          echo "WARNING: expected k8s database dump has NEVER appeared: k8s-$kexp.sql.gz" >&2
          degraded="$degraded $kexp(k8s-dump-never-created)"
        fi
      done

      # ---------------------------------------------------------------------
      #  In-cluster QUIESCED backups — consume their status markers
      # ---------------------------------------------------------------------
      # Some databases cannot be dumped while their writer runs. jellyfin is the
      # only one: library.db is rollback-journal (not WAL) and it holds a write
      # lock for its whole runtime. On docker this loop stops the container. On
      # k3s it cannot — minas is an AGENT node with no API access — so the backup
      # is taken by the workload's own initContainer, which runs in the window
      # where the writer is absent by construction. See K3S-QUIESCE-DESIGN.md.
      #
      # That backup happens OUTSIDE this script, so this script has to be told
      # whether it happened. The initContainer writes a durable marker; this
      # block is the consumer.
      #
      # ⛔ The expectation is DECLARED, not inferred from the markers present.
      # Inferring is the trap: a quiesce job that never runs writes no marker,
      # so a "walk whatever markers exist" check sees nothing wrong and reports
      # success forever. That is precisely how komga went un-dumped for its
      # entire life, and how the whole backup sat dead for a day. Naming the
      # expected set means ABSENCE is loud.
      #
      # `jellyfin` migrated to k3s 2026-08-07 and is the only member. Its capture
      # stage writes `staged <iso> bytes=<n>`; the staged database itself is then
      # validated and promoted by the sqlite loop below, which lists it as an
      # ordinary entry.
      # 26 hours, deliberately NOT the 48 used by the dump-staleness walks above.
      #
      # Those walk artifacts written by THIS unit, so 48 h tolerates one skipped run of
      # the unit itself. A quiesce marker is different: it is written by a CronJob that
      # fires nightly, and at 48 h a completely failed quiesce still looks healthy for
      # more than a day — the backup that follows simply promotes yesterday's capture
      # and reports success, so the first alarm would come ~25 h after the failure and
      # ~48 h after the last good capture. Cross-review flagged that as acceptable only
      # if it were an explicit RPO decision, and it was not. At 26 h a SINGLE missed
      # night is visible on the next run.
      quiesceMaxAge=93600
      for qn in jellyfin; do
        # ⛔ The marker lives in the SERVICE'S OWN staging tree, not in $dumpdir.
        # Moved there on cross-review: the capture runs the application's own image
        # as root, so mounting the shared dump directory into it would hand a
        # third-party image write access to every other service's dump — enough to
        # delete them, or to forge a success marker for a backup that never ran. It
        # now reaches nothing but its own tree. It also means the pod needs no
        # hostPath into $dumpdir, so losing that dataset degrades the BACKUP instead
        # of refusing to start the SERVICE.
        qf="/storage2/backup/staging/$qn/.status"
        if [ ! -f "$qf" ]; then
          echo "WARNING: expected in-cluster quiesce marker MISSING: $qf" >&2
          degraded="$degraded $qn(quiesce-marker-missing)"
          continue
        fi
        # Marker format, written atomically by the initContainer:
        #   success <iso8601> tables=<n> bytes=<n>
        #   FAILED  <iso8601> <reason>
        if ${pkgs.gnugrep}/bin/grep -q '^FAILED' "$qf" 2>/dev/null; then
          echo "WARNING: in-cluster quiesced backup FAILED: $(cat "$qf")" >&2
          degraded="$degraded $qn(quiesce-failed)"
          continue
        fi
        # A CAPTURE-stage marker reports `staged <iso> bytes=<n>` instead.
        #
        # An init container that only COPIES a quiesced database has no sqlite3
        # and therefore cannot honestly report a table count — the copy has not
        # been opened, let alone validated. Demanding `tables=` from it would
        # force it to either lie or fail, and a marker that lies is worse than no
        # marker. Validation of a staged copy happens later, in the sqlite loop
        # below, which treats the staged file as an ordinary database entry and
        # applies the full integrity/table/byte gate before promoting anything.
        #
        # So a `staged` marker asserts exactly what the capture stage can know:
        # it ran, and it produced this many bytes.
        if ${pkgs.gnugrep}/bin/grep -q '^staged ' "$qf" 2>/dev/null; then
          sbytes=$(${pkgs.gnugrep}/bin/grep -oE '^staged .*bytes=[0-9]+' "$qf" 2>/dev/null \
                   | ${pkgs.gnugrep}/bin/grep -oE 'bytes=[0-9]+' | cut -d= -f2 || true)
          if [ -z "$sbytes" ]; then
            echo "WARNING: capture marker MALFORMED (no parseable bytes=): $qf" >&2
            degraded="$degraded $qn(capture-marker-malformed)"
            continue
          fi
          if [ "$sbytes" -le 4096 ]; then
            echo "WARNING: capture staged an implausible $sbytes bytes: $qf" >&2
            degraded="$degraded $qn(capture-implausible)"
            continue
          fi
          qage=$(( $(date -u +%s) - $(${pkgs.coreutils}/bin/stat -c %Y "$qf") ))
          [ "$qage" -gt "$quiesceMaxAge" ] \
            && degraded="$degraded $qn(capture-stale-$((qage/3600))h)"
          continue
        fi
        # ⛔ REQUIRE the success shape; do not infer success from "not FAILED".
        #
        # The first version of this check treated any fresh file that did not
        # begin with FAILED as a success. A truncated write, an empty file, or
        # any garbage would therefore pass — and the marker is written by a
        # different system on a different host, which is exactly where partial
        # writes come from. Cross-review caught it; my own four-branch test had
        # not thought to feed it a MALFORMED marker, only a failing one.
        #
        # Parse the contract instead: success, a positive table count, and a
        # byte count above a floor. Same three-part rule the sqlite dumps use, and
        # for the same reason — an empty SQLite file is 4096 bytes of perfectly
        # valid, perfectly empty database that integrity_check calls "ok".
        qtbl=$(${pkgs.gnugrep}/bin/grep -oE '^success .*tables=[0-9]+' "$qf" 2>/dev/null \
               | ${pkgs.gnugrep}/bin/grep -oE 'tables=[0-9]+' | cut -d= -f2 || true)
        qbytes=$(${pkgs.gnugrep}/bin/grep -oE '^success .*bytes=[0-9]+' "$qf" 2>/dev/null \
                 | ${pkgs.gnugrep}/bin/grep -oE 'bytes=[0-9]+' | cut -d= -f2 || true)
        if [ -z "$qtbl" ] || [ -z "$qbytes" ]; then
          echo "WARNING: quiesce marker MALFORMED (not a parseable success): $qf" >&2
          degraded="$degraded $qn(quiesce-marker-malformed)"
          continue
        fi
        if [ "$qtbl" -le 0 ] || [ "$qbytes" -le 4096 ]; then
          echo "WARNING: quiesce marker reports an implausible dump: tables=$qtbl bytes=$qbytes" >&2
          degraded="$degraded $qn(quiesce-implausible)"
          continue
        fi
        # A stale SUCCESS is as bad as a failure — it means the job stopped
        # running and nothing else would notice.
        qage=$(( $(date -u +%s) - $(${pkgs.coreutils}/bin/stat -c %Y "$qf") ))
        [ "$qage" -gt "$quiesceMaxAge" ] \
          && degraded="$degraded $qn(quiesce-stale-$((qage/3600))h)"
      done

      # SQLite databases worth a consistent copy. Best-effort and explicitly
      # listed: a blind `find` for *.db would sweep in hundreds of caches and
      # Plex's own DB needs Plex's SQLite build for some operations.
      #
      # ⛔ `db|container` — the container is STOPPED for the duration of that one
      # dump, then restarted. Owner requirement 2026-08-06: "no degradation at all".
      #
      # A busy timeout alone is NOT sufficient and this was proven, not assumed:
      # with `-cmd ".timeout 60000"` the jellyfin dump still failed after waiting
      # the full 61 seconds, because jellyfin holds library.db write-locked for its
      # ENTIRE runtime rather than in transient bursts — even a plain SELECT returns
      # "database is locked". Waiting cannot succeed against a lock that never
      # releases. With jellyfin stopped the same dump succeeds immediately
      # (458 MB, integrity_check ok).
      #
      # Leave the container field EMPTY to dump without stopping anything.
      # The `media` wave, migrated to k3s 2026-08-07. ~1.3 GB of application state
      # that until now had NO consistent dump — only restic's raw copy of a live
      # database file, which is precisely the inconsistency this loop exists to avoid.
      # They were never in this list; the cutover did not remove them.
      #
      # All nine take an EMPTY container field, and that is measured rather than
      # assumed: each was dumped with the online `.backup` API against its RUNNING
      # Pod, and every one returned integrity_check ok — prowlarr 0.2 s, tautulli
      # 0.5 s, overseerr 0.0 s, and sonarr's 960 MB database in 2.1 s. Unlike
      # jellyfin, these apps take short transactions rather than holding a write
      # lock for their whole runtime, so there is nothing to quiesce and stopping
      # them would be a self-inflicted outage.
      #
      # ⚠️ A service that needs quiescing while running on k3s cannot use the
      # `docker stop` branch below — there is no container to stop, and minas has no
      # API access to scale anything. jellyfin was that case, and it is solved
      # OUTSIDE this loop: its pod's initContainer captures the database in the
      # window where the writer is absent, and this loop only validates and promotes
      # the result. Anything else needing quiescing should follow that pattern rather
      # than growing a pod-level stop here. See K3S-QUIESCE-DESIGN.md.
      #
      # ⛔ `/storage/Media/Library/metadata.db` is the ONE entry here that is not
      # under a path the filesystem backup already copies. The rsync above covers
      # /etc /home /usr/local /opt /srv and the two container-storage trees — NOT
      # /storage. That is deliberate for ~98 TB of re-downloadable media, but
      # metadata.db is not media: it is calibre's entire library CATALOG (every
      # tag, rating, series and custom column, 925 KB). Losing the pool would leave
      # the book files intact and the organisation of them gone, with no copy
      # anywhere. Dumping it onto storage2 puts it on a different pool, which is
      # the only protection it currently has.
      # ⛔ jellyfin is NOT listed as the live database at
      # /usr/local/etc/jellyfin/config/data/data/library.db any more, and it must NOT
      # be re-added as one. That entry was removed in the same commit that migrated
      # jellyfin to k3s, deliberately: after docker stopped, this loop would still
      # have found the same host file, had no container to stop, waited the full 60 s
      # against the k3s writer, failed — and marked EVERY nightly backup degraded
      # from then on, permanently. A backup alert that is always red is a backup
      # alert nobody reads.
      #
      # What is listed instead is the QUIESCED COPY staged by the pod's
      # initContainer, which ran while jellyfin was absent by construction. It is a
      # static file, so the container field is empty — there is nothing to stop — and
      # it gets exactly the same integrity_check + table-count + byte-floor gate as
      # everything else before it is promoted. That is the point of staging rather
      # than letting the capture write a dump directly: the copy is unvalidated until
      # this loop validates it, and a good dump is never replaced by one that has not
      # passed.
      #
      # ⚠️ Note the shape of this list: it is one backslash-continued `for` command,
      # so a comment CANNOT be placed between its entries — the comment would end the
      # continuation and the next line would be parsed as a command to execute.
      for entry in \
        "/storage2/backup/staging/jellyfin/current/library.db|" \
        "/etc/komga/config/database.sqlite|" \
        "/etc/komga/config/tasks.sqlite|" \
        "/usr/local/etc/tautulli/tautulli.db|" \
        "/usr/local/etc/docker-overseer/db/db.sqlite3|" \
        "/usr/local/etc/docker-prowlarr/prowlarr.db|" \
        "/home/edgar/docker-services/sonarr/config/sonarr.db|" \
        "/home/edgar/docker-services/radarr/config/radarr.db|" \
        "/etc/lidarr/data/lidarr.db|" \
        "/home/edgar/docker-services/animearr/config/sonarr.db|" \
        "/usr/local/etc/maintainerr/maintainerr.sqlite|" \
        "/home/edgar/git/docker/books/shelfmark/config/users.db|" \
        "/usr/local/etc/kavita/kavita.db|" \
        "/etc/calibre/config/app.db|" \
        "/storage/Media/Library/metadata.db|" \
        "/home/edgar/docker-services/plex/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db||nointegrity" \
      ; do
        db=''${entry%%|*}
        rest=''${entry#*|}
        stopc=''${rest%%|*}
        # Third field, optional: the validation MODE.
        #
        # Empty (the default, and what every entry but plex uses) means the full
        # three-part gate: integrity_check = ok AND table count > 0 AND a byte floor.
        #
        # `nointegrity` means integrity_check CANNOT RUN on this database, so the gate
        # drops to table count + byte floor. Only plex needs it, and the reason is
        # specific rather than convenient: plex's schema uses a custom collation, so
        # stock sqlite3 fails at PREPARE with
        #     Error: in prepare, unknown tokenizer: collating
        # for both `integrity_check` and `quick_check`. Listing plex without this would
        # therefore mark the backup degraded EVERY night — the permanently-red signal
        # this file has been burned by before — while never promoting a dump.
        #
        # What is lost, stated plainly: structural corruption in the copy would not be
        # detected here. What still holds is not nothing — `.backup` is SQLite's own
        # page-level API and produces a consistent copy by construction, and reading
        # `sqlite_master` requires parsing the whole schema, so a badly damaged file
        # still fails. Plex's own `/usr/lib/plexmediaserver/Plex SQLite` CAN run
        # integrity_check (verified), but it lives inside the container image and this
        # script runs on the host, so it is not reachable from here.
        case "$rest" in
          *"|"*) vmode=''${rest#*|} ;;
          *)     vmode="" ;;
        esac
        # A listed path that does not exist is a DEFECT, not a no-op. This used to be
        # a bare `continue`, and it hid one for as long as the entry existed: the list
        # named `/usr/local/etc/komga/database.sqlite`, komga's database has always
        # lived at `/etc/komga/config/`, and so komga was never dumped even once. The
        # loop reported nothing, because skipping silently is what it was told to do.
        # Anything named here is meant to be backed up; if it is not there, say so.
        # ⛔ A pod-staged capture must be LOCKED while it is dumped.
        #
        # Entries under /storage2/backup/staging are written by a workload's
        # initContainer, which promotes a new capture by RENAMING a directory onto the
        # same pathname this loop reads. `.backup` opens the source read-WRITE — it
        # must, to recover a hot journal — so an unlocked concurrent promotion can
        # leave sqlite3 holding an fd on the old inode while `library.db-journal`
        # resolves through the reused path to the NEW pair. Applying that journal to
        # that database yields a structurally VALID file, so integrity_check, the table
        # count and the byte floor all pass and a silently wrong dump gets promoted.
        #
        # The capture normally runs at 23:30 and this at ~00:17, but any pod restart
        # re-runs it, so the windows are not actually disjoint. Both sides take the
        # same flock; the kernel releases it if either dies.
        lockdir=""
        case "$db" in
          /storage2/backup/staging/*/*/*) lockdir=$(${pkgs.coreutils}/bin/dirname "$(${pkgs.coreutils}/bin/dirname "$db")") ;;
        esac
        if [ ! -f "$db" ]; then
          echo "WARNING: listed for dump but MISSING: $db" >&2
          degraded="$degraded $db(missing)"
          continue
        fi
        # Spaces become underscores as well as slashes. plex is the first entry whose
        # path contains them ("Application Support", "Plex Media Server"), and without
        # this the dump artifact would be a filename with spaces in it — which every
        # unquoted consumer of $dumpdir downstream would then get wrong. No existing
        # entry contains a space, so their names are unchanged.
        n=$(echo "$db" | ${pkgs.gnused}/bin/sed 's|/|_|g; s| |_|g')

        # Acquire the capture lock BEFORE opening the database, and hold it across
        # validation. Waiting is bounded: a capture holds it only for a few renames.
        held_lock=""
        if [ -n "$lockdir" ]; then
          exec 9>"$lockdir/.lock" || true
          if ${pkgs.util-linux}/bin/flock -w 120 9; then
            held_lock=1
          else
            echo "WARNING: could not acquire capture lock $lockdir/.lock in 120s" >&2
            degraded="$degraded $db(capture-lock-timeout)"
            exec 9>&-
            continue
          fi
        fi

        # Stop the holder, with a trap so a crash mid-dump cannot leave the
        # service down. The trap is cleared immediately after the restart below.
        #
        # ⚠️ The trap MUST record a failed restart. An earlier version ended in
        # `|| true`, which meant that if the script died mid-dump AND the trap's
        # restart also failed, the service stayed down with NOTHING written
        # anywhere — silent, indefinite outage. `|| true` there suppressed exactly
        # the case the trap exists for. (Found in review, 2026-08-06.)
        #
        # LIMIT, stated rather than papered over: traps do not run on SIGKILL,
        # OOM-kill or power loss. This covers ordinary failure and Ctrl-C, not
        # those. The backstop for the uncovered cases is the heartbeat noticing a
        # missing container, not this script.
        restart_needed=""
        if [ -n "$stopc" ] && ${pkgs.docker}/bin/docker ps --format '{{.Names}}' 2>/dev/null \
             | ${pkgs.gnugrep}/bin/grep -qx "$stopc"; then
          if ${pkgs.docker}/bin/docker stop -t 30 "$stopc" >/dev/null 2>&1; then
            restart_needed="$stopc"
            trap 'if [ -n "$restart_needed" ] && \
                     ! ${pkgs.docker}/bin/docker start "$restart_needed" >/dev/null 2>&1; then
                    echo "CRITICAL: trap could not restart $restart_needed" >&2
                    echo "trap failed to restart $restart_needed at $(date -u -Is)" \
                      > /var/lib/backup-root-data.failed
                  fi' EXIT INT TERM
          else
            degraded="$degraded $db(could-not-stop-$stopc)"
          fi
        fi
        # Write to .tmp and VALIDATE before replacing the previous copy. `.backup`
        # writing straight to the final name is a data-loss bug with a friendly
        # face: backing up a zero-byte or truncated source produces a perfectly
        # VALID, perfectly EMPTY database that passes `integrity_check` — and it
        # would silently replace the last good consistent copy. Same failure the
        # pg_dump size gate exists to prevent; SQLite needs its own because an
        # empty SQLite file is 4096 bytes of valid database, not 0 bytes.
        # `-cmd ".timeout 60000"` is REQUIRED, not a tuning knob. Without it the
        # first run after the 2026-08-06 restore reported:
        #     WARNING: database dumps degraded: .../jellyfin/.../library.db(sqlite-failed)
        # and reproducing it by hand (without the 2>/dev/null that hides it) gave:
        #     Error: database is locked
        # Jellyfin holds library.db open write-mode (lsof: `287uw`, uid 911) and is
        # in rollback-journal mode, so a live writer blocks `.backup` outright.
        # SQLite's default busy timeout is ZERO — it gives up on the first SQLITE_BUSY
        # instead of waiting. `.backup` is safe against concurrent writers, but only
        # if it is allowed to wait for the lock.
        #
        # This matters more than one missing dump: the heartbeat reports
        # `DEGRADED dumps:` on every run, and Healthchecks alerts on state
        # TRANSITIONS — so a permanently-degraded signal means a genuinely new
        # failure raises no new notification. A backup alert that is always red is
        # a backup alert nobody reads.
        #
        # The timeout only ever costs time on a locked DB; the success path is
        # unchanged. Failing after 60s still lands in the sqlite-failed branch below.
        # ⛔ DUMP AS THE DATABASE'S OWNER, NEVER AS ROOT.
        #
        # `sqlite3 <db>` opens the source read-WRITE, and on a WAL database that
        # CREATES `-wal`/`-shm` when they are absent — which is exactly the state
        # right after the owning app stops or checkpoints. Created by root they are
        # root-owned, and the Pod, running as uid 1000, then cannot open its own
        # database: "attempt to write a readonly database". A nightly backup would
        # be bricking the service it exists to protect, intermittently, depending on
        # whether it happened to run during a restart.
        #
        # Same trap the migration runbook records for integrity_check, arrived at
        # from the other direction. Dropping privileges for the dump means the
        # staging directory must be writable by that uid; validation and the final
        # move stay with root.
        owner=$(${pkgs.coreutils}/bin/stat -c %u "$db")
        group=$(${pkgs.coreutils}/bin/stat -c %g "$db")
        stage="$dumpdir/.stage"
        rm -rf "$stage"
        ${pkgs.coreutils}/bin/install -d -m 0700 -o "$owner" -g "$group" "$stage"
        if ${pkgs.util-linux}/bin/setpriv --reuid="$owner" --regid="$group" --clear-groups \
             ${pkgs.sqlite}/bin/sqlite3 -cmd ".timeout 60000" "$db" \
             ".backup '$stage/$n.tmp'" 2>/dev/null; then
          # `nointegrity` entries (plex only) cannot be integrity_checked by stock
          # sqlite3 at all — see the vmode comment above — so the check is SKIPPED
          # rather than run and failed. Everything else still runs it.
          if [ "$vmode" = "nointegrity" ]; then
            ok=ok
          else
            ok=$(${pkgs.sqlite}/bin/sqlite3 "$stage/$n.tmp" "PRAGMA integrity_check;" 2>/dev/null || echo bad)
          fi
          tbls=$(${pkgs.sqlite}/bin/sqlite3 "$stage/$n.tmp" \
                 "SELECT COUNT(*) FROM sqlite_master WHERE type='table';" 2>/dev/null || echo 0)
          # THREE gates, not two. `integrity_check` returning ok is necessary and
          # nowhere near sufficient: an empty SQLite file is 4096 bytes of
          # perfectly valid, perfectly empty database and passes it. The table
          # count catches that, and the byte floor catches a truncated-but-
          # structurally-sound copy that still has its schema.
          #
          # The floor was MISSING until 2026-08-07 even though K3S-QUIESCE-DESIGN.md
          # asserted it was here — a documentation claim about code, contradicted
          # by the code, found by cross-review. Every real dump in this list is
          # >= 80 KB (the smallest is shelfmark's users.db at 81920), so 4096 is a
          # floor that cannot reject a legitimate dump while still catching the
          # empty-database case the probe reproduced (`ok=ok tables=0 bytes=4096`).
          dsz=$(${pkgs.coreutils}/bin/stat -c %s "$stage/$n.tmp" 2>/dev/null || echo 0)
          if [ "$ok" = "ok" ] && [ "''${tbls:-0}" -gt 0 ] && [ "''${dsz:-0}" -gt 4096 ]; then
            mv "$stage/$n.tmp" "$dumpdir/$n"
            # `mv` preserves the staging file's ownership, which is the DATABASE's
            # uid — so without this the backup artifacts become writable by a
            # non-root user. The source data is already theirs, so this leaks
            # nothing, but a backup an unprivileged process can rewrite is a
            # weaker backup. Restore the previous root:root property.
            ${pkgs.coreutils}/bin/chown root:root "$dumpdir/$n"
            echo "sqlite backup: $db ($tbls tables, as uid $owner)"
          else
            degraded="$degraded $db(sqlite-empty-or-corrupt)"
          fi
        else
          degraded="$degraded $db(sqlite-failed)"
        fi
        # Removes the staging copy on every path, including the stray `-wal`/`-shm`
        # that validating the temp file can leave beside it.
        rm -rf "$stage"

        # Restart the holder IMMEDIATELY — downtime is bounded to this one dump,
        # not to the whole sqlite loop. Then clear the trap so a later, unrelated
        # failure does not try to start a container it never stopped.
        if [ -n "$restart_needed" ]; then
          if ${pkgs.docker}/bin/docker start "$restart_needed" >/dev/null 2>&1; then
            echo "restarted $restart_needed after consistent dump"
          else
            # Louder than `degraded`: a service left DOWN is worse than a missing
            # dump, and must not be filed under the same soft marker.
            echo "CRITICAL: failed to restart $restart_needed after dump" >&2
            echo "failed to restart $restart_needed at $(date -u -Is)" \
              > /var/lib/backup-root-data.failed
            # Fail the UNIT too — but at the very end, not here. Exiting now would
            # skip the file backup entirely, leaving both no backup AND a stopped
            # service. Recorded and re-raised after the rsync completes, so the
            # backup is still taken and systemd still reports failure.
            # (Without this the unit reported success while jellyfin was down.)
            svc_left_down="$svc_left_down $restart_needed"
          fi
          restart_needed=""
        fi
        # Release the capture lock, if this entry took one, so a quiesce that fires
        # mid-backup waits rather than being refused.
        if [ -n "$held_lock" ]; then
          exec 9>&-
          held_lock=""
        fi
        trap - EXIT INT TERM
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
      # ⛔ immich-data/pgdata is a LIVE PostgreSQL cluster. Copying it here would
      # produce a torn, unrestorable directory that LOOKS like a backup — the exact
      # failure this file warns about above. The database is dumped separately.
      # The pattern is deliberately anchored on immich-data rather than a bare
      # */pgdata so it cannot silently start excluding some other service's data.
      ${pkgs.rsync}/bin/rsync -aHAX --delete --inplace \
        --exclude='*/Cache/***' --exclude='*/transcode/***' \
        --exclude='immich-data/pgdata/***' \
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
      # Retention failure must leave a trace the heartbeat can see. Printing to
      # the journal only means snapshots quietly accumulate until the pool fills —
      # local logs nobody reads are exactly the blind spot this file exists to
      # remove. The marker clears only when BOTH passes succeed.
      retfail=""
      prune daily 14 || retfail="daily"
      prune weekly 8 || retfail="$retfail weekly"
      if [ -n "$retfail" ]; then
        echo "snapshot retention failing:$retfail at $(date -u -Is)" \
          > /var/lib/backup-root-data.retention-failed
      else
        rm -f /var/lib/backup-root-data.retention-failed
      fi

      # Timestamp of last success, checked by the heartbeat (see ./monitoring.nix).
      # Clearing the failure marker is what actually ends the alert — a success
      # ping alone would not, because failure state has to outlive a single ping.
      date -u +%s > /var/lib/backup-root-data.stamp

      # A service we stopped for a dump and could not restart is a REAL failure,
      # not a degraded backup. Raised here rather than at the point of failure so
      # the file backup above still completes -- exiting early would have left no
      # backup AND a stopped service. Deliberately BEFORE clearing .failed, so the
      # marker survives for the heartbeat to report.
      if [ -n "$svc_left_down" ]; then
        echo "CRITICAL: backup finished but these stayed DOWN:$svc_left_down" >&2
        exit 1
      fi

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
      # head -1, NOT cat: the secret may carry a second line (the dedicated
      # critical check). `cat` would hand curl a URL containing a newline, which
      # it rejects outright — silently losing the immediate failure notification.
      URL="$(head -1 ${config.sops.secrets.healthchecks-url.path})"
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
    usbutils
    # BMC — ipmitool could not be installed on the old box because a devel-repo
    # glibc pin blocked it; freeipmi was the workaround. Both, here.
    ipmitool
    freeipmi
    # basics not already supplied by modules/nixos/common.nix
    tmux
    htop
    python3
    sops
    age
  ];
}
