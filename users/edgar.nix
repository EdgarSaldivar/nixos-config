{ config, lib, ... }:

{  users ={
        users.edgar = {
         isNormalUser = true;
         home = "/home/edgar";
         description = "Edgar Saldivar";
         extraGroups = [ "wheel" "networkmanager" ];
         #shell = lib.mkDefault "/run/current-system/sw/bin/bash";
         openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
         hashedPassword = "$6$CuG6PBQ863qJs0L5$TMHQb323NtPGT5zUrrAeOTw2uDfC/DMuwUUgZKh9rzEqUz2cfZcrrBCAVIw.sMrf6p/7jfhAJvkJo11yPCXFt.";
};
};
}
