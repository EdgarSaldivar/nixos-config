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

      # P1B — Kubernetes Secret encryption at rest, enabled 2026-08-06.
      #
      # Added AFTER `k3s secrets-encrypt enable` wrote
      # /var/lib/rancher/k3s/server/cred/encryption-config.json. That order is
      # correct for v1.35.6: `enable` writes the config and saves bootstrap state,
      # then the server must be restarted with this flag to act on it. (The worry
      # that the flag must come first applies only to k3s before v1.30, where
      # `enable` failed without an existing config file.) Until the restart,
      # `secrets-encrypt status` fails with "missing annotation on node pelargir" --
      # that is the staged state, not a fault.
      #
      # ⛔ DO NOT REMOVE THIS LINE, and do not roll back to a generation predating
      # it, without first running `k3s secrets-encrypt disable` + `rotate-keys` to
      # rewrite every Secret back to plaintext. Encrypted rows remain in the
      # datastore, so a server started without this flag CANNOT DECRYPT THEM: pod
      # volume mounts, cert-manager, reflector and every restarting pod fail. The
      # failure is silent (activation succeeds, k3s starts) and delayed (running
      # pods keep their mounted Secrets and only break on restart), so it will not
      # look related to the rollback that caused it. See docs/runbooks/pelargir/rollback.md, which opens
      # with this check because `switch --rollback` is its primary recovery path.
      "--secrets-encryption"
    ];
    adminPorts = [
      22
      1883
      8080
      8123
    ];
  };
}
