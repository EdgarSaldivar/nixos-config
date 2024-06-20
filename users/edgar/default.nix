{ config, lib, ... }:

{
  users = {
    mutableUsers = false;
    users.edgar = {
      isNormalUser = true;
      home = "/home/edgar";
      description = "Edgar Saldivar";
      shell = pkgs.fish;
      extraGroups = [ "wheel" "networkmanager" ];
      #passwordFile = config.age.secrets."passwords/users/edgar".path;
      hashedPassword = "$6$B.UffRI3k5R278aQ$v4I9eQJx81fhab/Hz3hJbmB.JOU6Zyr.o9yMXllp4oTkA.bZgLU9jEIJiC/3VTCYnWYmYuWfVYPg5MrsrL9KF/";
      openssh.authorizedKeys.keys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA+PoI3q67ZKz5oWtHVWfKzIRyBagoaFqYu/TqndfqTW" ];
    };
  };
}
