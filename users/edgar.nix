{ config, lib, ... }:

{  users ={
        users.edgar = {
         isNormalUser = true;
         home = "/home/edgar";
         description = "Edgar Saldivar";
         extraGroups = [ "wheel" "networkmanager" ];
         #shell = lib.mkDefault "/run/current-system/sw/bin/bash";
         openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
         hashedPassword = "$y$j9T$ET2aGnxdPcazW7JWBfPfo0$0nAEs/cggVEG1abKv98SlTl7FcVOml1SiCLjpA3Odj9";
};
};
}
