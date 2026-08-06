# Osgiliath is a k3s agent. Cluster transport is Tailscale-backed; workload
# listeners remain available on the LAN and tailnet because the pods use host
# networking to preserve their existing ports.
{
  config,
  lib,
  ...
}:
{
  imports = [ ../../../modules/nixos/fleet/k3s-node.nix ];

  fleet.k3sNode = {
    enable = true;
    role = "agent";
    serverAddr = "https://pelargir:6443";
    tokenFile = config.sops.secrets.k3s_agent_token.path;
    vpnAuthFile = config.sops.templates."k3s-vpn-auth".path;
    # Pelargir's enablelb label is the documented ServiceLB allow-list, keeping
    # its 80/443 hostPort DaemonSet off this unlabeled node. The dedicated taint
    # separately keeps unrelated workloads off Osgiliath; only the explicitly
    # tolerating preserved workloads run here.
    extraFlags = [ "--node-taint osgiliath.saldivar.io/workloads=true:NoSchedule" ];
    adminPorts = [
      22
      80
      443
      1883
      5000
      8123
      8554
      8555
      8971
      9001
    ];
  };

  # Ethernet and the recovery Wi-Fi are both trusted home LAN paths. Public
  # reachability is constrained by the router; only the local HTTPS edge is
  # intended to be forwarded from outside.
  networking.firewall.allowedTCPPorts = lib.mkAfter [
    80
    443
    1883
    5000
    8123
    8554
    8555
    8971
    9001
  ];
  networking.firewall.allowedUDPPorts = lib.mkAfter [ 8555 ];
  networking.firewall.interfaces.tailscale0.allowedUDPPorts = [ 8555 ];

  systemd.services.k3s = {
    after = [
      "frigate-storage.target"
      "osgiliath-secrets-gate.service"
    ];
    requires = [
      "frigate-storage.target"
      "osgiliath-secrets-gate.service"
    ];
  };
}
