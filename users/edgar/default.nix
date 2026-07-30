# Dropped the unused `utils` and `sops` module arguments: neither is supplied
# by the module system here, so any host importing this failed to evaluate
# with "called without required argument 'sops'".
{ config, lib, ... }:
{
  users = {
    mutableUsers = false;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      # `networkmanager` only exists as a group when NetworkManager is enabled.
      # minas-tirith uses systemd-networkd, so unconditionally listing it made
      # user activation complain about a group that does not exist on that host.
      extraGroups = [
        "wheel"
      ] ++ lib.optional config.networking.networkmanager.enable "networkmanager";
      openssh.authorizedKeys.keys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW"
      ];
    };
  };
}
