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
    extraFlags = [
      "--node-label svccontroller.k3s.cattle.io/enablelb=true"

      # P0.7 scheduling isolation, added 2026-08-06 for the minas k3s migration.
      #
      # k3s does NOT taint its server by default (unlike kubeadm), so pelargir was
      # freely schedulable. With no resource requests anywhere, every pod is
      # BestEffort and the scheduler cannot tell an 8 GB ARM Pi from a 125 GB /
      # 32-thread x86 host — so a migrated media workload with no nodeSelector
      # would happily land here and contend with Home Assistant.
      #
      # ⚠️  Deliberately the STANDARD control-plane taint, not a custom one. The
      # k3s-generated ServiceLB DaemonSet tolerates exactly
      # `node-role.kubernetes.io/control-plane` (Exists/NoSchedule) and
      # `CriticalAddonsOnly` — verified on the live cluster. A custom taint such
      # as pelargir.saldivar.io/workloads would have been silently rejected by
      # svclb and taken this node's :80/:443 ingress with it.
      #
      # Ordering: tolerations were added to home-assistant, zigbee2mqtt, mosquitto
      # and ddns-updater and applied BEFORE this line existed. NoSchedule does not
      # evict running pods, but without those tolerations they would not have come
      # back after any restart.
      "--node-taint node-role.kubernetes.io/control-plane:NoSchedule"
    ];
    adminPorts = [
      22
      1883
      8080
      8123
    ];
  };
}
