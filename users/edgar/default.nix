# Dropped the unused `utils` and `sops` module arguments: neither is supplied
# by the module system here, so any host importing this failed to evaluate
# with "called without required argument 'sops'".
{ ... }:
{
  users = {
    mutableUsers = false;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      extraGroups = [
        "wheel"
        "networkmanager"
      ];
      openssh.authorizedKeys.keys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW"
      ];
    };
  };
}
