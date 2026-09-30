# Self-heal for wedged gluetun VPN sidecars (deluge-books, deluge-vpn, books-netns).
#
# On 2026-09-26 a ~13-minute PIA outage left both TCP gluetun sidecars stuck
# half-way through their own VPN restart for 3.5 days: tunnel up, but no public IP,
# no forwarded port, and no further self-recovery. deluge-books was down the whole
# time. Nothing inside those Pods can reach the API by design, so the repair lives
# here, on the control plane. The program documents the mechanism and why a recycle
# is scale-to-zero-and-back rather than a Pod delete: scripts/gluetun-watchdog.py.
{
  config,
  pkgs,
  ...
}:
{
  systemd.services.gluetun-watchdog = {
    description = "Recycle gluetun-gated Deployments whose VPN has wedged";

    # `after` only orders; the program treats an unreachable API as a failed run
    # rather than a healthy one, and the timer's OnBootSec keeps the first run clear
    # of k3s startup.
    after = [ "k3s.service" ];
    wants = [ "k3s.service" ];

    # ⛔ The ONLY PATH this program gets; nothing is inherited.
    path = [
      pkgs.python3
      config.services.k3s.package
    ];

    serviceConfig = {
      Type = "oneshot";
      # A recycle waits up to the Pod's grace period (180 s for deluge-vpn) plus a
      # 120 s margin, on top of one exec per gluetun Pod against a Pi control plane.
      TimeoutStartSec = "15m";
      StateDirectory = "gluetun-watchdog";
      ProtectSystem = "strict";
      # kubectl caches discovery under $HOME/.kube; ProtectHome hides /root. Same
      # arrangement as k3s-reconcile.nix.
      ProtectHome = true;
      PrivateTmp = true;
      NoNewPrivileges = true;
      ExecStart = "${pkgs.python3}/bin/python3 ${./scripts/gluetun-watchdog.py}";
    };

    environment = {
      STATE_DIR = "/var/lib/gluetun-watchdog";
      HOME = "/var/lib/gluetun-watchdog";
    };
  };

  systemd.timers.gluetun-watchdog = {
    description = "Check gluetun VPN sidecars every 5 minutes";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "10min";
      # With the 15-minute threshold this acts after 15-20 minutes of continuous
      # failure: three or four consecutive bad observations, never one.
      OnUnitActiveSec = "5min";
    };
  };
}
