# pelargir — single-server sqlite k3s control plane on the fleet tailnet.
{ config, ... }:
{
  imports = [ ../../../modules/nixos/fleet/k3s-node.nix ];

  fleet.k3sNode = {
    enable = true;
    role = "server";
    tokenFile = config.sops.secrets.k3s_token.path;
    # Agent joins use a less-privileged credential than server joins.
    agentTokenFile = config.sops.secrets.k3s_agent_token.path;
    vpnAuthFile = config.sops.templates."k3s-vpn-auth".path;
    tlsSans = [ "pelargir.saldivar.io" ];
    adminPorts = [
      22
      1883
      8080
      8123
    ];
  };
}
