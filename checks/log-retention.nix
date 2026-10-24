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
      journald = cfg.environment.etc."systemd/journald.conf".text;
      service = cfg.systemd.services.pod-log-retention or { };
      maxAge = cfg.fleet.logRetention.maxAgeDays;
    in
    !cfg.fleet.logRetention.enable
    || maxAge > promiseDays
    # journald reads the LAST assignment of a key, so exactly one of each.
    || lib.length (lib.filter (lib.hasPrefix "MaxRetentionSec=") (lib.splitString "\n" journald)) != 1
    || lib.length (lib.filter (lib.hasPrefix "MaxFileSec=") (lib.splitString "\n" journald)) != 1
    || !lib.hasInfix "MaxRetentionSec=${toString maxAge}day\n" journald
    || !lib.hasInfix "MaxFileSec=1day\n" journald
    || service == { }
    || lib.toInt service.environment.POD_LOG_MAX_AGE_SECONDS >= maxAge * 86400
    || !lib.elem "timers.target" (cfg.systemd.timers.pod-log-retention.wantedBy or [ ])
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
      expectedTests = "10";
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
