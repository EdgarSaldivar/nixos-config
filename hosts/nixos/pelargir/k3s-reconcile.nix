# Periodic reconciliation of the k3s auto-deploy directory against the cluster.
#
# ROADMAP item 5. k3s auto-deploy has no lifecycle: removing an object from a
# manifest, or removing a manifest file entirely, does NOT delete the applied
# resource. `manifests.nix` deliberately only reports a stale file rather than
# deleting it, because deleting during activation could drop a live route. That
# trade is sound. The gap it leaves is that nothing tracks what has accumulated.
#
# On 2026-08-16 a traefik Middleware named `cloudflare-only` was found live,
# referenced by nothing, eleven days after it stopped being wanted -- and it was an
# ACL that would have 403'd Home Assistant had anyone pointed a route at it. Nothing
# reported it because nothing was looking. This is the thing that looks.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  # PyYAML is the whole reason this is a python program rather than shell: parsing
  # multi-document Kubernetes YAML with awk is how you get a reconciliation report
  # that is confidently wrong.
  python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
in
{
  systemd.services.k3s-reconcile = {
    description = "Report k3s objects that no manifest declares any more";

    # ⛔ The ONLY PATH this program gets; nothing is inherited. A missing binary
    # here fails at runtime while the unit can still look like it did something,
    # which this repository has been bitten by before -- see the `gawk` note in
    # minas-tirith/monitoring.nix.
    path = [
      python
      config.services.k3s.package
      pkgs.coreutils
    ];

    serviceConfig = {
      Type = "oneshot";
      # Bounded. A wedged API call must not leave this unit `activating` forever.
      TimeoutStartSec = "10m";
      StateDirectory = "k3s-reconcile";
      # It only ever reads cluster state and writes its own report.
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = true;
      ExecStart = "${python}/bin/python3 ${./scripts/k3s-reconcile.py}";
    };

    environment.REPORT_DIR = "/var/lib/k3s-reconcile";
  };

  systemd.timers.k3s-reconcile = {
    description = "Weekly k3s manifest reconciliation";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      # Weekly, not daily: orphans are a slow accumulation, and a report that
      # arrives more often than anyone reads it is a report nobody reads.
      OnCalendar = "Mon 04:00";
      # The host is not always up at 04:00 on a Monday, and a report that silently
      # skips the weeks you were rebooting is worse than useless.
      Persistent = true;
      RandomizedDelaySec = "15m";
    };
  };
}
