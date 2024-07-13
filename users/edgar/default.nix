{ config, utils, lib, pkgs, sops, ... }:


  secrets = sops.secrets."./secrets.enc.yaml".data;

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

