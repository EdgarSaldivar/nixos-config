# Every command in the extracted shell programs must resolve to the package it was
# hard-coded to before the extraction.
#
# THE FAILURE THIS EXISTS TO CATCH
#
# Until 2026-08-16 these programs invoked absolute store paths: there was no
# resolution step and no way to get the wrong binary. They are now real `.sh` files
# with bare names, resolved through `systemd.services.<name>.path`. That trades
# unreadable programs for programs with a NEW failure mode -- PATH order and PATH
# membership.
#
# This is not hypothetical on this host. The comment beside `gawk` in monitoring.nix
# records the exact failure: the package was missing from `path`, the program printed
# `awk: command not found`, the unit still exited 0, the heartbeat still pinged OK,
# and that check silently stopped checking. A monitoring program that fails open is
# worse than no monitoring program.
#
# The extraction ADDED that risk to three more commands on the heartbeat --
# ras-mc-ctl, sqlite3, systemctl -- plus the entire backup set, so this check grew
# with it.
#
# TWO TIERS, because the packages are x86_64-linux
#
#   * everywhere: textual invariants -- the package set is exactly what is expected,
#     each mapping is truthful against the program's CODE rather than its prose, and
#     no program has drifted back toward embedded store paths.
#   * x86_64-linux only: REAL resolution. Build the PATH and ask which directory
#     actually wins. This is a use for the Linux CI leg beyond mere parity.
#
# The mappings are contracts, extracted mechanically from the interpolations that
# existed before the change -- 83 in the backup, 14 in the heartbeat -- not written
# from memory.
{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  minas = nixosConfigurations.minas-tirith.config;

  # ⚠️ `systemd.services.<n>.path` is the MERGED list, not the one written in the
  # module: NixOS appends these five to every service. Compared as a SET, because
  # multiplicity is an artefact of that merge; ORDER is what decides resolution and
  # `minas-unit-contract` pins it exactly.
  nixosServiceDefaults = [
    "coreutils"
    "findutils"
    "gnugrep"
    "gnused"
    "systemd"
  ];

  targets = {
    backup-root-data = {
      program = ../hosts/nixos/minas-tirith/scripts/backup-root-data.sh;
      owners = {
        age = "age";
        basename = "coreutils";
        chown = "coreutils";
        dirname = "coreutils";
        install = "coreutils";
        stat = "coreutils";
        docker = "docker";
        grep = "gnugrep";
        sed = "gnused";
        gzip = "gzip";
        k3s = "k3s";
        rsync = "rsync";
        sqlite3 = "sqlite";
        findmnt = "util-linux";
        flock = "util-linux";
        setpriv = "util-linux";
        zfs = "zfs";
      };
      alsoOnPath = [ ];
    };

    healthcheck-ping = {
      program = ../hosts/nixos/minas-tirith/scripts/healthcheck-ping.sh;
      owners = {
        awk = "gawk";
        grep = "gnugrep";
        ras-mc-ctl = "rasdaemon";
        smartctl = "smartmontools";
        sqlite3 = "sqlite";
        systemctl = "systemd";
      };
      # ⚠️ These were ALREADY bare names before the extraction -- the heartbeat has
      # always resolved them through `path`. They are therefore not part of the
      # equivalence claim (there is no prior store path to compare against), but they
      # ARE part of the package set, and dropping one still breaks the program in the
      # fails-open way described above. Listed so the set comparison stays exact.
      alsoOnPath = [
        "curl"
        "util-linux"
        "zfs"
        "docker"
        "k3s"
        "coreutils"
      ];
    };
  };

  realResolution = pkgs.stdenv.hostPlatform.system == "x86_64-linux";

  sortStr = lib.sort (a: b: a < b);

  mkExpected = t: lib.unique (lib.attrValues t.owners ++ t.alsoOnPath ++ nixosServiceDefaults);

  mkActual =
    name: lib.unique (map (p: p.pname or (lib.getName p)) minas.systemd.services.${name}.path);

  perTarget = lib.mapAttrsToList (name: t: ''
    echo "=== ${name} ==="

    # ---- 1. the program must stay extracted --------------------------------
    if grep -q '/nix/store/' ${t.program}; then
      echo "FAIL: embedded store paths reappeared in ${name}:" >&2
      grep -n '/nix/store/' ${t.program} >&2
      fail=1
    fi

    # ---- 2. the mapping must be truthful, against CODE not prose -----------
    # Review pointed out that a bare regex is satisfied by a mention in a comment,
    # and these programs are heavily commented -- so "the mapping is truthful"
    # would have been close to unfalsifiable. Strip comments first.
    sed -E 's/(^|[[:space:]])#.*$/\1/' ${t.program} > ${name}-code.sh
    for pair in ${lib.concatStringsSep " " (lib.mapAttrsToList (c: o: "${c}:${o}") t.owners)}; do
      c="''${pair%%:*}"
      if ! grep -qE "(^|[^A-Za-z0-9_./-])$c([^A-Za-z0-9_-]|$)" ${name}-code.sh; then
        echo "FAIL: ${name} mapping claims '$c' is used, but it is not in the code." >&2
        fail=1
      fi
    done

    # ---- 3. the package set must be exactly the expected one ---------------
    expected="${lib.concatStringsSep " " (sortStr (mkExpected t))}"
    actual="${lib.concatStringsSep " " (sortStr (mkActual name))}"
    if [ "$expected" != "$actual" ]; then
      echo "FAIL: ${name} path package set changed." >&2
      echo "  expected: $expected" >&2
      echo "  actual  : $actual" >&2
      fail=1
    fi

    # ---- 4. REAL resolution, where the packages can be built ---------------
    ${
      if realResolution then
        ''
          for pair in ${lib.concatStringsSep " " (lib.mapAttrsToList (c: o: "${c}:${o}") t.owners)}; do
            c="''${pair%%:*}"; want="''${pair##*:}"
            got="$(PATH="${
              lib.makeBinPath minas.systemd.services.${name}.path
            }" command -v "$c" 2>/dev/null || true)"
            if [ -z "$got" ]; then
              echo "FAIL: ${name}: '$c' does not resolve on the unit PATH at all." >&2
              fail=1
              continue
            fi
            owner="$(echo "$got" | sed -E 's#^/nix/store/[a-z0-9]{32}-##; s#/bin/.*##; s#-[0-9].*$##')"
            if [ "$owner" != "$want" ] && [ "$owner" != "$want-user" ]; then
              echo "FAIL: ${name}: '$c' resolves to '$owner', expected '$want' ($got)" >&2
              fail=1
            fi
          done
          echo "  real resolution checked"
        ''
      else
        ''
          echo "  real resolution skipped: packages are x86_64-linux, this is ${pkgs.stdenv.hostPlatform.system}"
        ''
    }
    checked=$((checked + 1))
  '') targets;
in
pkgs.runCommand "minas-command-resolution" { nativeBuildInputs = [ pkgs.gnused ]; } ''
  fail=0
  checked=0

  ${lib.concatStringsSep "\n" perTarget}

  # ANTI-VACUITY: the per-target blocks are generated at eval time, so an empty or
  # mis-built `targets` set would produce a script that checks nothing and exits 0.
  if [ "$checked" -ne ${toString (lib.length (lib.attrNames targets))} ]; then
    echo "VACUITY: expected ${toString (lib.length (lib.attrNames targets))} targets, checked $checked" >&2
    exit 1
  fi

  [ "$fail" -eq 0 ] || exit 1
  touch $out
''
