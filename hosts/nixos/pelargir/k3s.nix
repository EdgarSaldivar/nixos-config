# pelargir — single-server sqlite k3s control plane on the fleet tailnet.
{ config, pkgs, ... }:
{
  services.tailscale.enable = true;

  services.k3s = {
    enable = true;
    role = "server";
    tokenFile = config.sops.secrets.k3s_token.path;
    # Agent joins use a less-privileged credential than server joins.
    agentTokenFile = config.sops.secrets.k3s_agent_token.path;
    extraFlags = [
      "--vpn-auth-file=${config.sops.templates."k3s-vpn-auth".path}"
      "--tls-san pelargir.saldivar.io"
      "--kubelet-arg=fail-swap-on=false"
    ];
  };

  # Nodes now meet through Tailscale's dedicated CGNAT address space, so the
  # home/site-A private-LAN overlap no longer needs a uniform wg0 overlay. The
  # site-A peer remains only as the out-of-band BMC path.
  #
  # Rev-stamp 2026-08-03: K3s' Distributed hybrid/multicloud page (last updated
  # 2026-07-24) documents name=tailscale,joinKey=... via --vpn-auth-file. The
  # accepted K3s VPN ADR and source rev f9212d5ae6886a41a584e0037d25cb79ffa9c35a
  # additionally verify that vpn-auth starts Tailscale, replaces the server
  # advertise address and node IP with the Tailscale address, and selects
  # Flannel's Tailscale extension backend.
  # That backend advertises pod CIDRs with `tailscale set`; it does not run
  # VXLAN, so UDP 8472 is intentionally not opened. The published examples
  # still show explicit --node-external-ip flags; see the runbook deviation.
  # 22/1883/8080/8123 are NOT k3s ports — they are the admin/service surface
  # the retired wg0 peers used to reach (ssh, MQTT, the Z2M frontend, and the
  # HA offline-fallback UI). Now that the Mac and phone are tailnet clients
  # instead of WireGuard peers, that surface has to follow them or those
  # devices silently lose access the moment their old configs retire.
  # (Regression caught in review 2026-08-04.)
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [
    22
    1883
    6443
    8080
    8123
    10250
  ];

  systemd.services.k3s = {
    # vpn-auth talks to the local daemon; network-online alone does not mean
    # tailscaled is running. Keep the lifeline interface ordered first too.
    after = [
      "network-online.target"
      "time-sync.target"
      "tailscaled.service"
      "wireguard-wg0.service"
    ];
    wants = [
      "network-online.target"
      "time-sync.target"
      "tailscaled.service"
      "wireguard-wg0.service"
    ];
    # K3s invokes the client binary for vpn-auth and Flannel route updates.
    path = [ pkgs.tailscale ];
  };
}
