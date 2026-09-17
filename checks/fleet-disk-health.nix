{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

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
    imladris = "imladris";
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
    # ⛔ `devices` must be present EXACTLY when the host declares overrides.
    #
    # This used to read `collector.settings ? devices`, forbidding per-device
    # configuration outright — correct while every collector sat on direct
    # -attached disks, where an explicit device list is a brittle name
    # dependency. imladris broke that assumption: behind its ASM2464 USB bridge
    # smartctl auto-detects `-d sat`, which fails with "unsupported scsi
    # opcode", so the collector succeeds while seeing nothing.
    #
    # The biconditional keeps BOTH failures impossible: a host that needs
    # overrides cannot silently lose them, and a host that does not must not
    # grow a hand-maintained device list.
    || (collector.settings ? devices) != (cfg.fleet.diskHealth.deviceOverrides != [ ])
    || !timer.timerConfig.Persistent
    || !lib.elem "network-online.target" unit.after
    || !lib.elem "tailscaled.service" unit.after
  ) expected;

  # imladris' overrides are the reason the rule above was relaxed, so they are
  # pinned rather than merely permitted: four bays, all through ASMedia's vendor
  # passthrough. A pool with no parity has SMART as its only early warning.
  imladrisOverrides = nixosConfigurations.imladris.config.fleet.diskHealth.deviceOverrides;
  imladrisOverridesBroken =
    lib.length imladrisOverrides != 4 || lib.any (d: d.type or "" != "sntasmedia") imladrisOverrides;

  # ⛔ An override path containing an uppercase letter monitors NOTHING, silently.
  #
  # Scrutiny 0.9.2's collector lowercases each configured device path before
  # exec'ing smartctl, so the natural by-id spelling
  # (usb-ASMT_ASM246X_AAAABBBB0007-0:0) is executed as ...asmt_asm246x... and
  # fails to open on a case-sensitive filesystem. Every device then reports
  # model="" serial="", gets no UUID, and is skipped — while the collector exits
  # zero. imladris shipped exactly that bug on 2026-09-11.
  #
  # Fleet-wide rather than imladris-specific: the lowercasing is a property of
  # the collector, so any host that ever grows overrides inherits the trap.
  # Nothing legitimate needs an uppercase device path — udev can always be asked
  # for a lowercase alias, which is what imladris' services.udev.extraRules does.
  mixedCaseOverrideHosts = lib.attrNames (
    lib.filterAttrs (
      name: _:
      lib.any (
        d: lib.toLower (d.device or "") != (d.device or "")
      ) nixosConfigurations.${name}.config.fleet.diskHealth.deviceOverrides
    ) expected
  );

  minas = nixosConfigurations.minas-tirith.config;
in
if brokenCollectors != { } then
  throw "fleet disk-health collector contract failed for: ${lib.concatStringsSep ", " (builtins.attrNames brokenCollectors)}"
else if imladrisOverridesBroken then
  throw "imladris must declare all four enclosure bays with type sntasmedia; auto-detection reads them as -d sat and returns nothing"
else if mixedCaseOverrideHosts != [ ] then
  throw "disk-health deviceOverrides must use all-lowercase device paths (Scrutiny lowercases them before exec'ing smartctl, so an uppercase path never opens and the host monitors nothing while exiting zero); offending hosts: ${lib.concatStringsSep ", " mixedCaseOverrideHosts}"
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
