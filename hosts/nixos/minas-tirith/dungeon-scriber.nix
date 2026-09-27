# minas-tirith — the tailnet-only gate for Dungeon Scriber's API NodePort.
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
# checks/dungeon-scriber-deployment-contract.nix fails the build if it changes shape.
{ lib, ... }:
let
  release = import ./dungeon-scriber-release.nix;
  port = toString release.tailnet.port;
  iface = release.tailnet.interface;
  rule = "-p tcp --dport ${port} ! -i ${iface} -j DROP";
in
{
  networking.firewall.extraCommands = lib.mkAfter ''
    ip46tables -t raw -D PREROUTING ${rule} 2>/dev/null || true
    ip46tables -t raw -A PREROUTING ${rule}
  '';
  networking.firewall.extraStopCommands = lib.mkAfter ''
    ip46tables -t raw -D PREROUTING ${rule} 2>/dev/null || true
  '';
}
