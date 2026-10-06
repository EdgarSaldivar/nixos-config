# Optimizer concurrency applies to its own public torrents; global Deluge limits
# remain untouched. Credentials are read from existing app XML only at runtime.
{
  lib,
  pkgs,
  ...
}:
let
  policy = builtins.fromJSON (builtins.readFile ../../../policies/media-library-policy.json);
  package = pkgs.callPackage ../../../pkgs/media-optimizer { };
  settings = {
    state_dir = "/var/lib/media-optimizer";
    legacy_state_dir = "/home/edgar/.local/state/media-library-optimization";
    concurrency = policy.campaign.concurrentDownloads;
    verification_concurrency = policy.campaign.verificationConcurrency;
    minimum_savings = policy.campaign.ordinaryMinimumSavingsFraction;
    cooldown_days = policy.campaign.cooldownDays;
    protected_title_regex = policy.campaign.protectedTitleRegex;
    original_retention_days = 0;
    preferred_mb_per_minute = policy.preferredMBPerMinute;
    stage_host = "/storage/Media/Torrents/optimization";
    stage_deluge = "/data/optimization";
    deluge_label = "media-optimizer";
    deluge_url = "http://10.43.167.246:8112/json";
    deluge_credentials = {
      app = "animearr";
      client_id = 1;
    };
    apps =
      lib.mapAttrs
        (name: port: {
          url = "http://127.0.0.1:${toString port}";
          config_xml = "/home/edgar/docker-services/${name}/config/config.xml";
        })
        {
          radarr = 7878;
          sonarr = 8989;
          animearr = 9292;
        };
  };
  configFile = pkgs.writeText "media-optimizer.json" (builtins.toJSON settings);
  control = pkgs.writeShellScriptBin "media-optimization" ''
    exec ${package}/bin/media-optimizer --config ${configFile} "$@"
  '';
in
{
  environment.systemPackages = [ control ];
  environment.etc."media-optimizer.json".source = configFile;
  systemd.services.media-optimizer = {
    description = "Gradual public media replacement downloads";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "k3s.service"
    ];
    wants = [ "network-online.target" ];
    environment = {
      MEDIA_OPTIMIZER_CONFIG = configFile;
      MEDIA_OPTIMIZER_POLICY_JSON = builtins.toJSON settings;
      MEDIA_OPTIMIZER_CONTROL = "${control}/bin/media-optimization";
    };
    serviceConfig = {
      User = "edgar";
      Group = "users";
      ExecStart = "${package}/bin/media-optimizer --config ${configFile} run";
      Restart = "on-failure";
      RestartSec = "60s";
      StateDirectory = "media-optimizer";
      StateDirectoryMode = "0700";
      UMask = "0022";
      Nice = 10;
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ProtectHome = "read-only";
      ReadWritePaths = [ "/storage/Media" ];
      TimeoutStopSec = "30s";
    };
  };
}
