# minas-tirith — k3s agent on the fleet tailnet.
{ config, ... }:
{
  imports = [ ../../../modules/nixos/fleet/k3s-node.nix ];

  # On the networking side, the fleet module adds the dynamic tailscale0/cni0
  # interfaces and their firewall policy. Minas' MAC-matched systemd-networkd
  # LAN definitions remain authoritative for the physical NICs, and Docker's
  # 172.16/12 pool does not overlap k3s' default pod/service ranges or
  # Tailscale's CGNAT range.
  fleet.k3sNode = {
    enable = true;
    role = "agent";
    serverAddr = "https://pelargir:6443";
    tokenFile = config.sops.secrets.k3s_agent_token.path;
    vpnAuthFile = config.sops.templates."k3s-vpn-auth".path;
  };
}
