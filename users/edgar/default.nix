{ config, utils, lib, pkgs, sops, ... }:

{
  sops.secrets."users/edgar/hashedPassword" = {
    sopsFile = ./secrets.enc.yaml;
    neededForUsers = true;
  };
  sops.age.keyFile = "/etc/age/keys.txt";

  users = {
    mutableUsers = false;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      extraGroups = [ "wheel" "networkmanager" ];
      #password = "password";
      hashedPasswordFile = config.sops.secrets."users/edgar/hashedPassword".path;
      openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
    };
  };
}
