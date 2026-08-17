# The systemd units that wrap minas' two large shell programs, pinned as text.
#
# WHY THIS EXISTS, AND WHY IT IS NOT A FIXTURE TEST
#
# `minas-shell-fixtures` exercises what the programs DO. This check pins how they
# are WRAPPED — ExecStart, Environment, PATH, credentials, timeouts, scheduling.
# Those are exactly the properties that the extraction into `writeShellApplication`
# can change silently, because:
#
#   * every command in `backup-root-data` is an interpolated store path, so moving
#     to a real `.sh` means bare names resolved through PATH. The rendered body
#     necessarily changes, which means byte-equality of the SCRIPT stops being
#     available as evidence. The UNIT is what remains gatable.
#   * `writeShellApplication` supplies its own shebang, `set -euo pipefail`, and a
#     `runtimeInputs` PATH prefix, while `healthcheck-ping` currently relies on
#     `systemd.services.healthcheck-ping.path` as its ONLY PATH. Command resolution
#     can therefore change without a single line of the program changing.
#
# A refactor that preserves behaviour must leave these units identical apart from
# the ExecStart store path. If it does not, that is the thing to look at first.
#
# NORMALISATION — deliberately narrow
#
# Store hashes and versions are stripped, so a nixpkgs bump does not fail this
# check. Package IDENTITY and ORDER are kept, because PATH order decides which
# `grep` wins. ExecStart's store path becomes a placeholder, since it is the one
# thing the extraction is expected to change.
#
# A failure after a nixpkgs bump that legitimately adds a directive is fixed by
# updating the expected text DELIBERATELY, in a commit that says so — not by
# loosening the comparison.
{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:
let
  minas = nixosConfigurations.minas-tirith.config;

  # ⛔ `unsafeDiscardStringContext` is load-bearing, not a shortcut.
  #
  # The unit text contains `ExecStart=/nix/store/...-unit-script-<name>-start`. That
  # is a string with CONTEXT, so writing it into a file makes the unit-script a
  # build dependency -- and these are x86_64-linux scripts, which an aarch64-darwin
  # evaluator cannot build. The check would then fail on the Mac for a reason that
  # has nothing to do with the units.
  #
  # This check reads the units as TEXT. It never executes them, and the ExecStart
  # path is normalised away immediately below, so the dependency is genuinely not
  # wanted. Discarding context is what makes the check evaluate identically on both
  # platforms, which is the point of the two-platform CI matrix.
  unitText = name: builtins.unsafeDiscardStringContext minas.systemd.units."${name}.service".text;

  # 2026-08-16: `backup-root-data`'s PATH gained eleven packages. That is the
  # extraction, and it is the ONE line this check was built to make visible.
  #
  # Before, every command was an interpolated absolute store path and PATH was the
  # NixOS default. Now the program is a real `.sh` with bare names and those eleven
  # packages resolve them. Nothing else about either unit moved -- ExecStart, the
  # other Environment lines, Type, Nice, IOSchedulingClass, TimeoutStartSec,
  # OnFailure and Description were byte-identical across the change, which is what
  # says the extraction touched packaging and not behaviour.
  #
  # 2026-08-16, second half: `healthcheck-ping`'s PATH gained rasdaemon, sqlite and
  # systemd. Those three were interpolated as store paths inside the program and so
  # never needed to be on PATH; the extracted program uses bare names and does.
  #
  # This is the single most dangerous line in the extraction. The comment beside
  # `gawk` in monitoring.nix records what happens when a package is missing here:
  # `awk: command not found` on stderr, the unit still exits 0, the heartbeat still
  # pings OK, and the check silently stops checking. A missing rasdaemon, sqlite or
  # systemd would fail the same way -- invisibly, on a monitoring program.
  #
  # `coreutils` sits ahead of `util-linux` here, and that ordering is load-bearing:
  # both ship binaries, and the eleven are ordered so the package a command was
  # previously hard-coded to is the one that wins. See minas-command-resolution.
  expected = pkgs.writeText "minas-unit-contract.expected" ''
    ===== backup-root-data.service =====
    [Unit]
    Description=Back up mutable service data from root to ZFS storage2
    OnFailure=notify-failure@backup-root-data.service

    [Service]
    Environment="LOCALE_ARCHIVE=glibc-locales/lib/locale/locale-archive"
    Environment="PATH=age/bin:coreutils/bin:docker/bin:gnugrep/bin:gnused/bin:gzip/bin:k3s/bin:rsync/bin:sqlite/bin:util-linux/bin:zfs-user/bin:coreutils/bin:findutils/bin:gnugrep/bin:gnused/bin:systemd/bin:age/sbin:coreutils/sbin:docker/sbin:gnugrep/sbin:gnused/sbin:gzip/sbin:k3s/sbin:rsync/sbin:sqlite/sbin:util-linux/sbin:zfs-user/sbin:coreutils/sbin:findutils/sbin:gnugrep/sbin:gnused/sbin:systemd/sbin"
    Environment="TZDIR=tzdata/share/zoneinfo"
    ExecStart=@SCRIPT@
    IOSchedulingClass=idle
    Nice=10
    TimeoutStartSec=6h
    Type=oneshot
    ===== healthcheck-ping.service =====
    [Unit]
    Description=Health-gated heartbeat to healthchecks.io

    [Service]
    Environment="LOCALE_ARCHIVE=glibc-locales/lib/locale/locale-archive"
    Environment="PATH=curl/bin:util-linux/bin:zfs-user/bin:docker/bin:k3s/bin:smartmontools/bin:gnugrep/bin:gawk/bin:coreutils/bin:rasdaemon/bin:sqlite/bin:systemd/bin:coreutils/bin:findutils/bin:gnugrep/bin:gnused/bin:systemd/bin:curl/sbin:util-linux/sbin:zfs-user/sbin:docker/sbin:k3s/sbin:smartmontools/sbin:gnugrep/sbin:gawk/sbin:coreutils/sbin:rasdaemon/sbin:sqlite/sbin:systemd/sbin:coreutils/sbin:findutils/sbin:gnugrep/sbin:gnused/sbin:systemd/sbin"
    Environment="TZDIR=tzdata/share/zoneinfo"
    ExecStart=@SCRIPT@
    StateDirectory=healthcheck-ping
    TimeoutStartSec=60s
    Type=oneshot
  '';

  actualRaw = pkgs.writeText "minas-unit-contract.actual-raw" ''
    ===== backup-root-data.service =====
    ${unitText "backup-root-data"}===== healthcheck-ping.service =====
    ${unitText "healthcheck-ping"}'';
in
pkgs.runCommand "minas-unit-contract" { nativeBuildInputs = [ pkgs.gnused ]; } ''
  # 1. ExecStart's store path is the one thing extraction is EXPECTED to change.
  # 2. Then strip store hash + version, keeping package identity and order.
  # ⚠️ The package-name pattern is written the long way ON PURPOSE. The obvious
  # `([^/]*?)-[0-9]...` does not work: POSIX ERE has NO lazy quantifiers, so `*?`
  # matches greedily and `glibc-locales-2.42-67` normalises to `glibc-locales-2.42`
  # instead of `glibc-locales` -- a version leaking into the contract, which would
  # then fail on the next nixpkgs bump for no real reason.
  #
  # Instead: a name is one or more `-`-joined components that START WITH A LETTER.
  # The first digit-leading component begins the version, and everything from there
  # is discarded. Handles coreutils-9.11, util-linux-2.42.2-bin, k3s-1.35.6+k3s1,
  # zfs-user-2.4.3, glibc-locales-2.42-67 and tzdata-2026c alike.
  sed -E \
    -e 's#ExecStart=/nix/store/[a-z0-9]{32}-[^[:space:]]*#ExecStart=@SCRIPT@#' \
    -e 's#/nix/store/[a-z0-9]{32}-([a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z][a-zA-Z0-9]*)*)[^/]*#\1#g' \
    ${actualRaw} | sed -E 's/[[:space:]]+$//' > actual.txt

  sed -E 's/[[:space:]]+$//' ${expected} > expected.txt

  # ANTI-VACUITY: if normalisation ate everything, or the units evaluated empty,
  # a diff of two near-empty files would pass and prove nothing.
  lines=$(grep -c . actual.txt || true)
  if [ "$lines" -lt 20 ]; then
    echo "VACUITY: normalised unit text is only $lines lines -- normalisation or evaluation is broken." >&2
    cat actual.txt >&2
    exit 1
  fi
  if ! grep -q 'ExecStart=@SCRIPT@' actual.txt; then
    echo "VACUITY: no ExecStart was normalised -- the sed pattern no longer matches." >&2
    exit 1
  fi
  # Store paths must be fully normalised away, or a nixpkgs bump would fail this
  # check for a reason that has nothing to do with the units.
  if grep -q '/nix/store/' actual.txt; then
    echo "VACUITY: store paths survived normalisation:" >&2
    grep -n '/nix/store/' actual.txt >&2
    exit 1
  fi

  if ! diff -u expected.txt actual.txt; then
    echo "" >&2
    echo "The systemd units wrapping minas' shell programs changed." >&2
    echo "If this is an intended part of the writeShellApplication extraction," >&2
    echo "update the expected text above in the SAME commit and say why." >&2
    exit 1
  fi

  touch $out
''
