{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# `nardol-model switch` decides "already serving" before it touches the unit. On 2026-11-12 it
# trusted the saved choice: a stale drop-in had made the launcher refuse the 9B and serve the
# default (fallback=1), yet every switch back to the 9B answered "already serving" because the saved
# choice still named it. This runs nardol's own script, as built, against a fake systemctl and curl
# and a temporary serving file, and asserts it restarts unless the serving file says the wanted
# profile started without a fallback, keeping the tried-profile staleness rule.
let
  nardol = nixosConfigurations.nardol.config;
  nardolModel = lib.findFirst (
    p: lib.getName p == "nardol-model"
  ) null nardol.environment.systemPackages;
  # Its text without the store references: the run below uses this system's tools and the fakes,
  # never nardol's binaries, so no x86_64-linux build is needed to check it on the Mac.
  script = builtins.unsafeDiscardStringContext nardolModel.text;
  stateDir = builtins.dirOf nardol.nardol.inference.profileStateFile;
  profileData = import ../lib/inference-profiles.nix;
  fallbackProfile = profileData.default;
  want = lib.head (lib.filter (n: n != fallbackProfile) (lib.attrNames profileData.profiles));
in
assert lib.assertMsg (nardolModel != null) "nardol-model is not in nardol's system packages";
pkgs.runCommand "nardol-model-switch"
  {
    nativeBuildInputs = with pkgs; [
      coreutils
      gnused
      gawk
      gnugrep
      jq
    ];
    inherit
      script
      stateDir
      want
      fallbackProfile
      ;
    wantEngine = profileData.profiles.${want}.engine;
    fallbackEngine = profileData.profiles.${fallbackProfile}.engine;
    passAsFile = [ "script" ];
  }
  ''
    set -euo pipefail
    fail() { echo "nardol-model-switch: $*" >&2; exit 1; }
    tmp=$(mktemp -d)
    mkdir -p "$tmp/bin"

    # The script with its PATH line dropped and its two host paths pointed at the scratch dir.
    sed -e '1d' -e '/^export PATH=/d' \
      -e "s|/run/nardol-inference/serving|$tmp/run/serving|g" \
      -e "s|$stateDir|$tmp/state|g" "$scriptPath" > "$tmp/nardol-model"
    grep -q 'already serving' "$tmp/nardol-model" || fail "the script has no already-serving guard"
    ! grep -q -e /run/nardol-inference -e "$stateDir" "$tmp/nardol-model" || fail "a host path survived the rewrite"
    ! grep -q '^export PATH=' "$tmp/nardol-model" || fail "the script's PATH survived the rewrite"

    cat > "$tmp/bin/systemctl" <<EOF
    #!${pkgs.runtimeShell}
    echo "\$*" >> "$tmp/calls"
    unit="\''${!#}"
    case "\$1" in
      is-active)
        case "\$unit" in
          docker-ikllama) [ "\''${UNIT_ACTIVE:-1}" = 1 ] ;;
          nardol-gaming.target) [ "\''${GAMING:-0}" = 1 ] ;;
          *) exit 3 ;;
        esac ;;
      is-failed) exit 1 ;;
      restart|reset-failed|start|stop) exit 0 ;;
      *) exit 2 ;;
    esac
    EOF
    printf '#!%s\nexit 0\n' "${pkgs.runtimeShell}" > "$tmp/bin/curl"
    chmod +x "$tmp/bin/systemctl" "$tmp/bin/curl"
    export PATH="$tmp/bin:$PATH"

    now=$(date +%s)
    setup() {
      rm -rf "$tmp/state" "$tmp/run"; mkdir -p "$tmp/state/profiles.d" "$tmp/run"
      : > "$tmp/calls"; echo "$1" > "$tmp/state/profile"
    }
    serving() { printf 'profile=%s\nengine=%s\nstarted=%s\nfallback=%s\n' "$1" "$2" "$3" "$4" > "$tmp/run/serving"; }
    switch() { UNIT_ACTIVE="''${UNIT_ACTIVE:-1}" bash "$tmp/nardol-model" switch "$1" > "$tmp/out" 2>&1 || true; }
    already() {
      grep -qx "already serving $1" "$tmp/out" || fail "$2: expected already serving, got: $(cat "$tmp/out")"
      ! grep -q '^restart ' "$tmp/calls" || fail "$2: restarted though already serving"
    }
    restarted() {
      ! grep -q 'already serving' "$tmp/out" || fail "$2: said already serving: $(cat "$tmp/out")"
      grep -qx 'restart docker-ikllama' "$tmp/calls" || fail "$2: no restart: $(cat "$tmp/out")"
      [ "$(cat "$tmp/state/profile")" = "$1" ] || fail "$2: the saved choice is not $1"
    }

    # 2026-11-12: saved choice the 9B, the default serving after a fallback.
    setup "$want"; serving "$fallbackProfile" "$fallbackEngine" "$now" 1; switch "$want"
    restarted "$want" "after a fallback"
    # The wanted profile started cleanly: nothing to do.
    setup "$want"; serving "$want" "$wantEngine" "$now" 0; switch "$want"
    already "$want" "serving cleanly"
    # The serving file names the profile but says it fell back, or there is no serving file.
    setup "$want"; serving "$want" "$wantEngine" "$now" 1; switch "$want"
    restarted "$want" "profile with fallback=1"
    setup "$want"; switch "$want"
    restarted "$want" "no serving file"
    # Inference stopped: recorded, not restarted, as before.
    setup "$want"; serving "$want" "$wantEngine" "$now" 0; UNIT_ACTIVE=0 switch "$want"
    grep -q '^recorded ' "$tmp/out" || fail "stopped unit: $(cat "$tmp/out")"
    ! grep -q '^restart ' "$tmp/calls" || fail "stopped unit: restarted"

    # Tried profiles keep the staleness rule: tweaked after it started means restart.
    tried=switch-test
    setup "$tried"; echo '{}' > "$tmp/state/profiles.d/$tried.json"
    serving "$tried" vllm "$((now + 100))" 0; switch "$tried"
    already "$tried" "tried, untouched since it started"
    setup "$tried"; echo '{}' > "$tmp/state/profiles.d/$tried.json"
    serving "$tried" vllm "$((now - 100))" 0; switch "$tried"
    restarted "$tried" "tried, tweaked since it started"

    touch "$out"
  ''
