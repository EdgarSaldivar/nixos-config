# pelargir — single-server sqlite k3s control plane.
{ config, ... }:
{
  services.k3s = {
    enable = true;
    role = "server";
    tokenFile = config.sops.secrets.k3s_token.path;
    extraFlags = [
      "--node-ip 192.168.4.1"
      "--advertise-address 192.168.4.1"
      "--flannel-iface wg0"
      "--tls-san pelargir.saldivar.io"
      "--tls-san 192.168.4.1"
      "--kubelet-arg=fail-swap-on=false"
    ];
  };

  # Every node joins through wg0, including LAN-local future nodes. Uniformity
  # avoids route ambiguity because home 10.0.0.0/24 overlaps site A's /20.
  systemd.services.k3s = {
    # wireguard-wg0 ordering (review fix 2026-08-03): --node-ip/--flannel-iface
    # point at wg0, so starting before the interface exists just burns restart
    # cycles and log noise. network-online does not imply wg0 is up.
    after = [
      "network-online.target"
      "time-sync.target"
      "wireguard-wg0.service"
    ];
    wants = [
      "network-online.target"
      "time-sync.target"
      "wireguard-wg0.service"
    ];
  };
}
