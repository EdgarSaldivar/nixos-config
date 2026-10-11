{
  lib,
  pkgs,
  ...
}:

# dol-amroth's local model (pkgs/amon-din-local.nix) sleeps when idle and frees
# ~19 GiB. Each assertion guards a way to break that while everything still
# looks fine.
let
  localText = builtins.readFile ../pkgs/amon-din-local.nix;
  amonDinText = builtins.readFile ../pkgs/amon-din.nix;
  amonDinPackages = import ../pkgs/amon-din.nix { inherit pkgs; };

  # Stand-ins for the three things `status` consults. The curl double records
  # every URL it is asked for, so the test can prove nothing else was touched.
  fakeCurl = pkgs.writeShellScriptBin "curl" ''
    url="''${!#}"
    printf '%s\n' "$url" >> "$FAKE_LOG"
    case "$url" in
      */health) exit "''${FAKE_HEALTH:-0}" ;;
      */props) printf '{"is_sleeping":%s}\n' "''${FAKE_SLEEPING:-false}" ;;
      *) echo "unexpected request $url" >&2 ;;
    esac
  '';
  fakeTools = pkgs.runCommand "amon-din-local-fake-tools" { } ''
    mkdir -p $out/bin
    cat > $out/bin/launchctl <<'EOF'
    #!/bin/sh
    [ "$1" = print ] || exit 0
    [ "''${FAKE_LOADED:-0}" = 1 ] || exit 113
    [ -n "''${FAKE_PID:-}" ] && printf '\tstate = running\n\tpid = %s\n' "$FAKE_PID"
    exit 0
    EOF
    cat > $out/bin/ps <<'EOF'
    #!/bin/sh
    echo "''${FAKE_CPU:-0.0}"
    EOF
    cat > $out/bin/footprint <<'EOF'
    #!/bin/sh
    echo "llama-server [42]: 64-bit    Footprint: 21 GB (16384 bytes per page)"
    EOF
    chmod +x $out/bin/*
  '';
  controlUnderTest =
    (import ../pkgs/amon-din-local.nix {
      inherit pkgs;
      curl = fakeCurl;
    }).amon-din-local;
in
# 1. Status may only read /health and /props. /slots reloaded the sleeping
#    model in testing; a poll that does that pins 19 GiB for good.
if lib.hasInfix "URL/slots" localText || lib.hasInfix "URL/v1/models" localText then
  throw "amon-din-local: status must never call /slots or /v1/models; both wake the sleeping model"
else if !(lib.hasInfix "/props" localText && lib.hasInfix "is_sleeping" localText) then
  throw "amon-din-local: sleep state must come from /props is_sleeping, which does not wake the model"

# 2. The menu polls through `amon-din-local status`, never the server itself.
else if !lib.hasInfix "\${localCtl} status" amonDinText then
  throw "amon-din: the menu must read the local model's state via `amon-din-local status`"
else if lib.hasInfix "127.0.0.1:8080" amonDinText || lib.hasInfix "localhost:8080" amonDinText then
  throw "amon-din: the menu must not query the local model server directly"

# 3. Without idle sleep the model holds ~19 GiB of a 32 GB Mac forever.
else if !lib.hasInfix "--sleep-idle-seconds" localText then
  throw "amon-din-local: the server must keep --sleep-idle-seconds"

# 4. Both halves ship with the rest of Amon Dîn, so one profile upgrade moves them.
else if !(amonDinPackages ? amon-din-local && amonDinPackages ? amon-din-local-serve) then
  throw "amon-din: amon-din-local and amon-din-local-serve must be exported"

else
  pkgs.runCommand "amon-din-local-contract-ok"
    {
      nativeBuildInputs = [
        controlUnderTest
        fakeTools
        pkgs.gnugrep
      ];
    }
    ''
      export FAKE_LOG=$PWD/requests HOME=$PWD
      check() {
        expected="$1"; shift
        actual=$(env "$@" amon-din-local status)
        [ "$actual" = "$expected" ] || { echo "with $*: expected $expected, got $actual" >&2; exit 1; }
      }
      check off      FAKE_LOADED=0
      check starting FAKE_LOADED=1
      check starting FAKE_LOADED=1 FAKE_PID=42 FAKE_HEALTH=7
      check asleep   FAKE_LOADED=1 FAKE_PID=42 FAKE_SLEEPING=true
      check awake    FAKE_LOADED=1 FAKE_PID=42 FAKE_SLEEPING=false FAKE_CPU=0.4
      check busy     FAKE_LOADED=1 FAKE_PID=42 FAKE_SLEEPING=false FAKE_CPU=87.5
      test "$(env FAKE_LOADED=1 FAKE_PID=42 amon-din-local memory)" = "21 GB"
      # Nothing but the two non-waking endpoints was ever requested.
      if grep -v -E '/(health|props)$' "$FAKE_LOG"; then
        echo "status requested an endpoint that can wake the model" >&2; exit 1
      fi
      touch "$out"
    ''
