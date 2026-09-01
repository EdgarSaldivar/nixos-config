# pelargir — Pi 5 hardware protection and local/external health monitoring.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  ingressAcceptance = pkgs.callPackage ../minas-tirith/scripts/package.nix { };
  ingressBaseline = ../minas-tirith/baselines/minas-ingress-authentik-baseline-20260811T062828Z.txt;
  ingressHealthchecksSecret = config.sops.secrets."minas-ingress-healthchecks-url".path;

  # ⛔ The backends are DERIVED from the route catalog, never listed by hand.
  #
  # Same single-source-of-truth rule traefik-hostnames.nix follows: a list copied
  # into a second place works until someone edits one of them. Adding a route now
  # extends this coverage automatically, and a route pointing at a Service that
  # never gets endpoints fails here instead of in a user's browser.
  routeCatalog = import ../minas-tirith/traefik-routes/catalog.nix {
    pinCollectorRelease = import ../minas-tirith/pin-collector-release.nix;
  };
  # `serviceName or name` mirrors render.nix, which derives the backend DNS name the
  # same way. A disabled route publishes no router, so it has nothing to back.
  routeBackends = lib.mapAttrsToList (name: r: "${r.namespace}/${r.serviceName or name}") (
    lib.filterAttrs (_: r: r.enabled or true) routeCatalog.routes
  );
