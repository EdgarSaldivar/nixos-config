{ config, utils, lib, pkgs, ... }:

let
  sops = import (builtins.fetchGit {
    url = "https://github.com/Mic92/sops-nix";
    ref = "master";
  });
  secrets = sops.decrypt ./secrets.enc.yaml;
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
