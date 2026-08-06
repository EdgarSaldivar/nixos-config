# osgiliath — identity, networking and administrative access.
{ ... }:
{
  networking = {
    hostName = "osgiliath";
    useDHCP = false;
    useNetworkd = true;
  };
  systemd.network.enable = true;

  # Production LAN: DHCP is intentional, while matching by MAC avoids relying
  # on PCI discovery order or a particular predictable interface name.
  systemd.network.networks."10-ethernet" = {
    matchConfig.MACAddress = "84:47:09:6a:b2:5d";
    networkConfig.DHCP = "ipv4";
    dhcpV4Config.RouteMetric = 100;
    linkConfig.RequiredForOnline = "routable";
  };

  # LATER WI-FI SLICE BOUNDARY: the hardware and MAC match are declared here,
  # but association must be added by a secrets-backed iwd/wpa_supplicant unit.
  # Do not put an SSID or PSK in this file or elsewhere in the Git tree. Keeping
  # activation manual also prevents an unconfigured radio delaying this slice.
  systemd.network.networks."20-wifi-later" = {
    matchConfig.MACAddress = "9c:12:21:ac:79:82";
    networkConfig.DHCP = "ipv4";
    dhcpV4Config.RouteMetric = 600;
    linkConfig = {
      ActivationPolicy = "manual";
      RequiredForOnline = "no";
    };
  };

  services.openssh = {
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
