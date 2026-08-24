{ pkgs, ... }:
let
  python = pkgs.python3.withPackages (pythonPackages: [
    pythonPackages.pytest
    pythonPackages.pyyaml
  ]);
in
pkgs.runCommand "k3s-reconcile-tests"
  {
    nativeBuildInputs = [ python ];
    testFile = "${../hosts/nixos/pelargir/tests/test_k3s_reconcile.py}";
    reconcileScript = "${../hosts/nixos/pelargir/scripts/k3s-reconcile.py}";
    expectedTests = "7";
  }
  ''
    set -euo pipefail
    export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
    export K3S_RECONCILE_SCRIPT="$reconcileScript"

    python -m pytest -p no:cacheprovider --collect-only -q "$testFile" > collected
    collected_tests=$(grep -c '::test_' collected || true)
    if [ "$collected_tests" -ne "$expectedTests" ]; then
      echo "VACUITY: collected $collected_tests of $expectedTests k3s reconcile tests" >&2
      cat collected >&2
      exit 1
    fi

    python -m pytest -p no:cacheprovider -q --basetemp="$TMPDIR/pytest" "$testFile"
    touch "$out"
  ''
