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
      settings = {
        host.id = cfg.hostId;
        api.endpoint = cfg.endpoint;
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
