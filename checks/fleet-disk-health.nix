{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:

# Every disk collector must retain a stable fleet identity, the private
# endpoint, catch-up scheduling and its tailnet dependency. Keep the
# central service host-native and prevent Nardol's collector role from
# quietly turning it into a k3s node.
  let
    expected = {
      minas-tirith = "minas-tirith";
      nardol = "nardol";
      osgiliath = "osgiliath";
      pelargir = "pelargir";
    };
    brokenCollectors = lib.filterAttrs (
      name: hostId:
      let
        cfg = nixosConfigurations.${name}.config;
        collector = cfg.services.scrutiny.collector;
        unit = cfg.systemd.services.scrutiny-collector;
        timer = cfg.systemd.timers.scrutiny-collector;
      in
      !cfg.fleet.diskHealth.enable
      || !cfg.services.tailscale.enable
      || !collector.enable
      || collector.package.version != "0.9.2"
      || collector.schedule != "hourly"
      || collector.settings.host.id != hostId
      || collector.settings.api.endpoint != "http://minas-tirith:9080"
      || collector.settings ? devices
      || !timer.timerConfig.Persistent
      || !lib.elem "network-online.target" unit.after
      || !lib.elem "tailscaled.service" unit.after
    ) expected;
    minas = nixosConfigurations.minas-tirith.config;
  in
  if brokenCollectors != { } then
    throw "fleet disk-health collector contract failed for: ${lib.concatStringsSep ", " (builtins.attrNames brokenCollectors)}"
  else if
    !minas.services.scrutiny.enable
    || minas.services.scrutiny.package.version != "0.9.2"
    || !minas.services.scrutiny.influxdb.enable
    || minas.services.scrutiny.settings.web.listen.host != "0.0.0.0"
    || minas.services.scrutiny.settings.web.listen.port != 9080
    || minas.services.scrutiny.settings.web.influxdb.host != "127.0.0.1"
    || minas.services.scrutiny.settings.user.metrics.repeat_notifications
    || minas.services.scrutiny.collector.settings.commands.metrics_scan_args != "--scan-open --json"
    || minas.services.influxdb2.settings."http-bind-address" != "127.0.0.1:8086"
    || minas.services.scrutiny.openFirewall
    || !lib.elem 9080 minas.networking.firewall.interfaces.tailscale0.allowedTCPPorts
    || lib.elem 9080 minas.networking.firewall.allowedTCPPorts
  then
    throw "minas-tirith Scrutiny must remain host-native, pinned, and tailnet-only"
  else if nixosConfigurations.nardol.config.services.k3s.enable then
    throw "Nardol's disk collector must not enable k3s"
  else
    pkgs.runCommand "fleet-disk-health-ok" { } "touch $out"
