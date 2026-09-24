# minas-tirith — Beszel hub: the fleet's live system-metrics dashboard.
#
# Browser path: https://status.saldivar.io → Traefik (Authentik ForwardAuth) → the
# selector-less `monitoring/beszel` Service → 10.0.1.6:8090 on cni0. See
# manifests/node-services.yaml and traefik-routes/catalog.nix.
#
# ⛔ The hub logs a request in as whoever `X-authentik-email` names. That is safe
# ONLY because Traefik's ForwardAuth overwrites the header and nothing else can
# reach this port: it is not opened on eth0 or tailscale0, and cni0 (trusted by
# k3s-node.nix) is the Pod bridge. Opening 8090 anywhere else hands out logins.
#
# Agents (modules/nixos/fleet/metrics.nix) are reached hub → agent over SSH on
# the tailnet. The agent only accepts this hub's key, so the agents hold no
# secret; the hub's private key never leaves this host's state directory.
{
  lib,
  pkgs,
  config,
  ...
}:
let
  cfg = config.services.beszel.hub;
  dataDir = "${cfg.dataDir}/beszel_data";

  # Declared, not clicked: the hub reconciles its systems table with config.yml on
  # every start and deletes systems that are not listed. See ./beszel-systems.nix.
  systems = import ./beszel-systems.nix;
  systemsFile = (pkgs.formats.yaml { }).generate "beszel-config.yml" {
    systems = map (s: s // { port = 45876; }) systems;
  };

  # Replaces the module's ExecStartPre so the three first-run steps happen in one
  # place and in order:
  #   1. the SSH key exists before anything reads it, and its PUBLIC half is
  #      published to /run so an operator can read it without touching state;
  #   2. `migrate up` sees USER_EMAIL/USER_PASSWORD, which it consumes exactly once
  #      to create the Authentik-matched user (the password is random and only a
  #      break-glass credential — login is by header);
  #   3. config.yml is refreshed from the store.
  hubInit = pkgs.writeShellApplication {
    name = "beszel-hub-init";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.openssh
      cfg.package
    ];
    text = ''
      umask 077
      mkdir -p ${dataDir}
      if [ ! -e ${dataDir}/id_ed25519 ]; then
        ssh-keygen -q -t ed25519 -N "" -C beszel-hub@minas-tirith -f ${dataDir}/id_ed25519
      fi
      umask 022
      ssh-keygen -y -f ${dataDir}/id_ed25519 > /run/beszel-hub/hub.pub
      umask 077

      if [ ! -s ${cfg.dataDir}/break-glass-password ]; then
        head -c 32 /dev/urandom | base64 | tr -d '/+=' > ${cfg.dataDir}/break-glass-password
      fi
      USER_PASSWORD=$(cat ${cfg.dataDir}/break-glass-password)
      export USER_PASSWORD
      beszel-hub migrate up
      beszel-hub history-sync

      install -m 0600 ${systemsFile} ${dataDir}/config.yml
    '';
  };
in
{
  services.beszel.hub = {
    enable = true;
    # Wildcard for the same reason as Scrutiny: the Traefik Pod reaches the node's
    # LAN address over cni0. The firewall, not the bind, keeps it private.
    host = "0.0.0.0";
    port = 8090;
    environment = {
      APP_URL = "https://status.saldivar.io";
      TRUSTED_AUTH_HEADER = "X-authentik-email";
      # The Authentik administrator (AUTHENTIK_BOOTSTRAP_EMAIL in authentik.yaml).
      # Read once, on the first migration; it names the user the header logs in.
      USER_EMAIL = "teremaire@gmail.com";
    };
  };

  systemd.services.beszel-hub.serviceConfig.ExecStartPre = lib.mkForce [ (lib.getExe hubInit) ];

  # Deliberately NO networking.firewall.*.allowedTCPPorts entry for 8090 — see the
  # header. checks/fleet-metrics.nix fails the build if one appears.
}
