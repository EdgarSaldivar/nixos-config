{
  config,
  lib,
  pkgs,
  ...
}:
let
  scrutinyNotify = pkgs.writeShellApplication {
    name = "scrutiny-healthchecks-notify";
    runtimeInputs = with pkgs; [
      coreutils
      curl
      gnused
      util-linux
    ];
    text = ''
      set -eu

      state=/var/lib/scrutiny/alert.latched
      credential="''${CREDENTIALS_DIRECTORY:?}/healthchecks-url"

      # Scrutiny may notify for several disks concurrently. Serialise writes so
      # the latch is always a complete record, with the most recent alert last.
      exec 9>/var/lib/scrutiny/alert.lock
      flock 9
      tmp=$(mktemp /var/lib/scrutiny/.alert.latched.XXXXXX)
      trap 'rm -f "$tmp"' EXIT
      {
        printf '%s | %s\n' \
          "''${SCRUTINY_DATE:-unknown time}" \
          "''${SCRUTINY_SUBJECT:-Scrutiny disk alert}"
        printf 'host=%s device=%s serial=%s type=%s failure=%s\n' \
          "''${SCRUTINY_HOST_ID:-unknown}" \
          "''${SCRUTINY_DEVICE_NAME:-unknown}" \
          "''${SCRUTINY_DEVICE_SERIAL:-unknown}" \
          "''${SCRUTINY_DEVICE_TYPE:-unknown}" \
          "''${SCRUTINY_FAILURE_TYPE:-unknown}"
        printf '%s\n' "''${SCRUTINY_MESSAGE:-}"
      } > "$tmp"
      chmod 0600 "$tmp"
      mv "$tmp" "$state"
      trap - EXIT

      # Line two is Minas's dedicated critical channel. Retain monitoring.nix's
      # existing line-one fallback so installations without that optional
      # second check still receive the alert. The credential, never the store,
      # contains the secret Healthchecks URL.
      url=$(sed -n '2p' "$credential")
      if [ -z "$url" ]; then
        url=$(sed -n '1p' "$credential")
      fi
      if [ -z "$url" ]; then
        echo "WARNING: Healthchecks credential is empty; Scrutiny alert remains latched" >&2
      elif ! curl -fsS -m 20 --data-raw "CRITICAL: ''${SCRUTINY_SUBJECT:-Scrutiny disk alert}" "$url/fail" >/dev/null; then
        echo "WARNING: Scrutiny Healthchecks notification did not deliver; alert remains latched" >&2
      fi
    '';
  };
in
{
  services.scrutiny = {
    enable = true;
    influxdb.enable = true;
    openFirewall = false;
    settings = {
      web = {
        listen = {
          host = "0.0.0.0";
          port = 9080;
        };
        influxdb = {
          host = "127.0.0.1";
          port = 8086;
        };
      };
      # Scrutiny 0.9.2 otherwise repeats the same failure every hour. Alert on
      # the initial failure and on changed failing values; the dashboard and
      # persistent Healthchecks latch retain the unhealthy state.
      user.metrics.repeat_notifications = false;
      notify.urls = [ "script://${lib.getExe scrutinyNotify}" ];
    };

    # A plain scan reports the nine Adaptec-attached SATA devices as generic
    # SCSI. --scan-open verifies the transport and returns SAT explicitly,
    # while still auto-discovering all disks and the NVMe by stable identity.
    collector.settings.commands.metrics_scan_args = "--scan-open --json";
  };

  # InfluxDB is bundled by the Scrutiny module but has no tailnet or LAN
  # listener. Scrutiny is its only consumer.
  services.influxdb2.settings."http-bind-address" = "127.0.0.1:8086";

  # Scrutiny deliberately has no public Traefik route. The only firewall allow
  # for its wildcard listener is attached to the tailnet interface.
  networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ 9080 ];

  systemd.services.scrutiny.serviceConfig.LoadCredential = [
    "healthchecks-url:${config.sops.secrets.healthchecks-url.path}"
  ];
}
