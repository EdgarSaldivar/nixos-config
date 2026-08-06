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
    # K3s ServiceLB switches to allow-list mode once any node has enablelb=true.
    # Keep the bundled Traefik 80/443 listeners on Pelargir; Osgiliath needs
    # those same host ports for its preserved, node-local HTTPS edge. K3s only
    # applies --node-label during registration, so the migration runbook also
    # labels the already-registered node once during the authorized rollout.
    extraFlags = [ "--node-label svccontroller.k3s.cattle.io/enablelb=true" ];
    adminPorts = [
      22
      1883
      8080
      8123
    ];
  };
}
