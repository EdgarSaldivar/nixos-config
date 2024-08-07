{ config, utils, lib, pkgs, sops, ... }:

{
  sops.secrets = {
    "secrets.yaml" = {
      path = "./secrets.yaml.age";
      age = {
        recipients = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
      };
    };
  };

  users = {
    mutableUsers = false;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      extraGroups = [ "wheel" "networkmanager" ];
      openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
      password = "${config.sops.secrets."secrets.yaml".users.edgar.Password}";
    };
  };
}