in
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

  # This must run from pelargir. It is at a different site/public IP from
  # minas, so public DNS and the internet route are part of every observation.
  # A local minas probe cannot detect the outage class this monitor covers.
  systemd.services.minas-ingress-external = {
    description = "Verify minas public ingress and wildcard certificate externally";
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "oneshot";
      TimeoutStartSec = "4min";
      StateDirectory = "minas-ingress-external";
      RuntimeDirectory = "minas-ingress-external";
      UMask = "0077";
    };
    script = ''
      set -u

      count_file="$STATE_DIRECTORY/consecutive-failures"
      # Keep the temporary count on the same filesystem so mv is atomic. A
      # /run -> /var/lib move silently degrades to copy+unlink across mounts.
      next_count_file="$STATE_DIRECTORY/.consecutive-failures.tmp"
      result_file="$RUNTIME_DIRECTORY/result"
      url="$(${pkgs.coreutils}/bin/head -n 1 ${ingressHealthchecksSecret} 2>/dev/null || true)"
      if [ -z "$url" ]; then
        ${pkgs.util-linux}/bin/logger --priority daemon.err \
          --tag minas-ingress-external -- "dedicated Healthchecks URL is missing or empty"
        exit 1
      fi

      # ⛔ AN HTTP STATUS IS NOT PROOF THE BACKEND EXISTS.
      #
      # Every Authentik-protected hostname answers 302 to auth.saldivar.io BEFORE
      # anything reaches its Service, so this probe reported `matched=69 drifted=0`
      # while books-dl.saldivar.io had NO backend at all: gluetun's readiness probe
      # had been failing for 38 hours, the Pod never went Ready, and an unready Pod
      # is removed from its Service's endpoints. The public status never changed, so
      # nothing alerted. Same blindness hid deluge-vpn for eight days.
      #
      # A Service with zero endpoints cannot serve its route. Assert that directly
      # against the cluster rather than inferring it from a status code that
      # Authentik answers first.
      backend_gaps=""
      for backend in ${lib.concatStringsSep " " routeBackends}; do
        ns="''${backend%%/*}"
        svc="''${backend#*/}"
        # jsonpath emits one `x` per ready address; empty output means no endpoints.
        # `get endpoints` failing (absent Service) is a gap too, not a pass.
        # ⛔ BOUND EVERY REQUEST. kubectl's default request timeout is UNLIMITED, so
        # one stalled API call would hang this unit until TimeoutStartSec killed it —
        # the HTTP probe would never run, the failure counter would never advance, and
        # a monitor that cannot finish is a monitor that stops alerting. That is the
        # exact failure mode this check was added to catch, so it must not introduce
        # it. 3s is generous against a local API server, and bounds the pathological
        # case to ~72s across the backends, leaving the probe room inside the unit's
        # 4min TimeoutStartSec.
        if ! ${config.services.k3s.package}/bin/kubectl --request-timeout=3s \
             -n "$ns" get endpoints "$svc" \
             -o jsonpath='{range .subsets[*].addresses[*]}x{end}' 2>/dev/null \
             | ${pkgs.gnugrep}/bin/grep -q x; then
          backend_gaps="$backend_gaps $ns/$svc"
        fi
      done

      : > "$result_file"
      if [ -n "$backend_gaps" ]; then
        ${pkgs.coreutils}/bin/printf 'BACKEND GAPS (Service has no endpoints):%s\n' \
          "$backend_gaps" >>"$result_file"
      fi

      # The HTTP probe runs either way: its output is wanted in the report even when
      # a backend gap has already decided the verdict.
      if ${ingressAcceptance}/bin/ingress-acceptance \
        --baseline ${ingressBaseline} \
        --public-dns \
        --monitor-certificate \
        --timeout 5 \
        >>"$result_file" 2>&1 && [ -z "$backend_gaps" ]
      then
        ${pkgs.coreutils}/bin/printf '0\n' >"$next_count_file"
        ${pkgs.coreutils}/bin/mv "$next_count_file" "$count_file"
        summary="$(${pkgs.coreutils}/bin/tail -n 1 "$result_file")"
        ${pkgs.util-linux}/bin/logger --priority daemon.info \
          --tag minas-ingress-external -- "healthy: $summary"
        ${pkgs.curl}/bin/curl -fsS -m 20 "$url" >/dev/null \
          || ${pkgs.util-linux}/bin/logger --priority daemon.warning \
            --tag minas-ingress-external -- "healthy, but success ping did not deliver"
        exit 0
      fi

      previous=0
      if [ -r "$count_file" ]; then
        read -r previous <"$count_file" || previous=0
      fi
      case "$previous" in
        *[!0-9]*|"") previous=0 ;;
      esac
      failures="$((previous + 1))"
      ${pkgs.coreutils}/bin/printf '%s\n' "$failures" >"$next_count_file"
      ${pkgs.coreutils}/bin/mv "$next_count_file" "$count_file"
      summary="$(${pkgs.coreutils}/bin/tail -n 1 "$result_file")"
      # `tail -1` is the HTTP probe's line, which is exactly the signal that stayed
      # green through both outages. Lead with the gap so the alert names the cause.
      if [ -n "$backend_gaps" ]; then
        summary="no endpoints:$backend_gaps | $summary"
      fi

      if [ "$failures" -lt 3 ]; then
        ${pkgs.util-linux}/bin/logger --priority daemon.warning \
          --tag minas-ingress-external \
          -- "transient failure $failures/3 (not paging): $summary"
        # Keep the dedicated check from becoming late while the local counter
        # deliberately suppresses these first two failures. This does not reset
        # the counter; a third bad run still transitions the check via /fail.
        ${pkgs.curl}/bin/curl -fsS -m 20 \
          --data-raw "DEGRADED: transient ingress failure $failures/3: $summary" \
          "$url" >/dev/null \
          || ${pkgs.util-linux}/bin/logger --priority daemon.warning \
            --tag minas-ingress-external -- "transient ping did not deliver"
        exit 0
      fi

      # Healthchecks receives a bounded diagnostic: a one-line verdict plus at
      # most 7 KiB of the detailed table. The private runtime copy exists only
      # for this invocation; RuntimeDirectory is removed when the unit stops.
      {
        ${pkgs.coreutils}/bin/printf \
          'UNHEALTHY: minas ingress failed %s consecutive checks: %s\n\n' \
          "$failures" "$summary"
        ${pkgs.coreutils}/bin/head -c 7168 "$result_file"
      } | ${pkgs.curl}/bin/curl -fsS -m 20 --data-binary @- "$url/fail" >/dev/null \
        || ${pkgs.util-linux}/bin/logger --priority daemon.err \
          --tag minas-ingress-external -- "failure ping did not deliver"
      exit 1
    '';
  };

  systemd.timers.minas-ingress-external = {
    description = "Check minas public ingress every five minutes";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "5min";
      AccuracySec = "30s";
    };
  };
}
