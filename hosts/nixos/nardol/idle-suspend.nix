# Put nardol to sleep when nobody is using it.
#
# The machine exists to be woken on demand (see hosts/darwin/dol-amroth/amon-din.nix),
# so the power saving only materialises if it also puts itself back. S3 was
# proven on this hardware before any of this was written: 20/20 suspend/resume
# cycles, 6-8s resumes, zero Xid errors, NVENC verified afterwards.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  # Wall-clock idle required before sleeping, as a count of consecutive clean
  # polls. Generous on purpose: the cost of sleeping too eagerly is a user
  # staring at a dead Moonlight connection, while the cost of sleeping late is a
  # few watts.
  intervalSeconds = 60;
  idleChecksRequired = 15; # ~15 minutes

  idleCheck = pkgs.writeShellScript "nardol-idle-check" ''
    set -euo pipefail

    STATE=/run/nardol-idle-count
    WOLF_SOCK=/run/wolf/wolf.sock

    # ⛔ EVERY UNCERTAIN ANSWER MUST MEAN "STAY AWAKE".
    #
    # This function returns a reason string when the host is busy and nothing
    # when it is idle. Any probe that errors, times out, or returns something
    # unparseable returns a reason — never silence. Suspending a machine because
    # a query failed is the one outcome that loses a user's session, and it is
    # the failure mode a naive implementation has by default.
    busy_reason() {
      # 1. Wolf sessions — authoritative. Wolf's own API knows what it is
      #    streaming; container and port heuristics only approximate it.
      if [ -S "$WOLF_SOCK" ]; then
        local body
        if ! body=$(${pkgs.curl}/bin/curl -s --max-time 5 --unix-socket "$WOLF_SOCK" \
                      http://localhost/api/v1/sessions 2>/dev/null); then
          echo "wolf-api-unreachable"; return
        fi
        if ! printf '%s' "$body" | ${pkgs.jq}/bin/jq -e '.sessions' >/dev/null 2>&1; then
          echo "wolf-api-unparseable"; return
        fi
        if [ "$(printf '%s' "$body" | ${pkgs.jq}/bin/jq '.sessions | length')" != "0" ]; then
          echo "wolf-session-active"; return
        fi
        # 2. Lobbies outlive an individual session; a host with a live lobby is
        #    expecting someone back.
        if body=$(${pkgs.curl}/bin/curl -s --max-time 5 --unix-socket "$WOLF_SOCK" \
                    http://localhost/api/v1/lobbies 2>/dev/null); then
          if [ "$(printf '%s' "$body" | ${pkgs.jq}/bin/jq '.lobbies // [] | length' 2>/dev/null || echo 1)" != "0" ]; then
            echo "wolf-lobby-active"; return
          fi
        else
          echo "wolf-lobby-query-failed"; return
        fi
      elif ${pkgs.systemd}/bin/systemctl is-active --quiet docker-wolf.service; then
        # Wolf claims to be running but its socket is missing: unknown state.
        echo "wolf-socket-missing"; return
      fi
      # Wolf deliberately stopped is a legitimately idle host, so no reason here.

      # 3. Running child containers. Wolf sets WOLF_STOP_CONTAINER_ON_EXIT=TRUE,
      #    so a RUNNING child means a live app. Corroborates the API rather than
      #    replacing it — belt and braces across a Wolf upgrade that changes the
      #    session model.
      local running
      if ! running=$(${pkgs.docker}/bin/docker ps --format '{{.Names}}' 2>/dev/null); then
        echo "docker-unreachable"; return
      fi
      if printf '%s' "$running" | ${pkgs.gnugrep}/bin/grep -qvE '^(wolf)?$'; then
        echo "wolf-child-container-running"; return
      fi

      # 4. Somebody is logged in. Treated as a blocker rather than a grace
      #    signal: suspending mid-deploy or mid-debug is hostile, and an idle SSH
      #    session costs only that nobody is asleep while an operator is present.
      if ${pkgs.systemd}/bin/loginctl list-sessions --no-legend 2>/dev/null | ${pkgs.gnugrep}/bin/grep -q .; then
        echo "login-session-present"; return
      fi

      # 5. Anything holding a systemd sleep inhibitor, including a future
      #    inference job. This is the extension point for Phase 3.
      if ${pkgs.systemd}/bin/systemd-inhibit --list --no-legend 2>/dev/null \
           | ${pkgs.gnugrep}/bin/grep -qE 'sleep|idle'; then
        echo "sleep-inhibitor-held"; return
      fi
    }

    reason="$(busy_reason || echo "idle-check-failed")"

    if [ -n "$reason" ]; then
      [ -f "$STATE" ] && rm -f "$STATE"
      echo "busy: $reason"
      exit 0
    fi

    count=0
    [ -f "$STATE" ] && count=$(${pkgs.coreutils}/bin/cat "$STATE" 2>/dev/null || echo 0)
    count=$((count + 1))
    printf '%s' "$count" > "$STATE"

    if [ "$count" -lt ${toString idleChecksRequired} ]; then
      echo "idle $count/${toString idleChecksRequired}"
      exit 0
    fi

    # ⛔ RE-CHECK IMMEDIATELY BEFORE SLEEPING.
    #
    # The poll that incremented the counter is up to ''${intervalSeconds}s old. A
    # session starting in that window would otherwise be suspended out from under
    # the user. This does not close the race completely — nothing short of an
    # inhibitor held by Wolf itself would — but it narrows it from a minute to
    # milliseconds, and the recovery is a 12s `amon-din`.
    reason="$(busy_reason || echo "idle-check-failed")"
    if [ -n "$reason" ]; then
      rm -f "$STATE"
      echo "aborted at the last moment: $reason"
      exit 0
    fi

    rm -f "$STATE"
    echo "idle for ~$(( ${toString idleChecksRequired} * ${toString intervalSeconds} / 60 )) minutes; suspending"
    exec ${pkgs.systemd}/bin/systemctl suspend
  '';
in
{
  systemd.services.nardol-idle-suspend = {
    description = "Suspend Nardol when no session, container, login or inhibitor is active";
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${idleCheck}";
      # flock serialises against a second timer firing while the first is
      # deciding, which would let two runs each see a stale counter.
      ExecCondition = "${pkgs.util-linux}/bin/flock -n /run/nardol-idle.lock true";
    };
  };

  systemd.timers.nardol-idle-suspend = {
    description = "Poll Nardol for idleness";
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "5min";
      OnUnitActiveSec = "${toString intervalSeconds}s";
      # Do not let a missed poll fire a burst of catch-up runs straight after a
      # resume, which is exactly when the host is least likely to be idle.
      Persistent = false;
      AccuracySec = "10s";
    };
  };

  # The counter lives in /run so a reboot or a resume starts the clock again
  # rather than inheriting a stale count from before.
  systemd.tmpfiles.rules = [ "f /run/nardol-idle-count 0644 root root -" ];
}
