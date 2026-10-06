# Optimizer concurrency applies to its own public torrents; global Deluge limits
# remain untouched. Credentials are read from existing app XML only at runtime.
{
  config,
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
    subtitles = {
      bridge_port = 18787;
      instances = {
        main = {
          port = 16767;
          state_dir = "/var/lib/media-subtitles-main";
        };
        anime = {
          port = 16768;
          state_dir = "/var/lib/media-subtitles-anime";
        };
      };
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
  subtitleUnit = name: spec: {
    description = "Bazarr background subtitles (${name})";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "media-subtitle-bridge.service"
    ];
    requires = [ "media-subtitle-bridge.service" ];
    environment.PYTHONDONTWRITEBYTECODE = "1";
    serviceConfig = {
      User = "edgar";
      Group = "users";
      StateDirectory = "media-subtitles-${name}";
      StateDirectoryMode = "0700";
      ExecStartPre = "${control}/bin/media-optimization subtitle-seed ${name}";
      ExecStart = "${pkgs.bazarr}/bin/bazarr --config ${spec.state_dir} --port ${toString spec.port} --no-update True --no-signalr True";
      Restart = "on-failure";
      RestartSec = "30s";
      KillSignal = "SIGINT";
      SuccessExitStatus = "0 156";
      Nice = 10;
      UMask = "0022";
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = "read-only";
      ReadWritePaths = [ "/storage/Media" ];
    };
  };
  setupUnit = name: {
    description = "Assign English/native Bazarr subtitle preferences (${name})";
    after = [ "media-subtitles-${name}.service" ];
    requires = [ "media-subtitles-${name}.service" ];
    serviceConfig = {
      Type = "oneshot";
      User = "edgar";
      Group = "users";
      ExecStart = "${control}/bin/media-optimization subtitle-setup ${name}";
      TimeoutStartSec = "30min";
      Nice = 10;
    };
  };
in
{
  system.build.mediaOptimizationUnits = pkgs.linkFarm "media-optimization-units" (
    map
      (name: {
        inherit name;
        path = "${config.systemd.units.${name}.unit}/${name}";
      })
      [
        "media-optimizer.service"
        "media-subtitle-bridge.service"
        "media-subtitles-main.service"
        "media-subtitles-anime.service"
        "media-subtitles-setup-main.service"
        "media-subtitles-setup-anime.service"
        "media-subtitles-setup-main.timer"
        "media-subtitles-setup-anime.timer"
      ]
  );
  environment.systemPackages = [ control ];
  environment.etc."media-optimizer.json".source = configFile;
  systemd.services.media-subtitle-bridge = {
    description = "Read-only local Arr bridge for subtitle services";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    serviceConfig = {
      User = "edgar";
      Group = "users";
      ExecStart = "${control}/bin/media-optimization subtitle-bridge";
      Restart = "on-failure";
      RestartSec = "30s";
      NoNewPrivileges = true;
      ProtectSystem = "strict";
      ProtectHome = "read-only";
    };
  };
  systemd.services.media-subtitles-main = subtitleUnit "main" settings.subtitles.instances.main;
  systemd.services.media-subtitles-anime = subtitleUnit "anime" settings.subtitles.instances.anime;
  systemd.services.media-subtitles-setup-main = setupUnit "main";
  systemd.services.media-subtitles-setup-anime = setupUnit "anime";
  systemd.timers.media-subtitles-setup-main = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "3min";
      OnUnitActiveSec = "1h";
      Persistent = true;
    };
  };
  systemd.timers.media-subtitles-setup-anime = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "1h";
      Persistent = true;
    };
  };
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
