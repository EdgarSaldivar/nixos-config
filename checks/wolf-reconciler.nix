{ lib, pkgs, nixosConfigurations, darwinConfigurations, ... }:
  let
    python = pkgs.python3.withPackages (pythonPackages: [
      pythonPackages.pytest
      pythonPackages.tomlkit
    ]);
  in
  pkgs.runCommand "wolf-reconciler-tests" { nativeBuildInputs = [ python ]; } ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
    # ⛔ Interpolate the WHOLE nardol directory, not just tests/. This looks
    # wasteful — any nardol edit busts this derivation's cache — but it is load
    # bearing: test_wolf_reconcile.py imports wolf-reconcile.py from the parent
    # directory, so narrowing this to `${../hosts/nixos/nardol/tests}` puts the
    # tests in the store WITHOUT the module they test and pytest fails at
    # collection with FileNotFoundError. Tried during the 2026-08-16 extraction;
    # reverted.
    python -m pytest -q ${../hosts/nixos/nardol}/tests
    touch "$out"
  ''
