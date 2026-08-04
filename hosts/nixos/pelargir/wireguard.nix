# pelargir — site-A BMC lifeline and the Cloudflare ingress forward gate.
{ config, pkgs, ... }:
let
  # Cloudflare publishes these ranges at https://www.cloudflare.com/ips/.
  # Refreshed 2026-08-03; refresh both this set and ingress.yaml together.
  cloudflareV4 = [
    "173.245.48.0/20"
    "103.21.244.0/22"
    "103.22.200.0/22"
    "103.31.4.0/22"
    "141.101.64.0/18"
    "108.162.192.0/18"
    "190.93.240.0/20"
    "188.114.96.0/20"
    "197.234.240.0/22"
    "198.41.128.0/17"
    "162.158.0.0/15"
    "104.16.0.0/13"
    "104.24.0.0/14"
    "172.64.0.0/13"
    "131.0.72.0/22"
  ];
  cloudflareV6 = [
    "2400:cb00::/32"
    "2606:4700::/32"
    "2803:f800::/32"
    "2405:b500::/32"
    "2405:8100::/32"
    "2a06:98c0::/29"
    "2c0f:f248::/32"
  ];
in
{
  boot.kernel.sysctl."net.ipv4.ip_forward" = 1;

  # extraForwardRules is an nftables-only NixOS option and is emitted only
  # when forward filtering is active. Traefik's source-range middleware is
  # still the primary DNAT ACL; this host rule is the requested second belt.
  networking.nftables.enable = true;

  networking.wireguard.interfaces.wg0 = {
    ips = [ "192.168.4.1/24" ];
    listenPort = 51820;
    privateKeyFile = config.sops.secrets.wireguard_server_private_key.path;
    peers = [
      {
        # site_a_router — REPLACE-AT-RELIGHT with the router's real public key.
        publicKey = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
        presharedKeyFile = config.sops.secrets.wireguard_psk_site_a.path;
        # Deliberately /24, never the site's encompassing /20: home uses the
        # overlapping 10.0.0.0/24 and must keep its connected route.
        allowedIPs = [
          "192.168.4.2/32"
          "10.0.1.0/24"
        ];
        persistentKeepalive = 25;
      }
    ];
  };

  networking.firewall = {
    enable = true;
    filterForward = true;
    # WG endpoint on the WAN plus Traefik's ServiceLB listeners.
    allowedUDPPorts = [ 51820 ];
    allowedTCPPorts = [
      80
      443
    ];
    interfaces.eth0.allowedTCPPorts = [
      22
      1883
      8080
      8123
    ];

    # Belt-and-braces for DNAT/ServiceLB traffic. INPUT-only rules cannot
    # protect forwarded ports; Traefik's middleware remains the primary ACL.
    #
    # Rule order is load-bearing (review fix 2026-08-03). filterForward makes
    # the forward chain default-drop. Besides pod forwarding, only the scoped
    # tailscale0<->wg0 site-A subnet path is admitted: tailnet clients reach
    # the BMC through this lifeline, and pelargir advertises that exact route.
    #
    # The 80/443 Cloudflare gate is scoped to eth0 ingress and evaluated
    # BEFORE the interface-wide accepts, so a WAN scan cannot slip through via
    # the pod-bound accept.
    extraForwardRules = ''
      iifname "eth0" ip saddr { ${builtins.concatStringsSep ", " cloudflareV4} } tcp dport { 80, 443 } accept
      iifname "eth0" ip saddr 10.0.0.0/24 tcp dport { 80, 443 } accept
      iifname "eth0" ip6 saddr { ${builtins.concatStringsSep ", " cloudflareV6} } tcp dport { 80, 443 } accept
      iifname "eth0" tcp dport { 80, 443 } drop
      iifname "tailscale0" oifname "wg0" ip daddr 10.0.1.0/24 accept
      iifname "wg0" oifname "tailscale0" ip saddr 10.0.1.0/24 accept
      iifname "tailscale0" oifname "cni0" ip daddr 10.42.0.0/16 accept
      iifname { "cni0", "flannel.1" } accept
    '';
  };

  # Rev-stamp 2026-08-03: Tailscale's subnet-router guide requires forwarding,
  # `tailscale set --advertise-routes=...`, and separate route approval. Its
  # CLI reference says `set` changes only named preferences, unlike `up`.
  # K3s source rev f9212d5ae6886a41a584e0037d25cb79ffa9c35a verifies
  # vpn-auth owns authentication/up, skips `up` when already Running, and uses
  # this same preference for Flannel pod CIDRs. Since advertise-routes is a
  # complete list, this unit retains those entries while adding the lifeline
  # route, and follows k3s restarts without touching login.
  systemd.services.tailscale-advertise-site-a = {
    description = "Advertise the site-A BMC subnet through the WG lifeline";
    wantedBy = [ "multi-user.target" ];
    requires = [ "tailscaled.service" ];
    after = [
      "tailscaled.service"
      "k3s.service"
    ];
    partOf = [ "k3s.service" ];
    path = [
      pkgs.jq
      pkgs.tailscale
    ];
    script = ''
      set -eu
      existing_routes="$(${pkgs.tailscale}/bin/tailscale debug prefs \
        | ${pkgs.jq}/bin/jq -r \
            '[.AdvertiseRoutes[]? | select(. != "10.0.1.0/24")] | join(",")')"
      if [ -n "$existing_routes" ]; then
        tailscale set --advertise-routes=10.0.1.0/24,"$existing_routes"
      else
        tailscale set --advertise-routes=10.0.1.0/24
      fi
    '';
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };

  # Self-heal, because ordering alone cannot win this race (review 2026-08-04).
  # `advertise-routes` is a single replace-the-whole-list preference, and BOTH
  # this unit and k3s' Flannel Tailscale backend write it. `After=k3s.service`
  # only orders against k3s' readiness notification — Flannel sets the pod CIDR
  # *after* that, so it can drop the lifeline route we just added. The unit
  # above re-reads the current list and unions rather than overwriting, so
  # simply running it again converges; this timer does that periodically.
  # Cost is one CLI call every five minutes; the alternative is losing BMC
  # reachability silently until the next reboot.
  systemd.timers.tailscale-advertise-site-a = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "5min";
      AccuracySec = "30s";
    };
  };
}
