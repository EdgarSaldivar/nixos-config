{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# PinCollector's privacy page promises that server logs are kept for 30 days.
# These are the hosts holding its logs: minas-tirith runs its Pods and the public
# traefik that writes its access log; pelargir runs its backup CronJob. A host
# dropping out of this list, or a store losing its cap, would break that promise
# while every unit still reports active.
let
  hosts = [
    "minas-tirith"
    "pelargir"
  ];
  promiseDays = 30;

  broken = lib.filter (
    name:
    let
      cfg = nixosConfigurations.${name}.config;
      maxAge = cfg.fleet.logRetention.maxAgeDays;
      # journald strips whitespace and reads the LAST assignment of a key, so the
      # contract is on stripped lines: exactly one of each, with the right value.
      journald = map lib.trim (lib.splitString "\n" cfg.environment.etc."systemd/journald.conf".text);
      assignments = key: lib.filter (lib.hasPrefix "${key}=") journald;
      # A drop-in is read after journald.conf and would override it unseen here.
      dropIns = lib.filter (lib.hasPrefix "systemd/journald.conf.d/") (lib.attrNames cfg.environment.etc);
      prune = cfg.systemd.services.pod-log-retention or { };
      pruneTimer = cfg.systemd.timers.pod-log-retention or { };
      rotate = cfg.systemd.services.journal-rotate or { };
      rotateTimer = cfg.systemd.timers.journal-rotate or { };
    in
    !cfg.fleet.logRetention.enable
    || maxAge > promiseDays
    || assignments "MaxRetentionSec" != [ "MaxRetentionSec=${toString maxAge}day" ]
    || assignments "MaxFileSec" != [ "MaxFileSec=1day" ]
    || dropIns != [ ]
    # MaxFileSec never archives an IDLE journal; only a full rotation does.
    || !lib.hasSuffix "/bin/journalctl --rotate" (rotate.serviceConfig.ExecStart or "")
    || rotateTimer.timerConfig.OnCalendar or null != "daily"
    || !lib.elem "timers.target" (rotateTimer.wantedBy or [ ])
    || prune.environment.POD_LOG_ROOT or null != "/var/log/pods"
    || lib.toInt (prune.environment.POD_LOG_MAX_AGE_SECONDS or "0") <= 0
    # One day under the promise, so an hourly run can be late and still keep it.
    || lib.toInt (prune.environment.POD_LOG_MAX_AGE_SECONDS or "0") > (maxAge - 1) * 86400
    || !lib.hasInfix "pod-log-retention.py" (prune.serviceConfig.ExecStart or "")
    || pruneTimer.timerConfig.OnCalendar or null != "hourly"
    || !lib.elem "timers.target" (pruneTimer.wantedBy or [ ])
  ) hosts;

  python = pkgs.python3.withPackages (ps: [ ps.pytest ]);
in
if broken != [ ] then
  throw "log-retention: ${lib.concatStringsSep ", " broken} no longer caps journald and /var/log/pods under ${toString promiseDays} days (fleet.logRetention)"
else
  pkgs.runCommand "log-retention-tests"
    {
      nativeBuildInputs = [ python ];
      testFile = "${../modules/nixos/fleet/tests/test_pod_log_retention.py}";
      retentionScript = "${../modules/nixos/fleet/scripts/pod-log-retention.py}";
      expectedTests = "15";
    }
    ''
      set -euo pipefail
      export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
      export POD_LOG_RETENTION_SCRIPT="$retentionScript"

      python -m pytest -p no:cacheprovider --collect-only -q "$testFile" > collected
      collected_tests=$(grep -c '::test_' collected || true)
      if [ "$collected_tests" -ne "$expectedTests" ]; then
        echo "VACUITY: collected $collected_tests of $expectedTests pod log retention tests" >&2
        cat collected >&2
        exit 1
      fi

      python -m pytest -p no:cacheprovider -q --basetemp="$TMPDIR/pytest" "$testFile"
      touch "$out"
    ''
