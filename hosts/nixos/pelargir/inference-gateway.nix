# Make a sleeping GPU host reachable over HTTP.
#
# ⛔ WHY THIS EXISTS. Nardol suspends to S3 after ~15 idle minutes, and an HTTP
# connection attempt does not send a magic packet. So the first voice command
# after a quiet period does not wait for the host — it FAILS. Every piece of the
# assistant can be working perfectly and the answer is still "sorry, something
# went wrong", because nobody woke the machine.
#
# ⛔ WHY HERE AND NOT ON NARDOL. Nardol cannot host the service that wakes
# nardol. Pelargir is always on, runs Home Assistant with hostNetwork, and is
# already the proven sender of the magic packet on nardol's L2 segment.
#
# Home Assistant's llama.cpp integration points at 127.0.0.1:8001 instead of
# nardol:8000; everything else about the integration is unchanged.
{ config, pkgs, ... }:
let
  gateway = pkgs.callPackage ../../../pkgs/nardol-gateway { };
in
{
  systemd.services.nardol-gateway = {
    description = "Wake-on-demand proxy to nardol's inference server";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];

    serviceConfig = {
      ExecStart = ''
        ${gateway}/bin/nardol-gateway \
          --listen 127.0.0.1:8001 \
          --upstream http://nardol:8000 \
          --mac 9c:6b:00:36:e0:e8 \
          --broadcast 10.0.0.255:9 \
          --wake-timeout 90s \
          --lease-url http://nardol:8002
      '';
      Restart = "always";
      RestartSec = "5s";

      DynamicUser = true;
      # Only needs to send a UDP broadcast and make outbound HTTP.
      RestrictAddressFamilies = [
        "AF_INET"
        "AF_INET6"
      ];
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = true;
      PrivateTmp = true;
      PrivateDevices = true;
      ProtectKernelTunables = true;
      ProtectKernelModules = true;
      ProtectControlGroups = true;
      SystemCallArchitectures = "native";
    };
  };

  # ⛔ LOOPBACK ONLY, AND THAT IS THE SECURITY MODEL. The upstream has no
  # authentication whatsoever, so this must never be reachable off-host. Home
  # Assistant runs with hostNetwork on this machine, which is the only reason
  # 127.0.0.1 is sufficient — it shares pelargir's network namespace.
  assertions = [
    {
      assertion = !(config.networking.firewall.allowedTCPPorts or [ ] != [ ]
        && builtins.elem 8001 (config.networking.firewall.allowedTCPPorts or [ ]));
      message = "pelargir: nardol-gateway must stay on loopback; port 8001 must not be opened.";
    }
  ];
}
