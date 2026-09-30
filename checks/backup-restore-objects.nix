# Tests for the PinCollector restore tool that puts backed-up objects back into Garage with
# their recorded Content-Type and user metadata
# (hosts/nixos/minas-tirith/manifests/backup_restore_objects.py). It runs once, during a
# disaster, against the only copy of the photos: the metadata mapper must fail closed, and
# the verify step must catch a restore that lost metadata. Pure Python, no rclone: the push
# tests drive the mapper through a stand-in that speaks rclone's mapper protocol.
{ pkgs, ... }:
let
  python = pkgs.python3.withPackages (pythonPackages: [ pythonPackages.pytest ]);
in
pkgs.runCommand "backup-restore-objects-tests"
  {
    nativeBuildInputs = [ python ];
    testFile = "${../hosts/nixos/minas-tirith/tests/test_backup_restore_objects.py}";
    restoreScript = "${../hosts/nixos/minas-tirith/manifests/backup_restore_objects.py}";
    expectedTests = "48";
  }
  ''
    set -euo pipefail
    export PYTHONPYCACHEPREFIX="$TMPDIR/pycache"
    export BACKUP_RESTORE_OBJECTS_SCRIPT="$restoreScript"

    python -m pytest -p no:cacheprovider --collect-only -q "$testFile" > collected
    collected_tests=$(grep -c '::test_' collected || true)
    if [ "$collected_tests" -ne "$expectedTests" ]; then
      echo "VACUITY: collected $collected_tests of $expectedTests backup restore tests" >&2
      cat collected >&2
      exit 1
    fi

    python -m pytest -p no:cacheprovider -q --basetemp="$TMPDIR/pytest" "$testFile"
    touch "$out"
  ''
