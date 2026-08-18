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
  # Scrutiny receives a snapshot of this secret through LoadCredential, so a
  # secret rotation must restart it to refresh that in-memory credential.
  sops.secrets.healthchecks-url.restartUnits = [ "scrutiny.service" ];

  systemd.services.healthcheck-ping = {
    description = "Health-gated heartbeat to healthchecks.io";
    path = with pkgs; [
      curl
      util-linux
      zfs
      # k3s for `crictl`. This host is a k3s AGENT, so there is no kubectl and no
      # API access — crictl talking to the node's own containerd is the only way
      # to see workloads that have migrated off docker. See the runtime-agnostic
      # checks below.
      k3s
      smartmontools
      gnugrep
      # gawk: the k8s pod-readiness check below parses `crictl pods`. Omitting it
      # produced `awk: command not found` at runtime while the unit still exited
      # 0 and pinged OK — the check silently did nothing. systemd.services.path
      # is the ONLY PATH this script gets; nothing is inherited.
      gawk
      coreutils
      # ⛔ ADDED 2026-08-16, WITH THE EXTRACTION -- not optional garnish.
      #
      # These three were interpolated as absolute store paths inside the program, so
      # they never had to be on PATH. The program now uses bare names, so they do.
      #
      # Dropping any one of them reproduces EXACTLY the gawk failure documented
      # above: `ras-mc-ctl: command not found` on stderr, unit still exits 0,
      # heartbeat still pings OK, and that check silently stops checking. This host
      # has already been bitten by that once, which is why they are called out here
      # instead of quietly appended to the list.
      rasdaemon
      sqlite
      systemd
      # jq: the crash-loop check parses `crictl ps -a -o json`. It must NOT go back
      # to reading the `crictl ps` table -- that column-position parsing is what made
      # this program alert on a pod named "Ready" until 2026-08-16.
      jq
    ];
    serviceConfig = {
      Type = "oneshot";
      # Never let the heartbeat itself be the thing that wedges the box.
      TimeoutStartSec = "60s";
      # Persistent state for delta comparisons -> /var/lib/healthcheck-ping
      StateDirectory = "healthcheck-ping";
    };
    # The program itself lives in scripts/healthcheck-ping.sh -- a real file that
    # ShellCheck and `bash -n` can read, gated by checks/minas-shell-lint.nix.
    #
    # `readFile` + `replaceStrings` rather than a derivation: it keeps this a plain
    # string, so the characterization fixtures and the unit-contract check still
    # evaluate on aarch64-darwin without building an x86_64-linux package. The
    # placeholder is the ONE thing in the program that is not plain shell -- the
    # path to the sops-rendered secret, which is a store-independent runtime path.
    script =
      builtins.replaceStrings [ "@healthchecksUrlFile@" ] [ config.sops.secrets.healthchecks-url.path ]
        (builtins.readFile ./scripts/healthcheck-ping.sh);
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
