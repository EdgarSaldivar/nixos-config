{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Fleet metrics (Beszel) and the two host-native dashboards published through
# Traefik. Each rule below is a way the fleet could leak a login or go blind
# while every unit still reports active.
let
  hosts = [
    "minas-tirith"
    "pelargir"
    "osgiliath"
    "imladris"
    "nardol"
  ];
  minas = nixosConfigurations.minas-tirith.config;
  catalog = import ../hosts/nixos/minas-tirith/traefik-routes/catalog.nix {
    pinCollectorRelease = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
  };
  hubSystems = map (s: s.name) (import ../hosts/nixos/minas-tirith/beszel-systems.nix);

  allPorts =
    fw:
    fw.allowedTCPPorts ++ lib.concatMap (i: i.allowedTCPPorts or [ ]) (lib.attrValues fw.interfaces);
  nonTailnetPorts =
    fw:
    fw.allowedTCPPorts
    ++ lib.concatMap (i: fw.interfaces.${i}.allowedTCPPorts or [ ]) (
      lib.filter (i: i != "tailscale0") (lib.attrNames fw.interfaces)
    );

  # Every host opts in, and an enabled agent is reachable on tailnet only.
  brokenAgents = lib.filter (
    name:
    let
      cfg = nixosConfigurations.${name}.config;
      agent = cfg.services.beszel.agent;
    in
    !cfg.fleet.metrics.enable
    || agent.enable != (cfg.fleet.metrics.hubPublicKey != null)
    || (
      agent.enable
      && (
        agent.openFirewall
        || lib.elem 45876 (nonTailnetPorts cfg.networking.firewall)
        || !lib.elem 45876 cfg.networking.firewall.interfaces.tailscale0.allowedTCPPorts
        || agent.environment.KEY != cfg.fleet.metrics.hubPublicKey
      )
    )
  ) hosts;

  unknownHubSystems = lib.filter (n: !lib.elem n hosts) hubSystems;
  hub = minas.services.beszel.hub;
  gate = minas.services.nginx;
  protected = catalog.authentikRollout.protectedRoutes;
in
if brokenAgents != [ ] then
  throw "fleet-metrics: Beszel agent contract failed for: ${lib.concatStringsSep ", " brokenAgents} (every host enables fleet.metrics; the agent runs exactly when hubPublicKey is set, and listens on tailscale0 only)"
else if unknownHubSystems != [ ] then
  throw "fleet-metrics: beszel-systems.nix names hosts with no agent: ${lib.concatStringsSep ", " unknownHubSystems}"
else if
  !hub.enable
  # ⛔ The hub logs in whoever the header names, so it listens on loopback only and
  # the gate in front of it (nginx on 8090) demands Traefik's client certificate.
  || hub.host != "127.0.0.1"
  || hub.port != 8091
  || hub.environment.TRUSTED_AUTH_HEADER or "" != "X-authentik-email"
  || !gate.enable
  || !gate.virtualHosts.beszel-gate.onlySSL
  || !lib.hasInfix "ssl_verify_client on;" (gate.virtualHosts.beszel-gate.extraConfig or "")
  || gate.virtualHosts.beszel-gate.locations."/".proxyPass or "" != "http://127.0.0.1:8091"
  || catalog.routes.beszel.scheme or "" != "https"
  || catalog.routes.beszel.serversTransport or "" != "beszel-gate@file"
  # Any firewall opening for the gate port, on any interface, widens who can try.
  || lib.elem 8090 (allPorts minas.networking.firewall)
  || lib.elem 8091 (allPorts minas.networking.firewall)
  # tailscaled accepts all of tailscale0 ahead of nixos-fw, so the absence of an
  # opening is not enough: the raw-table drop is what actually closes the tailnet.
  || !lib.hasInfix "-t raw -A PREROUTING -p tcp --dport 8090 ! -i cni0 -j DROP" minas.networking.firewall.extraCommands
then
  throw "fleet-metrics: the Beszel hub on minas-tirith must listen on 127.0.0.1:8091 behind the mutual-TLS nginx gate on 8090 (https + beszel-gate@file transport on the route), trust only X-authentik-email, have NO firewall opening for either port, and keep the raw-table drop for every path but cni0"
else if
  !(lib.elem "beszel" protected)
  || !(lib.elem "scrutiny" protected)
  || !(lib.elem "beszel" catalog.legacyBasicAuthFallbackRoutes)
  || !(lib.elem "scrutiny" catalog.legacyBasicAuthFallbackRoutes)
then
  throw "fleet-metrics: status.saldivar.io and scrutiny.saldivar.io have no native login; both must stay Authentik-protected, with BasicAuth as their rollback fallback"
else
  pkgs.runCommand "fleet-metrics-ok" { } "touch $out"
