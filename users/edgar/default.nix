{ config, utils, lib, pkgs, ... }:

let
  sops = import (pkgs.fetchFromGitHub {
    owner = "Mic92";
    repo = "sops-nix";
    rev = "797ce4c";
    sha256 = "0f3k3vm6v0c2ld33i5fcfj3mj46z8qpyp7ymf4pbn78h2gy4hvry";
  });
  secrets = sops.secrets."./secrets.enc.yaml".data;
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
      #password = "123";
      openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
    };
  };
}
