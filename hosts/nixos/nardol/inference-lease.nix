# Admission control for a host that is allowed to fall asleep.
#
# ⛔ WHAT THIS FIXES, PRECISELY. pelargir's wake gateway can observe nardol
# ready and still lose: the idle loop may call suspend a moment later, while the
# request is in flight. Polling /slots cannot prevent it, because polling
# inverts systemd's contract — the contract is take-the-lock-THEN-work, and an
# inhibitor taken after logind has admitted a sleep operation is refused.
#
# The serialisation point is logind itself. Both Inhibit() and the suspend
# request go through it, so exactly one wins:
#
#   Inhibit() first -> block lock held -> the later suspend does not proceed.
#   suspend first   -> Inhibit() fails -> this service answers 409 and the
#                      gateway has sent NOTHING upstream, so retrying the
#                      connection after resume replays no side effects.
#
# That is a closure, not a narrowing. A health check, a flag file or a slot poll
# cannot provide it.
#
# ⚠️ REQUIRES systemd >= 257, where `block` locks bind privileged callers too;
# the older weaker behaviour is now spelled `block-weak`. nardol runs 260.2.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  lease = pkgs.callPackage ../../../pkgs/nardol-lease { };
  profileData = import ../../../lib/inference-profiles.nix;
  inferenceUnit = "docker-${config.nardol.inference.containerName}.service";
in
{
  systemd.services.nardol-lease = {
    description = "Inference admission lease (holds a logind block inhibitor)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];

    serviceConfig = {
      ExecStart = ''
        ${lease}/bin/nardol-lease \
          --listen 0.0.0.0:8002 \
          --ttl 120s \
          --health-url http://127.0.0.1:${toString config.nardol.inference.port}/health \
          --model-state ${lib.escapeShellArg config.nardol.inference.profileStateFile} \
          --default-model ${profileData.default} \
          --known-profiles ${lib.concatStringsSep "," (lib.attrNames profileData.profiles)} \
          --gaming-unit nardol-gaming.target \
          --inference-unit ${inferenceUnit} \
          --serving-state /run/nardol-inference/serving \
          --user-profiles-dir /var/lib/nardol-inference/profiles.d \
          ${lib.optionalString config.nardol.palantir.enable "--palantir-unit docker-palantir.service --palantir-off-file ${config.nardol.palantir.offFile}"}
      '';
      Restart = "always";
      RestartSec = "5s";

      # ⛔ NOT DynamicUser. The service must be able to take a logind sleep
      # inhibitor, and it shells out to systemd-inhibit to do so.
      User = "root";
      NoNewPrivileges = true;
      ProtectHome = true;
      PrivateTmp = true;
      SystemCallArchitectures = "native";
    };

    path = [ pkgs.systemd ];
  };

  # ⛔ A HELD LEASE MUST SURVIVE THE SERVICE RESTARTING, OR IT IS WORSE THAN
  # NOTHING. Killing the unit kills the systemd-inhibit children with it, so a
  # restart silently drops every lock while the gateway still believes it holds
  # one. Leases are short (120s) and the gateway renews, so the honest fix is to
  # make a restart LOOK like a lost lease: the gateway's renew gets 404 and it
  # stops assuming protection.
  #
  # Reachable on the LAN because the gateway lives on pelargir. Wyoming and the
  # model server are already exposed the same way; this host's boundary is the
  # network, not per-service auth.
  networking.firewall.interfaces.eth0.allowedTCPPorts = [ 8002 ];
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 8002 ];
}
