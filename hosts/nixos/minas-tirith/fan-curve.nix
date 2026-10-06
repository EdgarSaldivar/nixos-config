# Persistent, fail-full RTX 2080 cooling for this host only. BMC header identity is
# unverified, so every BMC output stays at fixed manual 100% duty.
{ lib, pkgs, ... }:
let
  controller = pkgs.writeTextFile {
    name = "minas-gpu-fans";
    destination = "/bin/minas-gpu-fans";
    executable = true;
    text = builtins.replaceStrings
      [ "#!/usr/bin/env python3" ]
      [ "#!${pkgs.python3}/bin/python3" ]
      (builtins.readFile ./scripts/minas-gpu-fans.py);
  };
  settings = {
    nvml_library = "/run/opengl-driver/lib/libnvidia-ml.so.1";
    gpu_uuid = "GPU-8fc251bf-f300-03e1-98eb-d2929b62c23e";
    expected_fans = 2;
    ipmitool = "${pkgs.ipmitool}/bin/ipmitool";
    smartctl = "${pkgs.smartmontools}/bin/smartctl";
    status_path = "/run/minas-gpu-fan-curve/status.json";
    floor = 50;
    steps = [
      { percent = 65; gpu = 55; motherboard = 45; x570 = 65; hdd = 40; }
      { percent = 80; gpu = 65; motherboard = 50; x570 = 75; hdd = 45; }
      { percent = 100; gpu = 75; motherboard = 55; x570 = 85; hdd = 50; }
    ];
    hysteresis_c = 3;
    cool_hold_seconds = 300;
    down_step_percent = 5;
    down_interval_seconds = 60;
    control_period = 10;
    board_period = 10;
    board_ttl = 30;
    hdd_period = 120;
    hdd_ttl = 180;
    hdds = map (id: "/dev/disk/by-id/${id}") [
      "ata-WDC_WD140EDFZ-11A0VA0_9MGH8STK"
      "ata-WDC_WD140EMFZ-11A0WA0_Z2HKXURT"
      "ata-WDC_WUH721414ALE604_9JH1LNDT"
      "ata-WDC_WD141PURP-74B5YY0_9RHGNX7L"
      "ata-WDC_WD140EDFZ-11A0VA0_9MGHLSMU"
      "ata-WDC_WD140EDFZ-11A0VA0_9MGH838K"
      "ata-WDC_WD140EDFZ-11A0VA0_Y5J31TWC"
    ];
  };
  configFile = pkgs.writeText "minas-gpu-fan-curve.json" (builtins.toJSON settings);
  command = "${controller}/bin/minas-gpu-fans --config ${configFile}";
in
{
  boot.kernelModules = [ "ipmi_devintf" "ipmi_si" ];

  systemd.services.minas-bmc-fans-full = {
    description = "Hold all Minas BMC fan outputs at fixed manual 100%";
    wantedBy = [ "multi-user.target" ];
    before = [ "minas-gpu-fan-curve.service" ];
    after = [ "systemd-modules-load.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${command} --bmc-full";
      TimeoutStartSec = "75s";
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };

  systemd.services.minas-gpu-fan-curve = {
    environment = {
      MINAS_FAN_CONFIG = configFile;
      # Reuse this literal in cross-platform policy tests without building the
      # Linux host's config derivation on Darwin.
      MINAS_FAN_POLICY_JSON = builtins.toJSON settings;
    };
    description = "Case-aware Minas RTX 2080 fan controller";
    wantedBy = [ "multi-user.target" ];
    wants = [ "minas-bmc-fans-full.service" ];
    after = [ "minas-bmc-fans-full.service" "systemd-modules-load.service" ];
    serviceConfig = {
      Type = "notify";
      NotifyAccess = "main";
      ExecStart = command;
      ExecStopPost = "${command} --hold-full";
      Restart = "always";
      RestartSec = "5s";
      WatchdogSec = "45s";
      TimeoutStopSec = "20s";
      RuntimeDirectory = "minas-gpu-fan-curve";
      RuntimeDirectoryMode = "0755";
    };
  };
}
