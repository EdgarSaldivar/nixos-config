# minas-tirith — outward monitoring.
#
# The old host had none. Consequences, all observed in July 2026:
#   - a multi-hour outage was discovered by hand, by trying to use the machine
#   - filesystem corruption sat unnoticed between monthly scrubs
#   - a fatal machine check went unrecorded entirely
#
# This is a health-GATED heartbeat, not a liveness ping. A bare "I'm alive"
# curl would have stayed green through the read-only filesystem failure on
# 2026-07-29 — the box was up and serving reads the whole time. This one checks
# the things that actually went wrong and reports failure explicitly.
{ config, pkgs, ... }:
{
  # The ping URL is a secret in THIS repo specifically: nixos-config is a PUBLIC
  # GitHub repository. Anyone with the URL can ping it and thereby suppress a
  # genuine outage alert — the monitoring would look healthy while the machine
  # was down. Kept in sops, read at runtime, never in the store or in git.
  sops.secrets.healthchecks-url = { };

  systemd.services.healthcheck-ping = {
    description = "Health-gated heartbeat to healthchecks.io";
    path = with pkgs; [
      curl
      util-linux
      zfs
      docker
      gnugrep
      coreutils
    ];
    serviceConfig = {
      Type = "oneshot";
      # Never let the heartbeat itself be the thing that wedges the box.
      TimeoutStartSec = "60s";
    };
    script = ''
      URL="$(cat ${config.sops.secrets.healthchecks-url.path})"
      problems=""

      # 1. Is the root filesystem still writable? This is exactly what failed
      #    on 2026-07-29 while everything else looked healthy.
      if ! findmnt -no OPTIONS / | grep -qw rw; then
        problems="''${problems}root filesystem is READ-ONLY; "
      fi

      # 2. Both ZFS pools healthy? (98 TB lives here)
      pools=$(zpool status -x 2>/dev/null || echo "zpool status failed")
      if ! echo "$pools" | grep -q "all pools are healthy"; then
        problems="''${problems}zpool: $(echo "$pools" | head -1); "
      fi

      # 3. Machine checks. Two uncorrectable MCEs (Bank 5 EX unit, Bank 2 L2)
      #    were recorded in July 2026 before the memory was brought back to
      #    AMD's 2667 spec for 4x dual-rank. If they return, this must shout.
      if [ -x ${pkgs.rasdaemon}/bin/ras-mc-ctl ]; then
        mce=$(${pkgs.rasdaemon}/bin/ras-mc-ctl --errors 2>/dev/null \
              | grep -ciE "^[0-9]+ .*(mce|memory)" || true)
        [ "''${mce:-0}" -gt 0 ] && problems="''${problems}rasdaemon reports $mce hardware errors; "
      fi

      # 4. Did we lose a chunk of the container stack?
      running=$(docker ps -q 2>/dev/null | wc -l)
      if [ "$running" -lt 30 ]; then
        problems="''${problems}only $running containers running; "
      fi

      if [ -n "$problems" ]; then
        curl -fsS -m 20 --data-raw "UNHEALTHY: $problems" "$URL/fail" || true
      else
        curl -fsS -m 20 "$URL" || true
      fi
    '';
  };

  systemd.timers.healthcheck-ping = {
    description = "Send heartbeat every 5 minutes";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "5min";
      AccuracySec = "30s";
    };
  };
}
