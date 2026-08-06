# Osgiliath's Wi-Fi is a lower-priority recovery path. The foundation network
# remains MAC-matched and retains its DHCP metric; this module only makes the
# deliberately manual link eligible once its runtime-only credential is ready.
{
  config,
  lib,
  ...
}:
{
  networking.wireless = {
    enable = true;
    secretsFile = config.sops.templates."wpa-supplicant-secrets".path;
    networks.Penthouse.pskRaw = "ext:psk_penthouse";
  };

  systemd.network.networks."20-wifi-later".linkConfig.ActivationPolicy = lib.mkForce "up";

  # An empty interfaces list makes NixOS' single wpa_supplicant unit discover
  # the MAC-matched wireless interface. Avoid guessing a kernel interface name.
  systemd.services.wpa_supplicant = {
    after = [ "osgiliath-secrets-gate.service" ];
    requires = [ "osgiliath-secrets-gate.service" ];
  };
}
