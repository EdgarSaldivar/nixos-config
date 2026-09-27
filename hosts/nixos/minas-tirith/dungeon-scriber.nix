# minas-tirith — Dungeon Scriber's tailnet entry points.
#
# 1. The tailnet-only gate for the API NodePort.
#
# The API is exposed to the tailnet as a NodePort (manifests/dungeon-scriber.yaml.in,
# rendered from dungeon-scriber-release.nix). kube-proxy implements a NodePort as nat
# PREROUTING DNAT straight into FORWARD, so nixos-fw's INPUT rules never see it: without
# this gate the port would answer on the LAN and on any public address the router
# forwards. And tailscaled accepts all of tailscale0 ahead of nixos-fw anyway (see
# beszel-hub.nix), so an allowedTCPPorts entry would be neither necessary nor sufficient.
#
# The raw table runs before both nat and filter, so this drop is the only rule that
# decides. It matches only packets addressed to this host itself
# (`addrtype --dst-type LOCAL`), which is what NodePort traffic is before its DNAT.
# Pod traffic forwarded through minas to other destinations on the same port number is
# untouched.
#
# It is installed whatever the release gates say. Gating it on `staged` would make the
# NodePort's protection depend on rebuilding minas before pelargir raises exposure,
# which nothing enforces across two hosts. Unconditional, it is in place from the first
# minas rebuild, and with nothing listening on the port it changes no traffic until
# exposure exists.
#
# 2. Optional HTTPS in front of it with `tailscale serve`.
#
# Both clients require HTTPS (ADR 0010 §5), and the NodePort is plain HTTP. With
# `minas.dungeonScriber.tailnetServe.enable` (which follows the release's tailnet.https),
# tailscaled terminates TLS for this node's MagicDNS name with a tailnet certificate
# and proxies to the NodePort over loopback, so clients use
# https://<node>.<tailnet>.ts.net. The tailnet must have MagicDNS and HTTPS
# certificates enabled first; see docs/runbooks/minas-tirith/dungeon-scriber.md.
#
# Serve reaches the NodePort from loopback. kube-proxy DNATs it in OUTPUT (localhost
# NodePorts), and the serve-only rule exempts `lo` explicitly anyway.
#
# checks/dungeon-scriber-deployment-contract.nix fails the build if either changes shape.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.minas.dungeonScriber;
  inherit (cfg) release;
  port = toString release.tailnet.port;
  iface = release.tailnet.interface;
  serve = cfg.tailnetServe;
  local = "-m addrtype --dst-type LOCAL";
  # With Serve on, the NodePort is dropped on every interface but loopback, the tailnet
  # included. HTTPS through Serve becomes the only path, so no tailnet client can reach
  # the API around the proxy and forge the X-Forwarded-For it trusts for one hop.
  tailnetRule = "-p tcp --dport ${port} ${local} ! -i ${iface} -j DROP";
  serveOnlyRule = "-p tcp --dport ${port} ${local} ! -i lo -j DROP";
  rule = if serve.enable then serveOnlyRule else tailnetRule;
  httpsPort = toString serve.httpsPort;
  backend = "http://127.0.0.1:${port}";
in
{
  options.minas.dungeonScriber = {
    release = lib.mkOption {
      type = lib.types.attrs;
      default = import ./dungeon-scriber-release.nix;
      description = ''
        The Dungeon Scriber release this host follows. Always the release file in
        production; the contract checks substitute synthetic releases here.
      '';
    };
    tailnetServe = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = release.tailnet.https;
        description = ''
          HTTPS for the Dungeon Scriber API on this node's tailnet name, through
          tailscale serve. Follows the release's tailnet.https, which pelargir also reads
          to render the API's trusted proxy hops and NetworkPolicy; the two must agree.
        '';
      };
      httpsPort = lib.mkOption {
        # tailscale serve terminates HTTPS only on these three ports.
        type = lib.types.enum [
          443
          8443
          10000
        ];
        default = 443;
        description = ''
          Tailnet HTTPS port. On this node 443 is also Traefik's hostPort. Serve answers
          tailnet connections to this node's Tailscale address before the kernel sees
          them, so tailnet clients reach Traefik on 443 no longer. Nothing in the fleet
          does that today (the ingress probe uses public DNS). Choose 8443 if something
          starts to.
        '';
      };
    };
  };

  config = {
    # Both variants are deleted first, so toggling Serve never leaves the other rule
    # behind in the raw table (which nixos-fw's reload does not flush).
    networking.firewall.extraCommands = lib.mkAfter ''
      ip46tables -t raw -D PREROUTING ${tailnetRule} 2>/dev/null || true
      ip46tables -t raw -D PREROUTING ${serveOnlyRule} 2>/dev/null || true
      ip46tables -t raw -A PREROUTING ${rule}
    '';
    networking.firewall.extraStopCommands = lib.mkAfter ''
      ip46tables -t raw -D PREROUTING ${tailnetRule} 2>/dev/null || true
      ip46tables -t raw -D PREROUTING ${serveOnlyRule} 2>/dev/null || true
    '';

    assertions = [
      {
        assertion = serve.enable == release.tailnet.https;
        message = "minas.dungeonScriber.tailnetServe.enable must equal tailnet.https in dungeon-scriber-release.nix, which pelargir renders the API from";
      }
    ]
    ++ lib.optionals serve.enable [
      {
        assertion = release.tailnetExposure;
        message = "minas.dungeonScriber.tailnetServe needs tailnetExposure: it proxies to the API NodePort";
      }
      {
        # Serve is one proxy hop. The API may trust it (1) or, while rolling between
        # states, nothing (0), and never anything further.
        assertion = lib.elem release.api.trustProxyHops [ 0 1 ];
        message = "minas.dungeonScriber.tailnetServe allows api.trustProxyHops of 0 or 1 only";
      }
    ];

    # Same shape as the fleet's other tailscale preference units
    # (pelargir/wireguard.nix): a oneshot that re-asserts one preference against the
    # running daemon and follows k3s restarts. The serve configuration persists in
    # tailscaled's state, so disabling this option must remove it explicitly. systemd
    # stops a unit that disappears on switch, and ExecStop turns the handler off.
    systemd.services.dungeon-scriber-tailnet-serve = lib.mkIf serve.enable {
      description = "Serve the Dungeon Scriber API over tailnet HTTPS";
      wantedBy = [ "multi-user.target" ];
      requires = [ "tailscaled.service" ];
      after = [
        "tailscaled.service"
        "k3s.service"
      ];
      partOf = [ "tailscaled.service" ];
      path = [ pkgs.tailscale ];
      script = ''
        set -eu
        tailscale serve --bg --https=${httpsPort} ${backend}
      '';
      preStop = ''
        tailscale serve --https=${httpsPort} off || true
      '';
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        Restart = "on-failure";
        RestartSec = "10s";
      };
    };
  };
}
