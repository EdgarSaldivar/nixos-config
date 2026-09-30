{ pkgs, ... }:
let
  python = pkgs.python3.withPackages (pythonPackages: [ pythonPackages.pytest ]);
in
pkgs.runCommand "gluetun-watchdog-tests"
  {
    nativeBuildInputs = [ python ];
    testFile = "${../hosts/nixos/pelargir/tests/test_gluetun_watchdog.py}";
    watchdogScript = "${../hosts/nixos/pelargir/scripts/gluetun-watchdog.py}";
    expectedTests = "13";
  }
  ''
    set -euo pipefail
    export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
    export GLUETUN_WATCHDOG_SCRIPT="$watchdogScript"

    python -m pytest -p no:cacheprovider --collect-only -q "$testFile" > collected
    collected_tests=$(grep -c '::test_' collected || true)
    if [ "$collected_tests" -ne "$expectedTests" ]; then
      echo "VACUITY: collected $collected_tests of $expectedTests gluetun watchdog tests" >&2
      cat collected >&2
      exit 1
    fi

    python -m pytest -p no:cacheprovider -q --basetemp="$TMPDIR/pytest" "$testFile"
    touch "$out"
  ''
