{
  lib,
  pkgs,
  ...
}:

# opencode's context limit for nardol follows whatever nardol serves
# (amonDinOpencodeSync in pkgs/amon-din.nix). Each assertion guards a way to
# break that while everything still looks fine: a limit that silently stops
# moving, a poll that reaches the inference port every minute, or a rewrite
# that damages a hand-edited config.
let
  amonDinText = builtins.readFile ../pkgs/amon-din.nix;

  # Records every URL it is asked for; answers /v1/models with FAKE_CTX.
  fakeCurl = pkgs.writeShellScriptBin "curl" ''
    url="''${!#}"
    printf '%s\n' "$url" >> "$FAKE_LOG"
    case "$url" in
      */v1/models) printf '{"data":[{"id":"default","max_model_len":%s}]}\n' "''${FAKE_CTX:-null}" ;;
      *) echo "unexpected request $url" >&2; exit 7 ;;
    esac
  '';
  fakeTools = pkgs.runCommand "amon-din-opencode-sync-fake-tools" { } ''
    mkdir -p $out/bin
    cat > $out/bin/opencode <<'EOF'
    #!/bin/sh
    echo "$*" >> "$FAKE_RELOADS"
    EOF
    cat > $out/bin/pgrep <<'EOF'
    #!/bin/sh
    exit "''${FAKE_NO_SERVICE:-0}"
    EOF
    chmod +x $out/bin/*
  '';
  sharedProfiles = import ../lib/inference-profiles.nix;
  changedProfiles = sharedProfiles // {
    profiles = sharedProfiles.profiles // {
      "glm-4.6v-flash" = sharedProfiles.profiles."glm-4.6v-flash" // {
        maxModelLen = sharedProfiles.profiles."glm-4.6v-flash".maxModelLen + 1;
      };
    };
  };
  syncUnderTest =
    (import ../pkgs/amon-din.nix {
      inherit pkgs;
      profiles = sharedProfiles;
      curl = fakeCurl;
    }).amon-din-opencode-sync;
  syncWithChangedProfiles =
    (import ../pkgs/amon-din.nix {
      inherit pkgs;
      profiles = changedProfiles;
      curl = fakeCurl;
    }).amon-din-opencode-sync;
in
# The menu must pass the serving profile. Called bare, the sync cannot tell an
# unchanged profile from a new one and reaches nardol's inference port on every
# one-minute poll.
if !lib.hasInfix ''amon-din-opencode-sync "$serving"'' amonDinText then
  throw "amon-din: the menu must call amon-din-opencode-sync with the serving profile"
else
  pkgs.runCommand "amon-din-opencode-sync-contract-ok"
    {
      nativeBuildInputs = [
        syncUnderTest
        fakeTools
        pkgs.gnugrep
        pkgs.diffutils
      ];
    }
    ''
      export HOME=$PWD FAKE_LOG=$PWD/requests FAKE_RELOADS=$PWD/reloads
      mkdir -p .config/amon-din .config/opencode
      STAMP=.config/amon-din/opencode-synced
      CHANGED_SYNC=${syncWithChangedProfiles}/bin/amon-din-opencode-sync
      # No notifications from a build.
      printf 'notify=0\n' > .config/amon-din/config
      OC=.config/opencode/opencode.jsonc
      cat > $OC <<'EOF'
      {
        // a comment that must survive
        "a": { "limit": { "context": 37888, "output": 16000 } }, // amon-din:nardol-context
        "b": { "limit": { "context": 37888, "output": 16000 } }, // amon-din:nardol-context
        "mac": { "limit": { "context": 112000, "output": 16000 } }
      }
      EOF
      count() { if [ -f "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi; }
      fail() { echo "FAIL: $*" >&2; cat $OC >&2; exit 1; }
      expect() { [ "$(count "$FAKE_LOG")" = "$1" ] && [ "$(count "$FAKE_RELOADS")" = "$2" ] \
        || fail "$3: expected $1 requests and $2 reloads, got $(count "$FAKE_LOG") and $(count "$FAKE_RELOADS")"; }

      # A new profile: one request, both marked lines rewritten, opencode reloaded.
      FAKE_CTX=147456 amon-din-opencode-sync heretic
      [ "$(grep -c '"context": 147456' $OC)" = 2 ] || fail "marked lines not rewritten"
      grep -q '"context": 112000' $OC || fail "an unmarked line changed"
      grep -q 'a comment that must survive' $OC || fail "a comment was lost"
      expect 1 1 "first sync"

      # The same profile again: nardol is not asked and nothing reloads.
      FAKE_CTX=147456 amon-din-opencode-sync heretic
      expect 1 1 "unchanged profile"

      # A switch is followed.
      FAKE_CTX=32768 amon-din-opencode-sync glm-4.6v-flash
      [ "$(grep -c '"context": 32768' $OC)" = 2 ] || fail "a switch was not followed"
      expect 2 2 "switch"

      # A name-only stamp from an older package must be queried once, even
      # when the served limit has not changed, then replaced with the new stamp.
      printf 'glm-4.6v-flash\n' > "$STAMP"
      FAKE_CTX=32768 amon-din-opencode-sync glm-4.6v-flash
      expect 3 2 "legacy stamp"
      case "$(cat "$STAMP")" in glm-4.6v-flash:*) ;; *) fail "legacy stamp was not replaced" ;; esac
      FAKE_CTX=32768 amon-din-opencode-sync glm-4.6v-flash
      expect 3 2 "unchanged current stamp"

      # The same serving name with changed shared profile data is queried once.
      cp "$STAMP" old-stamp
      FAKE_CTX=140000 "$CHANGED_SYNC" glm-4.6v-flash
      [ "$(grep -c '"context": 140000' $OC)" = 2 ] || fail "changed profile data was not followed"
      cmp -s "$STAMP" old-stamp && fail "profile data did not change the stamp"
      expect 4 3 "changed profile data"
      FAKE_CTX=140000 "$CHANGED_SYNC" glm-4.6v-flash
      expect 4 3 "unchanged changed-profile stamp"

      # Nothing usable from nardol: an error, the file untouched, and the stamp
      # not advanced, so the next poll retries the same profile.
      cp $OC before
      if FAKE_CTX=null amon-din-opencode-sync qwen3.8-27b; then fail "no context must be an error"; fi
      cmp -s $OC before || fail "the file changed without a context"
      FAKE_CTX=155648 amon-din-opencode-sync qwen3.8-27b
      [ "$(grep -c '"context": 155648' $OC)" = 2 ] || fail "did not retry after a failure"
      expect 6 4 "retry"

      # No opencode service running: rewrite, but never start one to reload it.
      FAKE_NO_SERVICE=1 FAKE_CTX=147456 amon-din-opencode-sync heretic
      [ "$(grep -c '"context": 147456' $OC)" = 2 ] || fail "not rewritten without a service"
      expect 7 4 "no service"

      # A config without the marker is neither read from nardol nor modified.
      printf '{ "limit": { "context": 1000 } }\n' > other.jsonc
      OPENCODE_CONFIG_FILE=$PWD/other.jsonc FAKE_CTX=147456 amon-din-opencode-sync other
      grep -q '"context": 1000' other.jsonc || fail "an unmarked config was modified"
      expect 7 4 "unmarked config"

      touch $out
    ''
