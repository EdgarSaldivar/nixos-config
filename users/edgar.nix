{ config, lib, ... }:

{  users ={
        users.edgar = {
         isNormalUser = true;
         home = "/home/edgar";
         description = "Edgar Saldivar";
         extraGroups = [ "wheel" "networkmanager" ];
         #shell = lib.mkDefault "/run/current-system/sw/bin/bash";
         openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
         hashedPassword = "$6$LuxIAS6Sdlda/UuT$7aKR35nuhv6HTuwxxtVzn7YFAuOVkI3a4aZ.c8KVD2En1jSc62Jw96VfssTJzW4mSmJEQudLdvBDsOCHcZ5r0.";
};
};
}
