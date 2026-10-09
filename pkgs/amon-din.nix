# Amon Dîn — wake nardol, prove its GPU is usable, play.
#
# ⛔ PACKAGED STANDALONE ON PURPOSE, not as a nix-darwin module.
#
# dol-amroth has nix-darwin installed but its last activation was 2025-04-05 on
# nixpkgs 24.11, while this flake targets 26.05. Delivering a menu bar icon by
# way of `darwin-rebuild switch` would drag a dormant seventeen-month-old system
# forward and switch on home-manager and the linux-builder as a side effect —
# far too much to happen because someone wanted a status glyph.
#
# So these are plain packages. `nix run`, `nix profile install`, or import the
# module. It also makes them shareable: nothing here assumes this fleet beyond
# the defaults, and every host-specific value is overridable at runtime.
{
  pkgs,
  lib ? pkgs.lib,
  # Overridable so someone else can point it at their own machine.
  nardolIp ? "10.0.0.118",
  nardolMac ? "1c:86:0b:3f:08:53",
  relayHost ? "pelargir",
  sshUser ? "edgar",
  # The servable models, shared with hosts/nixos/nardol/inference.nix. Passed as
  # an argument rather than imported so a recipient of this package can point it
  # at their own list — the same reason the host and MAC above are arguments.
  profiles ? import ../lib/inference-profiles.nix,
}:
let

  # A wired relay, kept as redundancy rather than necessity.
  #
  # ⚠️ The obvious diagnosis here was wrong, and the wrong version is worth
  # recording. When nardol would not wake, a magic packet from this Mac over
  # Wi-Fi failed and the same packet from pelargir on the wired segment also
  # failed — but the Wi-Fi attempt came first, so "the AP is not forwarding
  # directed broadcasts onto the wired side" looked like the answer. It was not.
  # The cause was ErP Ready in firmware, which cuts PCIe standby power in S5, and
  # the machine was not listening on any path. Once that was disabled, a
  # Mac-direct broadcast over Wi-Fi woke it on the first try (measured
  # 2026-09-12).
  #
  # The relay stays because it is nearly free and covers a genuinely different
  # failure — a Mac on a segment or VLAN whose broadcasts really are not
  # forwarded. It is not load-bearing today.

  # ⛔ STATUS MUST NEVER USE SSH, OR IT DEFEATS THE IDLE-SUSPEND DESIGN.
  #
  # nardol's idle loop blocks suspend on `loginctl list-sessions`. A status
  # check that SSHes in creates exactly such a session, so a menu bar item
  # polling every minute would hold the machine awake forever and quietly undo
  # Phase 2 — while looking like it was working.
  #
  # Wolf's Moonlight endpoint answers all of this over plain HTTP on 47989:
  #   <state>SUNSHINE_SERVER_FREE</state>  or  ..._BUSY
  #   <currentgame>0</currentgame>
  # No login session, no inhibitor, nothing the idle loop counts as activity.
  # A ping cannot wake a suspended host either — only a magic packet can — so
  # polling is free in both directions.
  amonDinStatus = pkgs.writeShellApplication {
    name = "amon-din-status";
    runtimeInputs = with pkgs; [
      curl
      coreutils
    ];
    text = ''
      set -uo pipefail
      CFG="$HOME/.config/amon-din/config"
      HOST=''${NARDOL:-$( [ -f "$CFG" ] && sed -n "s/^host=//p" "$CFG" | head -1 || true )}
      HOST=''${HOST:-${nardolIp}}
      # Short timeouts: this runs on a menu render and must never hang the UI.
      if ! /sbin/ping -c1 -W 1200 "$HOST" >/dev/null 2>&1; then
        echo "asleep"; exit 0
      fi
      info=$(curl -s --max-time 3 "http://$HOST:47989/serverinfo?uuid=0" 2>/dev/null) || {
        echo "waking"; exit 0
      }
      case "$info" in
        *SUNSHINE_SERVER_BUSY*) echo "busy" ;;
        *SUNSHINE_SERVER_FREE*) echo "ready" ;;
        *)                      echo "waking" ;;
      esac
    '';
  };

  # Read-only controller integration for the local inference seat. Keep this a
  # standalone Amon Dîn package: dol-amroth intentionally does not activate the
  # nix-darwin configuration, and installing this package must not change that.
  nardolLocalSeatProbe = pkgs.callPackage ./nardol-local-seat-probe.nix { };

  # Shell shared by every action that needs nardol awake before it can SSH in:
  # serve, model switch and restore. One copy, so a wake fix lands everywhere.
  # See nardolPlay below for the measurements behind the relay and deadline.
  wakeLib = ''
    CFG="$HOME/.config/amon-din/config"
    pref() { [ -f "$CFG" ] && sed -n "s/^$1=//p" "$CFG" | head -1 || true; }
    NARDOL=''${NARDOL:-$(pref host)}; NARDOL=''${NARDOL:-${nardolIp}}
    MAC=''${MAC:-$(pref mac)};        MAC=''${MAC:-${nardolMac}}
    RELAY=''${RELAY:-$(pref relay)};  RELAY=''${RELAY:-${relayHost}}
    DEADLINE=''${DEADLINE:-180}
    say() { printf '%s\n' "$*" >&2; }
    notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Amon Dîn\"" >/dev/null 2>&1 || true; }
    alert() { /usr/bin/osascript -e "display alert \"$1\" message \"$2\"" >/dev/null 2>&1 || true; }
    ssh_n() { ssh -o ConnectTimeout=4 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${sshUser}@$NARDOL" "$@"; }
    wake_nardol() {
      ssh_n true 2>/dev/null && return 0
      say "waking nardol..."; notify "Waking nardol..."
      wakeonlan "$MAC" >/dev/null 2>&1 || true
      wakeonlan -i 10.0.0.255 "$MAC" >/dev/null 2>&1 || true
      ( ssh -o ConnectTimeout=6 -o BatchMode=yes "$RELAY" \
          "wakeonlan $MAC >/dev/null 2>&1 || nix run --quiet nixpkgs#wakeonlan -- $MAC >/dev/null 2>&1" \
          >/dev/null 2>&1 || true ) &
      start=$(date +%s)
      until ssh_n true 2>/dev/null; do
        if [ $(( $(date +%s) - start )) -ge "$DEADLINE" ]; then
          alert "Nardol did not wake" "No answer within ''${DEADLINE}s."; return 1
        fi
        sleep 2
      done
    }
  '';
  wakeInputs = with pkgs; [
    openssh
    wakeonlan
    coreutils
    gnused
  ];

  # Wake Nardol and deliberately hand the GPU back to inference. This is the
  # operator override: the host command stops gaming even when a stream is live.
  amonDinServe = pkgs.writeShellApplication {
    name = "amon-din-serve";
    runtimeInputs = wakeInputs;
    text = ''
      set -euo pipefail
      ${wakeLib}
      wake_nardol
      say "preparing inference..."
      if out=$(ssh_n 'sudo nardol-model serve' 2>&1); then
        notify "$out"
      else
        alert "Could not serve inference" "$out"; exit 1
      fi
    '';
  };

  nardolPlay = pkgs.writeShellApplication {
    name = "amon-din";
    runtimeInputs = with pkgs; [
      openssh
      wakeonlan
      coreutils
      gnused
    ];
    text = ''
      set -euo pipefail

      if [ "''${1:-}" = "serve" ]; then
        exec ${amonDinServe}/bin/amon-din-serve
      fi

      CFG="$HOME/.config/amon-din/config"
      pref() { [ -f "$CFG" ] && sed -n "s/^$1=//p" "$CFG" | head -1 || true; }
      # Precedence: environment (scripting) > config file (the UI) > build-time
      # default. A recipient of this package retargets it without rebuilding.
      NARDOL=''${NARDOL:-$(pref host)}; NARDOL=''${NARDOL:-${nardolIp}}
      MAC=''${MAC:-$(pref mac)};        MAC=''${MAC:-${nardolMac}}
      RELAY=''${RELAY:-$(pref relay)};  RELAY=''${RELAY:-${relayHost}}
      # S3 resume measured at 6-10s; S5 cold boot including Tang unlock at 58s.
      # 180 leaves headroom for a cold boot that also fscks or waits on DHCP.
      DEADLINE=''${DEADLINE:-180}

      # Preferences, shared with the menu bar plugin. Environment still wins so
      # the CLI stays scriptable and testable regardless of the GUI's settings.
      CFG="$HOME/.config/amon-din/config"
      pref() { [ -f "$CFG" ] && sed -n "s/^$1=//p" "$CFG" | head -1 || echo "$2"; }
      LAUNCH=''${LAUNCH:-$(pref launch_moonlight 1)}
      [ "''${1:-}" = "--no-launch" ] && LAUNCH=0

      say() { printf '%s\n' "$*" >&2; }
      ssh_n() { ssh -o ConnectTimeout=4 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "${sshUser}@$NARDOL" "$@"; }

      if ssh_n true 2>/dev/null; then
        say "nardol is already up."
      else
        say "waking nardol..."
        wakeonlan "$MAC" >/dev/null 2>&1 || true
        wakeonlan -i 10.0.0.255 "$MAC" >/dev/null 2>&1 || true
        # Redundant path; see the note above about why it is not load-bearing.
        # ⚠️ BACKGROUNDED DELIBERATELY. Measured 2026-09-12: running this inline
        # turned a 6s wake into 25s of wall time, because resolving wakeonlan on
        # the relay is slow. Redundancy must not sit on the critical path of the
        # thing it is backing up — the direct send above has already gone out,
        # and the poll below is what actually decides success.
        (
          ssh -o ConnectTimeout=6 -o BatchMode=yes "$RELAY" \
            "wakeonlan $MAC >/dev/null 2>&1 || nix run --quiet nixpkgs#wakeonlan -- $MAC >/dev/null 2>&1" \
            >/dev/null 2>&1 || true
        ) &

        start=$(date +%s)
        until ssh_n true 2>/dev/null; do
          if [ $(( $(date +%s) - start )) -ge "$DEADLINE" ]; then
            say "nardol did not come up within ''${DEADLINE}s."
            say "  If it is powered off, wake-on-LAN needs ErP Ready disabled in firmware"
            say "  (see hosts/nixos/nardol/default.nix). If it is suspended, check that"
            say "  the magic packet reaches the wired segment."
            exit 1
          fi
          sleep 2
        done
        say "  up after $(( $(date +%s) - start ))s"
      fi

      # ⛔ DEMAND A FRESH VERIFICATION — via the VERIFIER, never the gate.
      #
      # Polling `systemctl is-active nardol-gaming-readiness` is worthless: it is
      # RemainAfterExit=true and reports active from whenever it last ran, so it
      # would answer "ready" instantly on a host whose GPU came back from suspend
      # broken. It also covers the window measured 2026-09-12 where SSH answers
      # several seconds before post-resume verification has finished.
      #
      # But restarting that gate is worse than useless, and this script did it
      # briefly. docker-wolf has Requires=nardol-gaming-readiness.service, and
      # systemd propagates a stop across Requires — restarting the gate tore Wolf
      # down (PID 27205 -> 27831) and this script printed "Wolf is not running"
      # and restarted it. On an idle host that is wasted time; run mid-session it
      # would kill the stream.
      #
      # nardol-gaming-verify runs the identical assertions with nothing depending
      # on it, so it is safe at any moment. See
      # hosts/nixos/nardol/wolf/readiness-assertions.nix.
      # ⛔ CLAIM THE GPU BEFORE VERIFYING IT, OR THE VERIFY IS MEANINGLESS.
      #
      # nardol-gaming.target is what actually arbitrates: it Conflicts= the
      # inference unit, and nardol-gpu-handover runs before it to PROVE the
      # driver reported the VRAM back rather than assuming a stopped container
      # freed it. Nothing else in the fleet starts this target — for a long time
      # nothing did at all, so the whole handover never ran on the real play
      # path and a game launched while the model held ~20 GB would have hit
      # CUDA OOM with nothing in the logs blaming inference.
      #
      # Starting the target BLOCKS until the handover succeeds, so a failure
      # here is a refusal to start gaming, which is the correct outcome: a
      # half-released GPU is worse than a late one.
      say "claiming the GPU for gaming..."
      if ! ssh_n 'sudo systemctl start nardol-gaming.target' 2>/dev/null; then
        say "could not claim the GPU — inference may still hold it."
        ssh_n 'systemctl status nardol-gpu-handover --no-pager -n 15' 2>/dev/null || true
        exit 1
      fi

      say "verifying the GPU..."
      if ! ssh_n 'sudo systemctl start --wait nardol-gaming-verify' 2>/dev/null; then
        say "GPU readiness FAILED — nardol is awake but cannot be trusted to encode."
        ssh_n 'systemctl status nardol-gaming-verify --no-pager -n 15' 2>/dev/null || true
        exit 1
      fi

      if ! ssh_n 'systemctl is-active --quiet docker-wolf' 2>/dev/null; then
        say "Wolf is not running; starting it..."
        ssh_n 'sudo systemctl start docker-wolf' || { say "could not start Wolf"; exit 1; }
      fi

      say "nardol is ready."
      if [ "$LAUNCH" = "1" ]; then
        open -a Moonlight 2>/dev/null || say "  (Moonlight not installed; launch your client manually)"
      fi
    '';
  };

  # The menu bar item, as a SwiftBar plugin.
  #
  # SwiftBar re-runs this script on its refresh interval and renders whatever it
  # prints: the first block is the title, everything after "---" is the menu.
  #
  # ⛔ ONE SOURCE OF TRUTH, OVER HTTP. Everything shown comes from nardol-lease's
  # /status (via nardol-local-seat-probe): what is ACTUALLY serving, whether it
  # is loading, gaming, busy or down, and whether the unit hit its restart
  # limit. Polling must never use SSH — a login session every minute would pin
  # nardol awake and undo idle-suspend. Actions (clicks) may SSH; status may not.
  #
  # Redesigned 2026-10-08: the old menu ticked the model this Mac last chose,
  # showed only gaming state, and buried the model picker under Preferences.
  amonDinPlugin = pkgs.writeShellApplication {
    name = "amondin.1m.sh";
    runtimeInputs = with pkgs; [
      coreutils
      gnused
      jq
      nardolLocalSeatProbe
    ];
    text = ''
      set -uo pipefail
      CFG="$HOME/.config/amon-din/config"
      mkdir -p "$(dirname "$CFG")"
      [ -f "$CFG" ] || printf 'launch_moonlight=1\nnotify=1\npoll=1\nmodel=${profiles.default}\n' > "$CFG"
      get() { sed -n "s/^$1=//p" "$CFG" | head -1; }
      SELF="${placeholder "out"}/bin/amondin.1m.sh"
      HOST=$(get host); HOST=''${HOST:-${nardolIp}}

      case "''${1:-}" in
        # ⚠️ GNU sed, not BSD: runtimeInputs supplies gnused, so plain -i.
        toggle) k="$2"; v=$(get "$k"); n=$([ "$v" = "1" ] && echo 0 || echo 1)
                sed -i "s/^$k=.*/$k=$n/" "$CFG"; exit 0 ;;
      esac

      if [ "$(get poll)" = "1" ]; then
        st=$(nardol-local-seat-probe "http://$HOST:8002/status")
      else
        st='{"state":"off"}'
      fi
      field() { printf '%s' "$st" | jq -r "$1 // empty" 2>/dev/null; }
      state=$(field .state); detail=$(field .detail)
      serving=$(field .serving_profile); saved=$(field .model_profile)
      since=$(field .since_seconds); limited=$(field .restart_limited)
      # Lease not answering yet while the host boots reads as "degraded"; it is
      # really still waking.
      case "$detail" in *refused*) state=waking ;; esac

      label() {
        case "$1" in
      ${lib.concatStringsSep "\n" (
        lib.mapAttrsToList (name: p: ''${name}) echo "${p.label}" ;;'') profiles.profiles
      )}
          "") echo "nothing" ;;
          *) echo "$1" ;;
        esac
      }
      ago() { [ -n "$1" ] && printf '%dm %02ds' $(( $1 / 60 )) $(( $1 % 60 )); }

      # A lease older than the serving file reports only the saved choice;
      # show that rather than "nothing" while it is up.
      if [ -z "$serving" ] && { [ "$state" = ready ] || [ "$state" = busy ]; }; then
        serving="$saved"
      fi
      # The checkmark: what is serving; mid-switch, what is loading; asleep, the
      # last choice made from this Mac (nardol cannot be asked without waking).
      current="$serving"
      [ "$state" = loading ] && current="$saved"
      [ -z "$current" ] && current=$(get model)

      case "$state" in
        ready)    echo ":brain.head.profile: | sfcolor=orange" ;;
        busy)     echo ":brain.head.profile: | sfcolor=green" ;;
        loading)  echo ":hourglass: | sfcolor=yellow" ;;
        gaming)   echo ":gamecontroller.fill: | sfcolor=green" ;;
        waking)   echo ":sunrise: | sfcolor=yellow" ;;
        asleep)   echo ":moon.zzz: | sfcolor=secondaryLabelColor" ;;
        degraded) echo ":exclamationmark.triangle.fill: | sfcolor=red" ;;
        *)        echo ":questionmark.circle: | sfcolor=secondaryLabelColor" ;;
      esac
      echo "---"
      case "$state" in
        ready)    echo "Ready · $(label "$serving")" ;;
        busy)     echo "Working · $(label "$serving")" ;;
        loading)  echo "Loading $(label "$saved") · $(ago "$since")" ;;
        gaming)   echo "Gaming session on" ;;
        waking)   echo "Waking up…" ;;
        asleep)   echo "Asleep · wakes when needed" ;;
        degraded) if [ "$limited" = true ]; then echo "Inference down · restart limit hit"
                  else echo "Inference down"; fi ;;
        *)        echo "Status polling is off" ;;
      esac
      engine=$(field .engine)
      if [ -n "$engine" ] && { [ "$state" = ready ] || [ "$state" = busy ]; }; then
        echo "on $engine · up $(ago "$since") | color=secondaryLabelColor size=11"
      fi
      echo "---"

      echo "Play | bash=${nardolPlay}/bin/amon-din terminal=false refresh=true"
      case "$state" in
        ready|busy|loading) ;;
        gaming) echo "Serve inference (ends the game) | bash=${amonDinServe}/bin/amon-din-serve terminal=false refresh=true" ;;
        *)      echo "Serve inference | bash=${amonDinServe}/bin/amon-din-serve terminal=false refresh=true" ;;
      esac
      if [ "$state" = degraded ]; then
        echo "Restore default model | bash=${amonDinRestore}/bin/amon-din-restore terminal=false refresh=true sfimage=arrow.counterclockwise"
      fi

      # Palantír runs only beside the GLM profile (hosts/nixos/nardol/palantir.nix),
      # and not at all while switched off in Settings.
      palantir=$(field .palantir)
      if [ "$palantir" = off ]; then :
      elif [ "$serving" = "glm-4.6v-flash" ] && { [ "$state" = ready ] || [ "$state" = busy ]; }; then
        echo "Ask Palantír… | bash=${palantirAsk}/bin/amon-din-palantir terminal=false sfimage=sparkle.magnifyingglass"
      else
        echo "Ask Palantír… | bash=${amonDinModel}/bin/amon-din-model param1=glm-4.6v-flash terminal=false refresh=true sfimage=sparkle.magnifyingglass tooltip=\"Palantír needs the GLM profile: this switches to it (~2.5 min)\""
      fi
      # ⛔ THE LIST COMES FROM lib/inference-profiles.nix, WHICH NARDOL ALSO
      # READS, so the menu cannot offer a model the host cannot serve.
      # ⛔ NO COLON AFTER "Model". SwiftBar reads ":name:" as an SF Symbol, and
      # with "Model: ..." as the parent every item in the submenu came up
      # disabled (2026-10-08) while Settings, built the same way, worked.
      echo "Model · $(label "$current")"
      ${lib.concatStringsSep "\n" (
        map (name: ''
          mark=""; [ "$current" = "${name}" ] && mark=" ✓"
          echo "--${
            profiles.profiles.${name}.label
          }$mark | bash=${amonDinModel}/bin/amon-din-model param1=${name} terminal=false refresh=true tooltip=\"${
            profiles.profiles.${name}.summary
          }\""'') ([ profiles.default ] ++ (lib.remove profiles.default (lib.attrNames profiles.profiles)))
      )}
      # Tried models (nardol-model try): listed by the lease, so only while
      # nardol is up; the host validated each name when it was added.
      tried=$(printf '%s' "$st" | jq -r '.user_profiles[]? | "\(.name)\t\(.engine)\t\(.repo)"' 2>/dev/null || true)
      if [ -n "$tried" ]; then
        echo "-----"
        echo "--Tried | color=secondaryLabelColor size=11"
        while IFS=$'\t' read -r n e r; do
          [[ "$n" =~ ^[a-z0-9][a-z0-9.-]{0,39}$ ]] || continue
          mark=""; [ "$current" = "$n" ] && mark=" ✓"
          echo "--$n$mark | bash=${amonDinModel}/bin/amon-din-model param1=$n terminal=false refresh=true tooltip=\"$r on $e\""
        done <<<"$tried"
      fi
      echo "-----"
      if [ "$state" = ready ] || [ "$state" = busy ] || [ "$state" = loading ] || [ "$state" = degraded ]; then
        echo "--Try a model from Hugging Face… | bash=${amonDinTry}/bin/amon-din-try terminal=false refresh=true sfimage=arrow.down.circle"
        if [ -n "$tried" ]; then
          echo "--Forget a tried model"
          while IFS=$'\t' read -r n _ r; do
            [[ "$n" =~ ^[a-z0-9][a-z0-9.-]{0,39}$ ]] || continue
            echo "----$n | bash=${amonDinTry}/bin/amon-din-try param1=--forget param2=$n terminal=false refresh=true tooltip=\"Deletes $r from nardol\""
          done <<<"$tried"
        fi
      fi
      echo "--Switching restarts the server: ~1 min (ik), ~2.5 min (vLLM) | color=secondaryLabelColor size=11"
      echo "---"
      if [ "$state" = gaming ]; then
        echo "Sleep | color=secondaryLabelColor tooltip=\"Not during a game session\""
      else
        echo "Sleep | bash=${sleepNow}/bin/amon-din-sleep terminal=false refresh=true"
      fi
      echo "Settings"
      echo "--Launch Moonlight after Play $([ "$(get launch_moonlight)" = 1 ] && echo '✓') | bash=$SELF param1=toggle param2=launch_moonlight terminal=false refresh=true"
      echo "--Show notifications $([ "$(get notify)" = 1 ] && echo '✓') | bash=$SELF param1=toggle param2=notify terminal=false refresh=true"
      echo "--Poll status $([ "$(get poll)" = 1 ] && echo '✓') | bash=$SELF param1=toggle param2=poll terminal=false refresh=true"
      # Known only while nardol answers; the switch lives on nardol, not here.
      case "$palantir" in
        off) echo "--Palantír video agent | bash=${amonDinPalantirToggle}/bin/amon-din-palantir-toggle param1=on terminal=false refresh=true tooltip=\"Off. Turn on to answer questions about videos (needs the GLM model)\"" ;;
        running|waiting) echo "--Palantír video agent ✓ | bash=${amonDinPalantirToggle}/bin/amon-din-palantir-toggle param1=off terminal=false refresh=true tooltip=\"On$([ "$palantir" = waiting ] && echo ', starts with the GLM model'). Click to turn off\"" ;;
      esac
      echo "-----"
      echo "--Edit config ($HOST)… | bash=/usr/bin/open param1=-t param2=$CFG terminal=false"
      echo "Refresh | refresh=true"
    '';
  };

  # Switching the served model. A separate binary for the same reason as
  # sleeping: the menu passes one argument to one program, so a mix-up cannot
  # turn a preference click into something else.
  #
  # ⛔ SSH IS FINE HERE AND ONLY HERE. The no-SSH rule covers POLLING — a login
  # session every minute would hold nardol awake forever. This runs when a human
  # clicks, which is exactly when a session is harmless, and it is the same path
  # `amon-din` already takes to start the gaming target.
  amonDinModel = pkgs.writeShellApplication {
    name = "amon-din-model";
    runtimeInputs = wakeInputs;
    text = ''
      set -euo pipefail
      WANT="''${1:?usage: amon-din-model <profile>}"
      # Tried-profile names arrive from the lease's JSON; never let one become shell.
      [[ "$WANT" =~ ^[a-z0-9][a-z0-9.-]{0,39}$ ]] || { echo "bad profile name" >&2; exit 2; }
      ${wakeLib}
      wake_nardol
      notify "Switching to $WANT..."
      # The menu now ticks what nardol reports serving; this local record only
      # labels the menu while nardol is asleep. Written after the host accepts.
      if out=$(ssh_n "sudo nardol-model switch $WANT" 2>&1); then
        if [ -f "$CFG" ] && grep -q '^model=' "$CFG"; then
          sed -i "s/^model=.*/model=$WANT/" "$CFG"
        else
          printf 'model=%s\n' "$WANT" >> "$CFG"
        fi
        notify "$out"
      else
        alert "Could not switch model" "$out"; exit 1
      fi
    '';
  };

  # "Try a model from Hugging Face…" and "Forget": the menu face of
  # `nardol-model try` / `forget`. A download can take many minutes, so the
  # dialog returns at once and the result arrives as a notification.
  amonDinTry = pkgs.writeShellApplication {
    name = "amon-din-try";
    runtimeInputs = wakeInputs;
    text = ''
      set -uo pipefail
      ${wakeLib}
      osa() { /usr/bin/osascript "$@" 2>/dev/null; }
      if [ "''${1:-}" = --forget ]; then
        n="''${2:-}"
        [[ "$n" =~ ^[a-z0-9][a-z0-9.-]{0,39}$ ]] || exit 2
        osa -e "display dialog \"Forget $n and delete its files from nardol?\" with title \"Amon Dîn\" buttons {\"Cancel\", \"Forget\"} default button \"Cancel\"" >/dev/null || exit 0
        if out=$(ssh_n "sudo nardol-model forget $n" 2>&1); then notify "$out"; else alert "Could not forget $n" "$out"; exit 1; fi
        exit 0
      fi
      repo=$(osa -e 'text returned of (display dialog "Hugging Face model (org/repo). GGUF repos run on ik_llama (Q4_K_M by default); safetensors on vLLM (FP8 if unquantized). Gated repos need the token in /var/lib/nardol-inference/hf-token on nardol." default answer "" with title "Try a model" buttons {"Cancel", "Download and serve"} default button "Download and serve")') || exit 0
      repo=''${repo#https://huggingface.co/}; repo=''${repo%%/tree/*}; repo=''${repo%/}
      if ! [[ "$repo" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]; then
        alert "Not a model id" "Expected org/repo, got: $repo"; exit 2
      fi
      wake_nardol
      notify "Downloading $repo… (the menu updates when it is serving)"
      if out=$(ssh_n "sudo nardol-model try $repo" 2>&1); then
        notify "$(tail -1 <<<"$out")"
      else
        alert "Could not try $repo" "$(tail -5 <<<"$out")"; exit 1
      fi
    '';
  };

  # Settings → Palantír video agent: `nardol-palantir on|off` on the host.
  amonDinPalantirToggle = pkgs.writeShellApplication {
    name = "amon-din-palantir-toggle";
    runtimeInputs = wakeInputs;
    text = ''
      set -uo pipefail
      ${wakeLib}
      case "''${1:-}" in on | off) ;; *) echo "usage: amon-din-palantir-toggle on|off" >&2; exit 2 ;; esac
      if out=$(ssh_n "sudo nardol-palantir $1" 2>&1); then notify "$out"; else alert "Could not turn Palantír $1" "$out"; exit 1; fi
    '';
  };

  # The way out of a restart-limited inference unit (see `nardol-model restore`).
  amonDinRestore = pkgs.writeShellApplication {
    name = "amon-din-restore";
    runtimeInputs = wakeInputs;
    text = ''
      set -euo pipefail
      ${wakeLib}
      wake_nardol
      notify "Restoring the default model..."
      if out=$(ssh_n 'sudo nardol-model restore' 2>&1); then
        notify "$out"
      else
        alert "Could not restore inference" "$out"; exit 1
      fi
    '';
  };

  # A command-line client for Palantír (nardol:8003), the video agent that runs
  # beside the GLM profile. Plain curl against its OpenAI-compatible API.
  palantirCli = pkgs.writeShellApplication {
    name = "palantir";
    runtimeInputs = with pkgs; [
      curl
      jq
      coreutils
      gnused
    ];
    text = ''
      set -euo pipefail
      CFG="$HOME/.config/amon-din/config"
      host=''${NARDOL:-$( [ -f "$CFG" ] && sed -n "s/^host=//p" "$CFG" | head -1 || true )}
      API="''${PALANTIR_URL:-http://''${host:-${nardolIp}}:8003}"
      usage() {
        cat >&2 <<EOF
      usage: palantir ask "question" [video-file-or-url ...]
             palantir add <video-file-or-url> [--index]
             palantir enroll <name> <photo> [photo ...]
             palantir people | videos
      EOF
        exit 2
      }
      # A local file is uploaded; a URL is registered by reference.
      add() {
        if [ -f "$1" ]; then
          curl -fsS -X POST "$API/v1/videos" -F "file=@$1" -F "index=''${2:-0}"
        else
          jq -n --arg u "$1" --argjson i "''${2:-0}" '{url: $u, index: ($i == 1)}' |
            curl -fsS -X POST "$API/v1/videos" -H 'content-type: application/json' -d @-
        fi
      }
      cmd="''${1:-}"; shift || true
      case "$cmd" in
        ask)
          [ $# -ge 1 ] || usage
          q="$1"; shift
          for v in "$@"; do
            id=$(add "$v" | jq -r .id)
            q="$q (video $id)"
          done
          jq -n --arg q "$q" '{model: "palantir", messages: [{role: "user", content: $q}]}' |
            curl -fsS --max-time 1800 "$API/v1/chat/completions" -H 'content-type: application/json' -d @- |
            jq -r '.choices[0].message.content'
          ;;
        add)
          [ $# -ge 1 ] || usage
          i=0; [ "''${2:-}" = "--index" ] && i=1
          add "$1" "$i" | jq -r '"\(.id)  \(.name)  \(.duration | floor)s"'
          ;;
        enroll)
          [ $# -ge 2 ] || usage
          name="$1"; shift
          args=(-F "name=$name")
          for f in "$@"; do args+=(-F "files=@$f"); done
          curl -fsS -X POST "$API/v1/people" "''${args[@]}" | jq -r '"\(.name): \(.added) of \(.photos) photos had a usable face"'
          ;;
        people) curl -fsS "$API/v1/people" | jq -r '.data[] | "\(.name)  (\(.references) photos)"' ;;
        videos) curl -fsS "$API/v1/videos" | jq -r '.data[] | "\(.id)  \(.name)  \(.duration | floor)s"' ;;
        *) usage ;;
      esac
    '';
  };

  # "Ask Palantír…" from the menu: a question, optionally a video, the answer
  # in a dialog. Long questions take a while (first look at a long video
  # indexes it), so progress goes to a notification first.
  palantirAsk = pkgs.writeShellApplication {
    name = "amon-din-palantir";
    runtimeInputs = [ palantirCli ];
    text = ''
      set -uo pipefail
      osa() { /usr/bin/osascript "$@" 2>/dev/null; }
      q=$(osa -e 'text returned of (display dialog "Ask Palantír about your videos:" default answer "" with title "Palantír" buttons {"Cancel", "Add a video…", "Ask"} default button "Ask")') || exit 0
      [ -n "$q" ] || exit 0
      args=()
      choice=$(osa -e 'button returned of (display dialog "Attach a video file to this question?" with title "Palantír" buttons {"No", "Choose…"} default button "No")') || choice=No
      if [ "$choice" = "Choose…" ]; then
        f=$(osa -e 'POSIX path of (choose file with prompt "Video for Palantír:" of type {"public.movie"})') || f=""
        [ -n "$f" ] && args+=("$f")
      fi
      osa -e 'display notification "Working on it… (a new video is indexed first)" with title "Palantír"'
      if a=$(palantir ask "$q" "''${args[@]}" 2>&1); then
        a=''${a//\"/\\\"}
        osa -e "display dialog \"$a\" with title \"Palantír\" buttons {\"OK\"} default button \"OK\""
      else
        a=''${a//\"/\\\"}
        osa -e "display alert \"Palantír could not answer\" message \"$a\""
      fi
    '';
  };

  # Sleeping is a deliberate, separate binary so the menu cannot invoke it by
  # accident through an argument mix-up.
  sleepNow = pkgs.writeShellApplication {
    name = "amon-din-sleep";
    runtimeInputs = with pkgs; [ openssh ];
    text = ''
      set -euo pipefail
      ssh -o ConnectTimeout=6 -o BatchMode=yes "${sshUser}@${nardolIp}" \
        'sudo systemctl suspend' 2>/dev/null || true
    '';
  };

  # Amon Dîn — the beacon that signals TO Nardol. Lighting it is what summons the
  # machine, which is as close to a literal description of this program as a name
  # is likely to get.
  #
  # A double-clickable app, because "run a terminal command" is not a way to
  # start a game. nix-darwin links anything under $out/Applications into
  # /Applications/Nix Apps, so this shows up in Finder, Spotlight and the Dock
  # like any other app and can be pinned there.
  #
  # It reports progress through notifications rather than a terminal window: the
  # wake takes ~7s from S3 and ~58s from a cold boot, which is long enough that
  # silence reads as failure.
  nardolPlayApp = pkgs.runCommand "amon-din-app" { } ''
    app="$out/Applications/Amon Dîn.app"
    mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

    cat > "$app/Contents/Info.plist" <<'PLIST'
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>CFBundleName</key><string>Amon Dîn</string>
      <key>CFBundleDisplayName</key><string>Amon Dîn</string>
      <key>CFBundleIdentifier</key><string>io.saldivar.amon-din</string>
      <key>CFBundleVersion</key><string>1.0</string>
      <key>CFBundlePackageType</key><string>APPL</string>
      <key>CFBundleExecutable</key><string>amon-din-app</string>
      <key>LSUIElement</key><true/>
    </dict>
    </plist>
    PLIST

    cat > "$app/Contents/MacOS/amon-din-app" <<'SH'
    #!/bin/sh
    notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Amon Dîn\"" >/dev/null 2>&1; }
    notify "Waking nardol..."
    if out=$(${nardolPlay}/bin/amon-din 2>&1); then
      notify "Ready. Launching Moonlight."
    else
      /usr/bin/osascript -e "display alert \"nardol is not ready\" message \"$out\"" >/dev/null 2>&1
      exit 1
    fi
    SH
    chmod +x "$app/Contents/MacOS/amon-din-app"
  '';
in
{
  # kebab-case so `nix run .#amon-din` reads naturally.
  amon-din = nardolPlay;
  amon-din-app = nardolPlayApp;
  amon-din-status = amonDinStatus;
  amon-din-menubar = amonDinPlugin;
  amon-din-sleep = sleepNow;
  amon-din-serve = amonDinServe;
  amon-din-model = amonDinModel;
  amon-din-restore = amonDinRestore;
  amon-din-try = amonDinTry;
  amon-din-palantir-toggle = amonDinPalantirToggle;
  amon-din-palantir = palantirAsk;
  palantir = palantirCli;
  nardol-local-seat-probe = nardolLocalSeatProbe;
}
