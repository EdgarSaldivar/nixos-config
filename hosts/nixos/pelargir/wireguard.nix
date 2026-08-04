# pelargir — native WireGuard hub and its network boundaries.
{ config, ... }:
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
      {
        # mac — REPLACE-AT-RELIGHT.
        publicKey = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA=";
        presharedKeyFile = config.sops.secrets.wireguard_psk_mac.path;
        allowedIPs = [ "192.168.4.10/32" ];
      }
      {
        # phone — REPLACE-AT-RELIGHT.
        publicKey = "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCA=";
        presharedKeyFile = config.sops.secrets.wireguard_psk_phone.path;
        allowedIPs = [ "192.168.4.11/32" ];
      }
      {
        # minas_agent — REPLACE-AT-RELIGHT. After its first k3s join, append
        # the /24 assigned as minas' flannel pod-cidr to this AllowedIPs list.
        publicKey = "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDA=";
        presharedKeyFile = config.sops.secrets.wireguard_psk_minas.path;
        allowedIPs = [ "192.168.4.6/32" ];
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
    interfaces = {
      wg0 = {
        allowedTCPPorts = [
          22
          1883
          6443
          8080
          8123
          10250
        ];
        allowedUDPPorts = [ 8472 ];
      };
      eth0.allowedTCPPorts = [
        22
        1883
        8080
        8123
      ];
    };

    # Belt-and-braces for DNAT/ServiceLB traffic. INPUT-only rules cannot
    # protect forwarded ports; Traefik's middleware remains the primary ACL.
    extraForwardRules = ''
      ip saddr 10.0.0.0/24 tcp dport { 80, 443 } accept
      iifname "wg0" tcp dport { 80, 443 } accept
      ip saddr { ${builtins.concatStringsSep ", " cloudflareV4} } tcp dport { 80, 443 } accept
      ip6 saddr { ${builtins.concatStringsSep ", " cloudflareV6} } tcp dport { 80, 443 } accept
      tcp dport { 80, 443 } drop
    '';
  };
}
