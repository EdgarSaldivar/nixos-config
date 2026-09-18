# Quieter fans on nardol, programmed into the Super I/O chip rather than driven
# by a daemon.
#
# ⛔ THE BIOS CURVE SPENT 20 °C OF HEADROOM ON NOISE. Measured 2026-09-17 under
# three minutes of all-core load, stock curve against the one below:
#
#                      stock BIOS      this file
#   CPU fan, idle        ~2000 rpm       1136 rpm
#   CPU fan, loaded       2596 rpm       1626 rpm   (pwm 217 -> 117)
#   case fans             1040 rpm        500 rpm   (pwm 127 -> 60)
#   Tctl, loaded            69 °C          71 °C
#
# 40% off the CPU cooler and half off the case fans for two degrees, on a part
# whose Tjmax is 90 °C.
#
# ⛔ THE ROOT CAUSE WAS A TARGET THE CPU CAN NEVER REACH. ASRock ships
# pwm2_target_temp = 50 °C, and this chip reads the CPU over SMBus as Tctl,
# which IDLES at 51 °C on Zen 3. The controller was therefore permanently
# chasing a temperature that does not exist, with step_up_time at 400 ms, so
# every Tctl spike — and Zen 3 spikes constantly — was audible. Raising the
# target and slowing the steps is most of the win; the curve shape is the rest.
#
# ⛔ THIS IS NOT fancontrol AND DELIBERATELY SO. The values are written into the
# nct6798's own automatic-mode registers and pwm*_enable stays 5, so the CHIP
# enforces the curve. Nothing runs in userspace afterwards, and a hung kernel, a
# killed daemon or a failed deploy cannot leave the fans stuck — which is
# exactly what manual PWM control (pwm*_enable = 1) risks on a machine that
# suspends and wakes all day.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.nardol.fanCurve;

  # ⚠️ BY NAME, NEVER BY INDEX. hwmon numbering follows probe order, and this
  # chip landed on hwmon3 only because nvme and k10temp bound first. A reboot
  # that changes that order would silently write this curve into the wrong
  # device's registers.
  findHwmon = ''
    HW=""
    for h in /sys/class/hwmon/hwmon*; do
      if [ "$(cat "$h/name" 2>/dev/null)" = "${cfg.chip}" ]; then HW="$h"; break; fi
    done
    if [ -z "$HW" ]; then
      echo "nardol-fan-curve: ${cfg.chip} not found; leaving firmware fan control alone" >&2
      exit 0
    fi
  '';

  point = hw: n: i: t: p: ''
    echo ${toString (t * 1000)} > ${hw}/pwm${toString n}_auto_point${toString i}_temp
    echo ${toString p}          > ${hw}/pwm${toString n}_auto_point${toString i}_pwm
  '';

  applyCurve = n: c: ''
    ${point "$HW" n 1 (builtins.elemAt c.points 0).temp (builtins.elemAt c.points 0).pwm}
    ${point "$HW" n 2 (builtins.elemAt c.points 1).temp (builtins.elemAt c.points 1).pwm}
    ${point "$HW" n 3 (builtins.elemAt c.points 2).temp (builtins.elemAt c.points 2).pwm}
    ${point "$HW" n 4 (builtins.elemAt c.points 3).temp (builtins.elemAt c.points 3).pwm}
    echo ${toString cfg.stepUpMs}   > $HW/pwm${toString n}_step_up_time
    echo ${toString cfg.stepDownMs} > $HW/pwm${toString n}_step_down_time
  '';
in
{
  options.nardol.fanCurve = {
    enable = lib.mkEnableOption "a quieter hardware fan curve on the Super I/O chip";

    chip = lib.mkOption {
      type = lib.types.str;
      default = "nct6798";
      description = ''
        hwmon name of the Super I/O chip. The X570 Taichi carries an NCT6798D,
        and it binds without `acpi_enforce_resources=lax` — which many ASRock
        boards do require, so do not add that kernel parameter on a guess.
      '';
    };

    cpuFan = lib.mkOption {
      type = lib.types.attrs;
      default = {
        pwm = 2;
        points = [
          { temp = 55; pwm = 60; }
          { temp = 70; pwm = 110; }
          { temp = 80; pwm = 180; }
          { temp = 88; pwm = 255; }
        ];
        targetTemp = 75;
      };
      description = ''
        ⛔ THE LAST POINT IS THE SAFETY FLOOR, NOT A PREFERENCE. 88 °C with a
        Tjmax of 90 keeps the chip ramping to 100% before the CPU throttles, and
        that is what makes the quiet region below it defensible. Lower the first
        three points freely; do not raise the last one.

        Driven by the chip's SMBus reading of the CPU (Tctl). Measured under
        sustained all-core load this curve settles at 71 °C and pwm 117.
      '';
    };

    caseFans = lib.mkOption {
      type = lib.types.attrs;
      default = {
        pwms = [ 1 4 ];
        points = [
          { temp = 30; pwm = 60; }
          { temp = 42; pwm = 100; }
          { temp = 50; pwm = 170; }
          { temp = 60; pwm = 255; }
        ];
      };
      description = ''
        Driven by SYSTIN, which sat at 33-35 °C through every load measured here
        — GPU inference at 249 W, all-core CPU, and gaming. The stock curve
        started at 40 °C, so these fans never entered it and simply idled at
        pwm 127 forever. The first point is deliberately BELOW the resting
        SYSTIN so the fans are always on a defined part of the curve rather
        than on whatever the chip does underneath it.

        ⚠️ pwm 60 is ~23% and these fans still spin at ~500 rpm. A different fan
        may stall there; check `fan1_input` is non-zero after changing this.
      '';
    };

    stepUpMs = lib.mkOption {
      type = lib.types.int;
      default = 3000;
      description = ''
        ⛔ THE STOCK 400 ms IS WHY THE FANS SURGE. Zen 3 Tctl spikes several
        degrees constantly at idle, and at 400 ms the fan chases every spike.
        Slowing the ascent is the single most audible change in this file.
      '';
    };

    stepDownMs = lib.mkOption {
      type = lib.types.int;
      default = 6000;
      description = "Slower than the ascent, so the fan settles rather than oscillating.";
    };
  };

  config = lib.mkIf cfg.enable {
    # The driver is not autoloaded: nothing in the boot path probes ISA Super I/O.
    boot.kernelModules = [ "nct6775" ];

    systemd.services.nardol-fan-curve = {
      description = "Program a quieter fan curve into the ${cfg.chip}";
      after = [ "systemd-modules-load.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        set -eu
        ${findHwmon}
        ${applyCurve cfg.cpuFan.pwm cfg.cpuFan}
        echo ${toString (cfg.cpuFan.targetTemp * 1000)} > $HW/pwm${toString cfg.cpuFan.pwm}_target_temp
        ${lib.concatMapStringsSep "\n" (n: applyCurve n cfg.caseFans) cfg.caseFans.pwms}
        echo "nardol-fan-curve: applied to $HW" >&2
      '';
    };

    # ⚠️ THE CHIP KEEPS ITS REGISTERS ACROSS S3, BUT NOT ACROSS EVERY WAKE PATH,
    # and this host suspends whenever it is idle. Reapplying on resume costs one
    # oneshot and removes the question entirely.
    powerManagement.resumeCommands = ''
      ${pkgs.systemd}/bin/systemctl restart nardol-fan-curve.service || true
    '';
  };
}
