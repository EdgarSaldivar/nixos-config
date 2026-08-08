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

    # ---------------------------------------------------------------------
    #  Unsafe sysctls — a PREREQUISITE for three remaining migrations
    # ---------------------------------------------------------------------
    # Four docker containers set sysctls, and Kubernetes splits them into a
    # blessed "safe" list and everything else. Only one of ours is on that list:
    #
    #   nextcloud     net.ipv4.ip_unprivileged_port_start   SAFE, works already
    #   flaresolverr  net.ipv6.conf.all.disable_ipv6        unsafe
    #   deluge-vpn    net.ipv4.conf.all.src_valid_mark      unsafe
    #   deluge-books  net.ipv4.conf.all.src_valid_mark      unsafe
    #
    # `src_valid_mark` is not optional decoration: it is a WIREGUARD
    # requirement. Without it the VPN containers do not route, so the entire
    # tier C VPN pair is blocked on this. `disable_ipv6` is what stops
    # flaresolverr's headless Chrome stalling on IPv6 that does not route —
    # the pod network is IPv4-only (10.42.1.0/24) yet pods still get IPv6
    # link-local addresses, so the sysctl is NOT redundant here.
    #
    # "Unsafe" is a Kubernetes policy label, not a claim about danger. Both of
    # these are per-network-namespace, so a Pod setting them cannot affect the
    # node or another Pod. They are unsafe only in that upstream has not
    # blessed them.
    #
    # ⚠️ What this genuinely widens: ANY Pod scheduled on minas may now request
    # these two, and the namespaces are pod-security audit/warn with no
    # `enforce`. That is a real if small increase in what a workload can ask
    # for. Scoped to exactly two sysctls rather than a wildcard for that reason.
    #
    # A Pod must still request them explicitly in `securityContext.sysctls`;
    # allowing them here only makes the request admissible.
    #
    # ⛔ DEPLOY IMPACT: changing kubelet args restarts the k3s agent, which
    # restarts every Pod on minas. Apply it deliberately, not incidentally
    # alongside an unrelated change.
    extraFlags = [
      "--kubelet-arg=allowed-unsafe-sysctls=net.ipv6.conf.all.disable_ipv6,net.ipv4.conf.all.src_valid_mark"
    ];
  };
}
