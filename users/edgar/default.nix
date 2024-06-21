{ config, utils, lib, pkgs, ... }:

imports =
    [
      # Include sops-nix
      (import (builtins.fetchTarball {
        # Adjust the version number as needed
        url = "https://github.com/Mic92/sops-nix/archive/refs/tags/v2.7.1.tar.gz";
      }))
    ];

let
  secrets = sops-nix.decrypt ./secrets.enc.yaml;
in
{
  users = {
    mutableUsers = false;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      extraGroups = [ "wheel" "networkmanager" ];
      hashedPassword = secrets.users.edgar.hashedPassword;
      openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
    };
  };

}


