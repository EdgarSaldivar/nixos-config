# minas-tirith — Dungeon Scriber's tailnet entry points.
#
# 1. The tailnet-only gate for the API NodePort (always on).
#
# The API is exposed to the tailnet as a NodePort (manifests/dungeon-scriber.yaml.in,
# rendered from dungeon-scriber-release.nix). kube-proxy implements a NodePort as nat
# PREROUTING DNAT straight into FORWARD, so nixos-fw's INPUT rules never see it: without
# this gate the port would answer on the LAN and on any public address the router
# forwards. And tailscaled accepts all of tailscale0 ahead of nixos-fw anyway (see
# beszel-hub.nix), so an allowedTCPPorts entry would be neither necessary nor sufficient.
#
# The raw table runs before both nat and filter, so this drop is the only rule that
# decides. It is installed unconditionally, before tailnetExposure is ever raised, so
# there is no window in which the NodePort exists without it.
#
# 2. Optional HTTPS in front of it with `tailscale serve` (off by default).
#
# Both clients require HTTPS (ADR 0010 §5), and the NodePort is plain HTTP. With
# `minas.dungeonScriber.tailnetServe.enable`, tailscaled terminates TLS for this node's
# MagicDNS name with a tailnet certificate and proxies to the NodePort over loopback, so
# clients use https://<node>.<tailnet>.ts.net. The tailnet must have MagicDNS and HTTPS
# certificates enabled first; see docs/runbooks/minas-tirith/dungeon-scriber.md.
#
# Serve traffic is handled inside tailscaled and reaches the NodePort from loopback.
# kube-proxy DNATs it in OUTPUT, and the serve-only rule exempts `lo` explicitly anyway.
# That depends on kube-proxy's localhost NodePorts (iptables mode's default); the
# runbook checks it on the host before the option is enabled.
#
# checks/dungeon-scriber-deployment-contract.nix fails the build if either changes shape.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  release = import ./dungeon-scriber-release.nix;
  port = toString release.tailnet.port;
  iface = release.tailnet.interface;
  cfg = config.minas.dungeonScriber.tailnetServe;
  # With serve enabled the NodePort is dropped on every interface but loopback, the
  # tailnet included. HTTPS through serve becomes the only path, so no tailnet client can
  # reach the API around the proxy and forge the X-Forwarded-For it trusts for one hop.
  tailnetRule = "-p tcp --dport ${port} ! -i ${iface} -j DROP";
  serveOnlyRule = "-p tcp --dport ${port} ! -i lo -j DROP";
  rule = if cfg.enable then serveOnlyRule else tailnetRule;
  httpsPort = toString cfg.httpsPort;
  backend = "http://127.0.0.1:${port}";
in
{
  options.minas.dungeonScriber.tailnetServe = {
    enable = lib.mkEnableOption "HTTPS for the Dungeon Scriber API on this node's tailnet name, through tailscale serve";
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

  config = {
    # Both variants are deleted first, so toggling serve never leaves the other rule
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

    assertions = lib.optionals cfg.enable [
      {
        assertion = release.tailnetExposure;
        message = "minas.dungeonScriber.tailnetServe needs tailnetExposure: it proxies to the API NodePort";
      }
      {
        # Serve is one proxy hop. The API must trust it to read the client address from
        # X-Forwarded-For, and trust nothing further.
        assertion = release.api.trustProxyHops == 1;
        message = "minas.dungeonScriber.tailnetServe needs api.trustProxyHops = 1 in dungeon-scriber-release.nix";
      }
    ];

    # Same shape as the fleet's other tailscale preference units
    # (pelargir/wireguard.nix): a oneshot that re-asserts one preference against the
    # running daemon and follows k3s restarts. The serve configuration persists in
    # tailscaled's state, so disabling this option must remove it explicitly. systemd
    # stops a unit that disappears on switch, and ExecStop turns the handler off.
    systemd.services.dungeon-scriber-tailnet-serve = lib.mkIf cfg.enable {
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
