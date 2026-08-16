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
      pkgs.sqlite
      pkgs.bats
      pkgs.gawk
      pkgs.gnused
      pkgs.bash
    ];
  }
  ''
    export BACKUP_SCRIPT=${backupScript}
    export HEARTBEAT_SCRIPT=${heartbeatScript}

    # ⛔ ANTI-VACUITY AND ANTI-DRIFT GATE.
    #
    # Two of the fixture groups REPRODUCE a decision rather than extracting it —
    # the backup-health state chain and the dump-promotion floor are re-expressed
    # in the bats file, because they are inline control flow rather than named
    # functions. A reproduced rule can silently stop describing the real one.
    #
    # So every constant and marker those fixtures assume is pinned here. If the
    # program changes its floor, renames a marker, or changes the staleness
    # threshold, this check fails loudly instead of leaving the suite green while
    # testing a rule that no longer exists.
    require() {
      grep -qF -e "$2" -- "$1" || { echo "MISSING from rendered program: $2" >&2; exit 1; }
    }

    # snapshot rotation + the awk/sqlite parsers are extracted, so presence is enough
    grep -q '^prune() {' "$BACKUP_SCRIPT" \
      || { echo "prune() not found in the rendered backup program" >&2; exit 1; }
    require "$HEARTBEAT_SCRIPT" 'inmce'
    require "$HEARTBEAT_SCRIPT" '0x2000000000000000'
    require "$HEARTBEAT_SCRIPT" '0x100000000000'

    # dump promotion: the size floor and the staged .tmp -> final shape
    require "$BACKUP_SCRIPT" '.sql.gz.tmp'
    require "$BACKUP_SCRIPT" '-gt 1024'

    # backup health: every marker the state-chain fixtures branch on
    require "$HEARTBEAT_SCRIPT" 'backup-root-data.retention-failed'
    require "$HEARTBEAT_SCRIPT" 'backup-root-data.degraded'
    require "$HEARTBEAT_SCRIPT" 'backup-root-data.failed'
    require "$HEARTBEAT_SCRIPT" 'backup-root-data.stamp'
    require "$HEARTBEAT_SCRIPT" '-gt 172800'
    require "$HEARTBEAT_SCRIPT" 'NEVER succeeded'

    bats ${suite}/backup.bats
    bats ${suite}/heartbeat.bats
    touch $out
  ''
