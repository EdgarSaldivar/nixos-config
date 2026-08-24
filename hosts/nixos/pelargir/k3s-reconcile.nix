# Periodic reconciliation of the k3s auto-deploy directory against the cluster.
#
# k3s auto-deploy has no lifecycle: removing an object from a
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

    # ⛔ Order after k3s, or the weekly report becomes a weekly false alarm.
    #
    # The timer is Persistent, so after any Monday-morning boot it fires as soon as
    # timers.target is reached. Without this the API server is not up yet, every
    # kubectl call fails, main() returns 2 with "cannot list addons", and the unit
    # goes red for a reason that has nothing to do with the cluster's contents.
    #
    # `after` only orders; it does not wait for the API to be SERVING, which is why
    # the program also polls for readiness before it does anything.
    after = [ "k3s.service" ];
    wants = [ "k3s.service" ];

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
      #
      # 20m, not 10m: the program may spend up to 5 minutes waiting for the API after
      # a Monday-morning boot before it does any work at all, and the work itself is
      # ~60 addons x (per-kind list + a server dry-run) against a Pi control plane.
      TimeoutStartSec = "20m";
      StateDirectory = "k3s-reconcile";
      # It only ever reads cluster state and writes its own report.
      ProtectSystem = "strict";

      # ⚠️ ProtectHome hides /root, and kubectl caches discovery under $HOME/.kube.
      # It degrades to warnings rather than failing, but every run then re-fetches
      # discovery for ~60 addons plus a dry-run each, against a Raspberry Pi control
      # plane, inside a 10-minute timeout. Point HOME at the StateDirectory so the
      # cache is both writable and confined.
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = true;
      ExecStart = "${python}/bin/python3 ${./scripts/k3s-reconcile.py}";
    };

    environment = {
      REPORT_DIR = "/var/lib/k3s-reconcile";
      # kubectl caches discovery under $HOME/.kube; ProtectHome hides /root, so point
      # HOME at the StateDirectory rather than let every run re-fetch discovery.
      HOME = "/var/lib/k3s-reconcile";
    };
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
