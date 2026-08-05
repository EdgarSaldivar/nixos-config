# pelargir — Pi 5 hardware protection and local health monitoring.
{ pkgs, ... }:
{
  # The Pi 5 exposes its hardware watchdog through bcm2835_wdt. systemd must
  # pet it frequently enough to recover a hard hang, and the repeated health
  # check below fails loudly if no /dev/watchdog* device actually appeared.
  # Merely loading a module is not accepted as proof of protection.
  boot.kernelModules = [ "bcm2835_wdt" ];
  systemd.settings.Manager = {
    RuntimeWatchdogSec = "30s";
    RebootWatchdogSec = "10min";
  };

  # rasdaemon records the platform RAS tracepoints the kernel exposes. ARM does
  # not provide x86 MCEs, but losing the persistent hardware-error recorder on
  # this unattended control-plane appliance would still be a regression.
  hardware.rasdaemon.enable = true;

  # This host has one Kingston NV1 NVMe. It is DRAM-less and holds the entire
  # appliance, so let smartd discover and monitor its NVMe health/error log.
  services.smartd = {
    enable = true;
    autodetect = true;
  };

  # Hardware facts verified on the physical host on 2026-08-04:
  # - the Argon ONE V5 fan uses the Pi 5 native four-pin PWM header;
  # - it appears as hwmon `pwmfan` and thermal `cooling_device0` (pwm-fan);
  # - the kernel already controls it, so this module MUST NOT add fan control;
  # - measured speed was ~4955 RPM minimum and ~6880 RPM maximum;
  # - idle temperature was ~56-60 C and synthetic all-core load reached ~83 C;
  # - `vcgencmd get_throttled` remained 0x0 throughout;
  # - I2C bus 1 was empty, so there is no case controller at address 0x1a.
  # These facts are recorded so the inapplicable I2C daemon/module is not
  # re-investigated or added later.
  #
  # The Pi 5 also has an RTC, but its battery is optional and is not confirmed
  # present here. `dtparam=rtc_bbat_vchg` is deliberately NOT set: charging an
  # absent/unknown battery is unjustified. Confirm the battery chemistry and
  # presence before enabling it. k3s is already ordered after time-sync.target;
  # a wrong clock would invalidate TLS certificates and break cluster/API
  # authentication before time synchronisation repairs it.
  systemd.services.pelargir-hardware-health = {
    description = "Verify Pi watchdog, throttling, temperatures, and fan telemetry";
    after = [ "systemd-modules-load.service" ];
    serviceConfig = {
      Type = "oneshot";
      TimeoutStartSec = "30s";
    };
    script = ''
      status=0

      info() {
        ${pkgs.util-linux}/bin/logger --priority daemon.info \
          --tag pelargir-hardware-health -- "$*"
      }
      warn() {
        ${pkgs.util-linux}/bin/logger --priority daemon.warning \
          --tag pelargir-hardware-health -- "$*"
        status=1
      }

      # Runtime verification is load-bearing: systemd otherwise continues with
      # no watchdog if the driver fails to bind.
      if ! ${pkgs.coreutils}/bin/ls /dev/watchdog* >/dev/null 2>&1; then
        warn "NO /dev/watchdog device; RuntimeWatchdogSec is not protecting pelargir"
      fi

      if throttled=$(${pkgs.raspberrypi-utils}/bin/vcgencmd get_throttled 2>&1); then
        case "$throttled" in
          throttled=0x0) info "power/throttle status healthy: $throttled" ;;
          *) warn "UNDERVOLTAGE OR THROTTLING DETECTED: $throttled" ;;
        esac
      else
        warn "vcgencmd get_throttled failed: $throttled"
      fi

      cpu_temp="unknown"
      if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
        cpu_raw=$(${pkgs.coreutils}/bin/cat /sys/class/thermal/thermal_zone0/temp)
        case "$cpu_raw" in
          *[!0-9]*|"") warn "invalid CPU thermal reading: $cpu_raw" ;;
          *)
            cpu_temp="$((cpu_raw / 1000)).$(((cpu_raw % 1000) / 100))C"
            [ "$cpu_raw" -ge 80000 ] && warn "CPU temperature high: $cpu_temp"
            ;;
        esac
      else
        warn "CPU thermal zone is missing"
      fi

      fan_rpm="unavailable"
      for hwmon in /sys/class/hwmon/hwmon*; do
        [ -r "$hwmon/name" ] || continue
        [ "$(${pkgs.coreutils}/bin/cat "$hwmon/name")" = pwmfan ] || continue
        if [ -r "$hwmon/fan1_input" ]; then
          fan_rpm="$(${pkgs.coreutils}/bin/cat "$hwmon/fan1_input") RPM"
        else
          warn "pwmfan hwmon exists but fan1_input telemetry is missing"
        fi
      done
      [ "$fan_rpm" = unavailable ] && warn "pwmfan hwmon telemetry is missing"

      nvme_temps=""
      for hwmon in /sys/class/hwmon/hwmon*; do
        [ -r "$hwmon/name" ] || continue
        [ "$(${pkgs.coreutils}/bin/cat "$hwmon/name")" = nvme ] || continue
        [ -r "$hwmon/temp1_input" ] || continue
        nvme_raw=$(${pkgs.coreutils}/bin/cat "$hwmon/temp1_input")
        case "$nvme_raw" in
          *[!0-9]*|"") warn "invalid NVMe thermal reading: $nvme_raw" ;;
          *)
            nvme_temp="$((nvme_raw / 1000)).$(((nvme_raw % 1000) / 100))C"
            nvme_temps="$nvme_temps $nvme_temp"
            [ "$nvme_raw" -ge 70000 ] && warn "NVMe temperature high: $nvme_temp"
            ;;
        esac
      done

      info "telemetry: cpu=$cpu_temp fan=$fan_rpm nvme=$nvme_temps"
      exit "$status"
    '';
  };

  systemd.timers.pelargir-hardware-health = {
    description = "Check Pi hardware health every five minutes";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "5min";
      AccuracySec = "30s";
    };
  };
}
