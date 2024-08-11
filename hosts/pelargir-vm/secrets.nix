{ config, pkgs, ... }:

let
  generateAgeKey = pkgs.writeScript "generate-age-key" ''
    #!/bin/sh
    ssh-to-age -private-key /etc/ssh/ssh_host_ed25519_key > /etc/age/keys.txt
  '';
in
{
  systemd.services.generate-age-key = {
    description = "Generate age key from SSH host key";
    after = [ "network.target" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${generateAgeKey}";
      Type = "oneshot";
      RemainAfterExit = true;
    };
  };
}
