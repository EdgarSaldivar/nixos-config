# One command from the Mac to a playable nardol.
#
# nardol is powered down or suspended when idle, so "start gaming" is a wake, a
# verification, and a launch. The verification is the part that is easy to get
# wrong, and every choice below is a measured one.
{ pkgs, lib, ... }:
let
  nardolIp = "10.0.0.118";
  nardolMac = "9c:6b:00:36:e0:e8";

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
  relayHost = "pelargir";

  nardolPlay = pkgs.writeShellApplication {
    name = "nardol-play";
    runtimeInputs = with pkgs; [
      openssh
      wakeonlan
      coreutils
    ];
    text = ''
      set -euo pipefail

      NARDOL=''${NARDOL:-${nardolIp}}
      MAC=''${MAC:-${nardolMac}}
      RELAY=''${RELAY:-${relayHost}}
      # S3 resume measured at 6-10s; S5 cold boot including Tang unlock at 58s.
      # 180 leaves headroom for a cold boot that also fscks or waits on DHCP.
      DEADLINE=''${DEADLINE:-180}
      LAUNCH=''${LAUNCH:-1}

      say() { printf '%s\n' "$*" >&2; }
      ssh_n() { ssh -o ConnectTimeout=4 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "edgar@$NARDOL" "$@"; }

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
  # A double-clickable app, because "run a terminal command" is not a way to
  # start a game. nix-darwin links anything under $out/Applications into
  # /Applications/Nix Apps, so this shows up in Finder, Spotlight and the Dock
  # like any other app and can be pinned there.
  #
  # It reports progress through notifications rather than a terminal window: the
  # wake takes ~7s from S3 and ~58s from a cold boot, which is long enough that
  # silence reads as failure.
  nardolPlayApp = pkgs.runCommand "nardol-play-app" { } ''
    app="$out/Applications/Play on Nardol.app"
    mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

    cat > "$app/Contents/Info.plist" <<'PLIST'
    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0">
    <dict>
      <key>CFBundleName</key><string>Play on Nardol</string>
      <key>CFBundleDisplayName</key><string>Play on Nardol</string>
      <key>CFBundleIdentifier</key><string>io.saldivar.nardol-play</string>
      <key>CFBundleVersion</key><string>1.0</string>
      <key>CFBundlePackageType</key><string>APPL</string>
      <key>CFBundleExecutable</key><string>nardol-play-app</string>
      <key>LSUIElement</key><true/>
    </dict>
    </plist>
    PLIST

    cat > "$app/Contents/MacOS/nardol-play-app" <<'SH'
    #!/bin/sh
    notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Play on Nardol\"" >/dev/null 2>&1; }
    notify "Waking nardol..."
    if out=$(${nardolPlay}/bin/nardol-play 2>&1); then
      notify "Ready. Launching Moonlight."
    else
      /usr/bin/osascript -e "display alert \"nardol is not ready\" message \"$out\"" >/dev/null 2>&1
      exit 1
    fi
    SH
    chmod +x "$app/Contents/MacOS/nardol-play-app"
  '';
in
{
  environment.systemPackages = [
    nardolPlay
    nardolPlayApp
  ];
}
