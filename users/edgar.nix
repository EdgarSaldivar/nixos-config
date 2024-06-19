{ config, lib, ... }:

{  users ={
        users.edgar = {
         isNormalUser = true;
         home = "/home/edgar";
         description = "Edgar Saldivar";
         extraGroups = [ "wheel" "networkmanager" ];
         #shell = lib.mkDefault "/run/current-system/sw/bin/bash";
         openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
         hashedPassword = [ "$y$j9T$fxvEVdlUsr/cQv/XF3/5i/$VzioI2sZMwgiNITF1f0ZbbOfQJdNisduCR1O6W/9CIA" ];
};
};
}
