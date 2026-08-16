# Characterization tests for minas' two large embedded shell programs.
#
# `backup-root-data` is ~1,030 lines of shell and `healthcheck-ping` ~555, both
# living inside Nix string literals. Their BEHAVIOUR is proportionate to what they
# guard — nine live ZFS pool members, every database dump, the fleet heartbeat —
# but their PACKAGING has no test surface at all, and that asymmetry is how the
# MCE detection stayed wrong through three consecutive audit rounds while passing
# review each time.
#
# ⛔ These tests run against the RENDERED programs, taken from the evaluated NixOS
# configuration rather than from a copy. A test against a copy drifts silently
# from what is deployed, which for this program would be worse than no test.
#
# They deliberately land BEFORE the programs are extracted into
# writeShellApplication. Tests written after a refactor describe the refactor;
# tests written before it pin the behaviour the refactor must preserve.
{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  minas = nixosConfigurations.minas-tirith.config;

  backupScript = pkgs.writeText "backup-root-data.rendered.sh" minas.systemd.services.backup-root-data.script;
  heartbeatScript = pkgs.writeText "healthcheck-ping.rendered.sh" minas.systemd.services.healthcheck-ping.script;

  suite = ../hosts/nixos/minas-tirith/scripts/tests;
in
pkgs.runCommand "minas-shell-fixtures"
  {
    nativeBuildInputs = [
      pkgs.bats
      pkgs.gawk
      pkgs.gnused
      pkgs.bash
    ];
  }
  ''
    export BACKUP_SCRIPT=${backupScript}
    export HEARTBEAT_SCRIPT=${heartbeatScript}

    # Sanity: refuse to pass vacuously if the rendered programs stop containing the
    # code these fixtures target. Without this, a future refactor that renames
    # prune() would make every test skip its subject and still report success.
    grep -q '^prune() {' "$BACKUP_SCRIPT" \
      || { echo "prune() not found in the rendered backup program" >&2; exit 1; }
    grep -q 'inmce' "$HEARTBEAT_SCRIPT" \
      || { echo "MCE section parser not found in the rendered heartbeat" >&2; exit 1; }

    bats ${suite}/backup.bats
    bats ${suite}/heartbeat.bats
    touch $out
  ''
