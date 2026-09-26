{
  config,
  lib,
  ...
}:
let
  cfg = config.fleet.diskHealth;
in
{
  options.fleet.diskHealth = {
    enable = lib.mkEnableOption "central fleet disk-health collection";

    hostId = lib.mkOption {
      type = lib.types.str;
      description = "Stable Scrutiny host identifier; set this explicitly per host.";
    };

    endpoint = lib.mkOption {
      type = lib.types.str;
      default = "http://minas-tirith:9080";
      readOnly = true;
      description = "Private tailnet Scrutiny API endpoint.";
    };

    deviceOverrides = lib.mkOption {
      type = lib.types.listOf (lib.types.attrsOf lib.types.str);
      default = [ ];
      example = lib.literalExpression ''
        [ { device = "/dev/disk/by-id/usb-..."; type = "sntasmedia"; } ]
      '';
      description = ''
        Explicit Scrutiny collector device entries, for hosts where smartctl's
        auto-detection produces a device type that cannot actually read SMART.

        ⛔ An EXCEPTION, not a knob. The default is the empty list, which omits
        the `devices` key entirely and leaves every existing host's collector
        byte-identical — verify with scripts/closure-equiv.sh before and after
        touching this option.

        Auto-detection is correct on direct-attached SATA, SAS and NVMe, and
        overriding it there would be a brittle device-name dependency of exactly
        the kind this module's discovery comment warns against.

        It is NOT correct behind every USB bridge. Measured on imladris'
        four-bay ASM2464 enclosure 2026-09-11: `smartctl --scan` reports `-d sat`
        for all four bays, and `-d sat` then fails with "Read Device Identity
        failed: scsi error unsupported scsi opcode". NVMe never crosses the USB
        link — the bridge translates it to SCSI — so real health requires
        ASMedia's vendor passthrough, `-d sntasmedia`. Without an override the
        collector runs green and reports nothing, which is the most dangerous
        possible outcome for a host whose pool has no redundancy.

        checks/fleet-disk-health.nix requires that hosts declaring overrides
        actually emit them, and that hosts declaring none never do.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.hostId != "";
        message = "fleet.diskHealth.hostId must be an explicit, non-empty stable identifier";
      }
    ];

    # This is also the complete Tailscale configuration for intermittently
    # powered collector-only hosts such as Nardol. Cluster hosts may separately
    # let k3s own their initial tailnet login, but still use ordinary tailscaled.
    services.tailscale.enable = true;

    services.scrutiny.collector = {
      enable = true;
      schedule = "hourly";
      # `devices` is added only when a host declares overrides, so the four
      # auto-detecting hosts keep an identical `settings` attrset and an
      # identical closure.
      settings = {
        host.id = cfg.hostId;
        api.endpoint = cfg.endpoint;
      }
      // lib.optionalAttrs (cfg.deviceOverrides != [ ]) {
        devices = cfg.deviceOverrides;
      };
    };

    # Scrutiny delegates discovery to smartctl, covering SATA/SAS/USB/NVMe
    # without brittle device-name overrides. Minas applies a host-specific
    # --scan-open override because its Adaptec HBA initially labels SAT disks
    # as generic SCSI during an ordinary scan.
    systemd.services.scrutiny-collector = {
      after = lib.mkAfter (
        [
          "network-online.target"
          "tailscaled.service"
        ]
        ++ lib.optional config.services.k3s.enable "k3s.service"
      );
      wants = lib.mkAfter [
        "network-online.target"
        "tailscaled.service"
      ];
    };

    # The upstream module already sets Persistent; repeat it here because
    # catch-up after downtime is part of the fleet contract, especially for
    # intentionally powered-off Nardol.
    systemd.timers.scrutiny-collector.timerConfig = {
      Persistent = true;
      RandomizedDelaySec = "5min";
    };
  };
}
