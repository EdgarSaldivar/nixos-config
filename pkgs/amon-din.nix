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
  # That makes the whole item a shell script the flake owns, rather than an app
  # with hidden state.
  #
  # The filename interval is 1m. That is affordable ONLY because the status probe
  # is SSH-free (see amon-din-status); a probe that created a login session at
  # this cadence would pin nardol awake permanently.
  amonDinPlugin = pkgs.writeShellApplication {
    name = "amondin.1m.sh";
    runtimeInputs = with pkgs; [
      coreutils
      gnused
    ];
    text = ''
      set -uo pipefail
      CFG="$HOME/.config/amon-din/config"
      mkdir -p "$(dirname "$CFG")"
      [ -f "$CFG" ] || printf 'launch_moonlight=1\nnotify=1\npoll=1\nmodel=${profiles.default}\n' > "$CFG"
      get() { sed -n "s/^$1=//p" "$CFG" | head -1; }
      SELF="${placeholder "out"}/bin/amondin.1m.sh"

      # ⛔ THE CHECKMARK IS THIS MAC'S MEMORY, NOT NARDOL'S STATE, and the
      # difference matters when they disagree. Asking the host what it serves
      # would mean SSH on every refresh, which pins the machine awake and undoes
      # idle-suspend — the one rule this plugin exists under. `nardol-model
      # current` on the host is the authority; this is the last choice made from
      # here, which for a single user is the same thing until it is not.
      MODEL=$(get model); MODEL=''${MODEL:-${profiles.default}}
      model_label() {
        case "$1" in
      ${lib.concatStringsSep "\n" (
        lib.mapAttrsToList (name: p: ''    ${name}) echo "${p.label}" ;;'') profiles.profiles
      )}
          *) echo "$1" ;;
        esac
      }

      case "''${1:-}" in
        # ⚠️ GNU sed, not BSD. runtimeInputs supplies gnused, so `-i ""` (the
        # macOS idiom) makes sed read "" as the script and the real script as a
        # filename. It fails with "can't read s/^...": invisible from a menu
        # click, and the toggle silently does nothing. Caught 2026-09-12.
        toggle) k="$2"; v=$(get "$k"); n=$([ "$v" = "1" ] && echo 0 || echo 1)
                sed -i "s/^$k=.*/$k=$n/" "$CFG"; exit 0 ;;
      esac

      if [ "$(get poll)" = "1" ]; then
        state=$(${amonDinStatus}/bin/amon-din-status)
      else
        state="unknown"
      fi

      # SF Symbols keep the title a glyph rather than text. Four states is the
      # most a menu bar glyph can carry legibly; "verifying" and "waking" collapse
      # into one because the user cannot act differently on them.
      case "$state" in
        ready)   echo ":flame.fill: | sfcolor=orange" ;;
        busy)    echo ":gamecontroller.fill: | sfcolor=green" ;;
        waking)  echo ":flame: | sfcolor=yellow" ;;
        asleep)  echo ":moon.zzz: | sfcolor=secondaryLabelColor" ;;
        *)       echo ":questionmark.circle: | sfcolor=secondaryLabelColor" ;;
      esac

      echo "---"
      case "$state" in
        ready)   echo "Nardol is awake and free" ;;
        busy)    echo "Nardol is streaming a session" ;;
        waking)  echo "Nardol is waking..." ;;
        asleep)  echo "Nardol is asleep" ;;
        *)       echo "Status polling is off" ;;
      esac
      echo "---"

      echo "Play | bash=${nardolPlay}/bin/amon-din terminal=false refresh=true"
      echo "Wake only | bash=${nardolPlay}/bin/amon-din param1=--no-launch terminal=false refresh=true"
      echo "---"
      # Sleeping used to sit behind a "Sleep now" -> "Confirm sleep" submenu,
      # on the reasoning that it is the only destructive action in the list.
      # Flattened by request 2026-09-17: it is one click now.
      #
      # ⚠️ The confirmation was the only thing standing between a stray cursor
      # and a suspended host. What remains is the busy guard below and the fact
      # that the cost is ~7s of `amon-din` to undo — which is the trade that was
      # accepted. Do not restore the submenu without asking.
      if [ "$state" = "busy" ]; then
        echo "Sleep | color=secondaryLabelColor"
        echo "--Cannot sleep during a session"
      else
        echo "Sleep | bash=${sleepNow}/bin/amon-din-sleep terminal=false refresh=true"
      fi
      echo "---"
      echo "Preferences"
      # ⛔ THE LIST COMES FROM lib/inference-profiles.nix, WHICH NARDOL ALSO
      # READS. A menu that offers a model the host cannot serve is a bug report
      # from the future; sharing the data is what prevents it. See that file.
      echo "--Model: $(model_label "$MODEL")"
      ${lib.concatStringsSep "\n" (
        # Default first, then the rest alphabetically. Attribute sets are
        # alphabetical, which put "(rollback)" above the model actually served.
        map (name: ''
          echo "----${profiles.profiles.${name}.label} $([ "$MODEL" = "${name}" ] && echo '✓') | bash=${amonDinModel}/bin/amon-din-model param1=${name} terminal=false refresh=true"
          echo "----${profiles.profiles.${name}.summary} | color=secondaryLabelColor size=11"'')
          (
            [ profiles.default ] ++ (lib.remove profiles.default (lib.attrNames profiles.profiles))
          )
      )}
      echo "-----"
      echo "----Switching restarts the server; loading takes 30-90s | color=secondaryLabelColor size=11"
      echo "--Launch Moonlight after Play $([ "$(get launch_moonlight)" = 1 ] && echo '✓') | bash=$SELF param1=toggle param2=launch_moonlight terminal=false refresh=true"
      echo "--Show notifications $([ "$(get notify)" = 1 ] && echo '✓') | bash=$SELF param1=toggle param2=notify terminal=false refresh=true"
      echo "--Poll status $([ "$(get poll)" = 1 ] && echo '✓') | bash=$SELF param1=toggle param2=poll terminal=false refresh=true"
      echo "-----"
      echo "--Host: $(get host) | bash=/usr/bin/open param1=-t param2=$CFG terminal=false"
      echo "--Edit config to retarget another machine | color=secondaryLabelColor"
      echo "--Status uses HTTP only, never SSH | color=secondaryLabelColor"
      echo "---"
      echo "Refresh now | refresh=true"
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
    runtimeInputs = with pkgs; [
      openssh
      gnused
      coreutils
    ];
    text = ''
      set -euo pipefail
      WANT="''${1:?usage: amon-din-model <profile>}"
      CFG="$HOME/.config/amon-din/config"
      notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Amon Dîn\"" >/dev/null 2>&1 || true; }

      notify "Switching to $WANT..."
      # ⛔ WRITE THE LOCAL CHOICE ONLY AFTER THE HOST ACCEPTS IT. The menu's
      # checkmark is read from this file, so recording first would show a
      # checkmark against a model nardol refused — the "silent lie" that
      # `nardol-model` validates its argument to avoid.
      if out=$(ssh -o ConnectTimeout=6 -o BatchMode=yes "${sshUser}@${nardolIp}" \
                 "sudo nardol-model switch $WANT" 2>&1); then
        if [ -f "$CFG" ] && grep -q '^model=' "$CFG"; then
          sed -i "s/^model=.*/model=$WANT/" "$CFG"
        else
          printf 'model=%s\n' "$WANT" >> "$CFG"
        fi
        notify "$out"
      else
        /usr/bin/osascript -e "display alert \"Could not switch model\" message \"$out\"" >/dev/null 2>&1 || true
        exit 1
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
  amon-din-model = amonDinModel;
  nardol-local-seat-probe = nardolLocalSeatProbe;
}
