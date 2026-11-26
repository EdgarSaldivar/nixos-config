# edoras — identity, networking and administrative access.
{ config, ... }:
{
  networking = {
    hostName = "edoras";
    useDHCP = false;
    useNetworkd = true;
  };
  systemd.network.enable = true;

  # DHCP on any wired interface. Today that is the onboard I225-V, enp3s0,
  # 18:c0:4d:89:97:9b. Deliberately NOT pinned by MAC as osgiliath is: the name
  # glob already survives the PCI renumbering that once renamed triforce's
  # enp8s0 to enp9s0, and a MAC pin would leave a remote box with nobody on
  # site unreachable the day someone plugs in a different NIC. `en*` and not
  # `Type=ether`: veth links are ether too, and a container runtime must never
  # have networkd running DHCP on them.
  #
  # The cable is still what a fresh install needs: the Wi-Fi passphrase is not
  # in this repo or in sops, and is written onto the box over the tailnet
  # once it can be reached. After that the radio can carry it alone, with
  # worse route metrics so the cable wins whenever it is plugged in (./wifi.nix).
  systemd.network.networks."10-ethernet" = {
    matchConfig.Name = "en*";
    networkConfig.DHCP = "yes";
    linkConfig.RequiredForOnline = "routable";
  };
  # A board with two NICs and one cable must not hold boot on the other.
  systemd.network.wait-online.anyInterface = true;

  # The only way in from off-site. The auth key is tagged tag:fleet and
  # pre-approved, so first boot joins the tailnet with no interactive login;
  # tailscaled-autoconnect reads it once and does nothing on later boots.
  services.tailscale = {
    enable = true;
    authKeyFile = config.sops.secrets.tailscale_auth_key.path;
    # 41641/udp, so peers can reach this node directly instead of through DERP.
    openFirewall = true;
  };

  # SSH on the tailnet only. It was open on the LAN too until the host had run
  # cleanly, as an install-day fallback; at the friend's house nobody who
  # manages it is ever on that LAN, and on-site rescue is the console login
  # (./secrets.nix). tailscaled already accepts tailscale0 ahead of nixos-fw;
  # the explicit opening below is what the firewall itself says.
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 22 ];
  services.openssh = {
    openFirewall = false;
    ports = [ 22 ];
    hostKeys = [
      {
        path = "/etc/ssh/ssh_host_ed25519_key";
        type = "ed25519";
      }
    ];
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  security.sudo = {
    enable = true;
    wheelNeedsPassword = false;
  };

  system.stateVersion = "26.05";
}
