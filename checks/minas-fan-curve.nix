# Offline policy and unit contract. The built config is the exact JSON passed to
# the service, so Python tests cannot silently test a second copy of the policy.
{ lib, pkgs, nixosConfigurations, ... }:
let
  minas = nixosConfigurations.minas-tirith.config;
  gpu = minas.systemd.services.minas-gpu-fan-curve;
  bmc = minas.systemd.services.minas-bmc-fans-full;
  configFile = gpu.environment.MINAS_FAN_CONFIG or null;
  rawPolicyJson = gpu.environment.MINAS_FAN_POLICY_JSON or null;
  # The JSON contains Linux binary paths but the offline tests never execute
  # those paths. Discard their string context so Darwin checks stay native.
  policyJson = if rawPolicyJson == null then null else builtins.unsafeDiscardStringContext rawPolicyJson;
  testConfig = pkgs.writeText "minas-gpu-fan-test-policy.json" policyJson;
  source = ../hosts/nixos/minas-tirith/scripts/minas-gpu-fans.py;
  tests = ../hosts/nixos/minas-tirith/scripts/tests/test_minas_gpu_fans.py;
  scriptText = builtins.readFile source;
  # Assert evaluated unit properties here; Python covers the actual policy and
  # the source text guard ensures the controller has no automatic-policy reset.
  contract =
    configFile != null
    && policyJson != null
    && lib.elem "ipmi_si" minas.boot.kernelModules
    && lib.elem "ipmi_devintf" minas.boot.kernelModules
    && lib.elem "minas-bmc-fans-full.service" gpu.wants
    && !lib.elem "minas-bmc-fans-full.service" gpu.requires
    && lib.elem "minas-bmc-fans-full.service" gpu.after
    && lib.hasInfix "--bmc-full" bmc.serviceConfig.ExecStart
    && lib.hasInfix "--hold-full" gpu.serviceConfig.ExecStopPost
    && bmc.serviceConfig.Type == "oneshot"
    && bmc.serviceConfig.RemainAfterExit
    && bmc.serviceConfig.Restart == "on-failure"
    && gpu.serviceConfig.Type == "notify"
    && gpu.serviceConfig.NotifyAccess == "main"
    && gpu.serviceConfig.Restart == "always"
    && gpu.serviceConfig.RestartSec == "5s"
    && lib.hasInfix "--config" gpu.serviceConfig.ExecStart
    && gpu.serviceConfig.WatchdogSec == "45s"
    && gpu.serviceConfig.TimeoutStopSec == "20s"
    && lib.elem "multi-user.target" gpu.wantedBy
    && !lib.hasInfix "nvmlDeviceSetDefaultFanSpeed" scriptText
    && !lib.hasInfix "nvmlDeviceSetFanSpeed" (builtins.replaceStrings [ "nvmlDeviceSetFanSpeed_v2" ] [ "" ] scriptText);
in
if !contract then throw "minas fan curve service contract changed" else
pkgs.runCommand "minas-fan-curve-tests" { nativeBuildInputs = [ pkgs.python3 ]; } ''
  work="$TMPDIR/minas-gpu-fans"
  mkdir -p "$work/scripts/tests"
  cp ${source} "$work/scripts/minas-gpu-fans.py"
  cp ${tests} "$work/scripts/tests/test_minas_gpu_fans.py"
  export MINAS_FAN_CONFIG=${testConfig}
  export PYTHONDONTWRITEBYTECODE=1
  output=$(python "$work/scripts/tests/test_minas_gpu_fans.py" 2>&1) || {
    echo "$output" >&2
    exit 1
  }
  echo "$output"
  echo "$output" | grep -Fq 'Ran 18 tests' || {
    echo 'VACUITY: expected 18 fan tests' >&2
    exit 1
  }
  touch "$out"
''
