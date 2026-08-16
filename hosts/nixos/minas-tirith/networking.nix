# minas-tirith — networking.
#
# Split out of system.nix on 2026-08-16; contents unchanged.
{ config, pkgs, ... }:
{
  # ---------------------------------------------------------------------------
  # Networking — systemd-networkd, matched by MAC not by interface name.
  # ---------------------------------------------------------------------------
  # This board renumbers PCI devices (see pci=realloc=off in
  # hardware-configuration.nix) and a rename has broken networking on Edgar's
  # hardware before. Matching on MAC means the config survives enp38s0 becoming
  # something else.
  #
  # Static rather than DHCP: the old install relied on a DHCP reservation on a
  # LAN whose owner reconfigures things. A server should not depend on that.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.enable = true;

  # ⛔ THE LOCAL LAN MUST NOT BE ROUTED OVER TAILSCALE. Without this, replies to
  # LAN hosts leave via tailscale0 sourced from this node's 100.x address, the
  # peer discards them as coming from the wrong address, and the connection never
  # establishes — it presents as a total silent blackhole.
  #
  # This took down all 26 public hostnames on 2026-08-10. The friend's server at
  # 10.0.1.203 fronts the router's 80/443 forwards and reverse-proxies saldivar.io
  # to this host. Its SYNs arrived on eth0 addressed to our own MAC and were never
  # answered, because `ip route get 10.0.1.203` resolved to `dev tailscale0
  # table 52`. Tailscale installs policy rule 5270 (`from all lookup 52`), which
  # wins over the main table's own `10.0.0.0/20 dev eth0` link route.
  #
  # ⚠️ WHY IT LOOKED IMPOSSIBLE TO DIAGNOSE, recorded because it will mislead again:
  # a reverse-path/asymmetric-routing failure leaves NO evidence in the places you
  # look first. There is no conntrack entry, no iptables counter increments, and the
  # CNI hostPort DNAT rule never matches — so it reads as "the packet never arrived"
  # even though tcpdump shows it arriving with the correct destination MAC.
  #
  # ⚠️ Docker did not expose this: its published port and SNAT behaviour kept the
  # reply on the path the request came in on. A k3s `hostPort` is PREROUTING DNAT,
  # which leaves the reply to ordinary policy routing — so migrating traefik to k3s
  # is what made a long-latent routing conflict fatal.
  #
  # Priority 5000 places this ABOVE Tailscale's 5270. Destinations outside the LAN,
  # including all 100.64.0.0/10 tailnet traffic and the k3s control-plane link to
  # pelargir, are untouched.
  #
  # ⛔ DO NOT "FIX" THIS UPSTREAM BY UNADVERTISING 10.0.1.0/24 ON PELARGIR. That
  # advertisement is deliberate: it is how tailnet clients reach site A's BMC through
  # pelargir's WireGuard lifeline (see pelargir/wireguard.nix — the wg0 peer carries
  # `allowedIPs = 10.0.1.0/24`, and its nftables forward rules scope the
  # tailscale0<->wg0 path to exactly that subnet). Removing it breaks BMC access for
  # every other tailnet client to fix a problem only this host has.
  #
  # ⛔ NOR can this host set `--accept-routes=false`: it needs pelargir's advertised
  # 10.42.0.0/24 pod CIDR for inter-node pod traffic, because the cluster's only
  # transport between sites is the tailnet. Accepting routes is required; preferring
  # the connected route for the LAN we are physically ON is the correct scope.
  #
  # ✅ CHECKED 2026-08-10: minas-tirith is the ONLY node directly attached to a subnet
  # that is advertised into the tailnet. pelargir is not on 10.0.1.0/24; osgiliath is
  # on 192.168.117.0/24. So this rule is complete, not one instance of a pattern — but
  # if a future node is added ON an advertised subnet, it needs the same rule.
  systemd.services.lan-route-priority = {
    description = "Prefer the LAN link over Tailscale for local 10.0.0.0/20 destinations";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # Idempotent: `ip rule add` duplicates silently on re-run, so delete first.
      ExecStart = pkgs.writeShellScript "lan-route-priority" ''
        ${pkgs.iproute2}/bin/ip rule del to 10.0.0.0/20 lookup main priority 5000 2>/dev/null || true
        ${pkgs.iproute2}/bin/ip rule add to 10.0.0.0/20 lookup main priority 5000
      '';
      ExecStop = pkgs.writeShellScript "lan-route-priority-stop" ''
        ${pkgs.iproute2}/bin/ip rule del to 10.0.0.0/20 lookup main priority 5000 2>/dev/null || true
      '';
    };
  };

  systemd.network.networks."10-lan" = {
    matchConfig.MACAddress = "a8:a1:59:c0:4e:73"; # was enp38s0
    address = [ "10.0.1.6/20" ];
    routes = [
      { Gateway = "10.0.0.1"; }
      # This subnet is retired for k3s transport, which now uses Tailscale, but
      # it is NOT stale yet: site A's router still terminates the WireGuard
      # lifeline to pelargir. Keep the return route until that peer is retired.
      # Docker must also remain pinned away from 192.168.x (see ./containers.nix).
      {
        Destination = "192.168.4.0/24";
        Gateway = "10.0.0.1";
      }
    ];
    networkConfig = {
      DNS = [ "10.0.0.1" ];
      IPv6AcceptRA = true;
    };
    linkConfig.RequiredForOnline = "routable";
  };

  # Second onboard NIC (a8:a1:59:c0:4e:72) is unused and unplugged. Declared so
  # boot does not wait on it.
  systemd.network.networks."20-lan-unused" = {
    matchConfig.MACAddress = "a8:a1:59:c0:4e:72";
    linkConfig.ActivationPolicy = "down";
    linkConfig.RequiredForOnline = "no";
  };

}
