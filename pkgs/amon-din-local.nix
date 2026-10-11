# Amon Dîn, local seat: a model served on dol-amroth itself, for opencode's
# subagents, so nardol's context goes to the main agent.
#
# Qwen3.6-35B-A3B (3B active per token) on llama.cpp. Chosen by measurement on
# this Mac (M2 Max 38-core, 32 GB) on 2026-10-10, needle recall at each depth:
#
#   engine / build                     write tok/s 8k/32k/54k   deepest 8/8   warm agent turn
#   llama.cpp, IQ4_XS (this file)      65-69 / 56-58 / 49-50    94k (117k 7-8/8)   ~3 s
#   MLX in LM Studio 0.4.26, 4-bit     83 / 73 / CRASH          ~46k, then Metal OOM unloads it
#   Qwen3.8-27B dense, either engine   13-21 tok/s; 4 min to read 30k — too slow on 400 GB/s
#
# A dense 27B reads all ~16 GB of weights per token; the MoE reads ~3B worth,
# which is the only thing that is fast on this memory bandwidth.
#
# ⛔ STATUS MUST NEVER WAKE THE MODEL. --sleep-idle-seconds frees all ~19 GiB
# when idle, and some endpoints load it straight back: GET /slots reloaded
# 18 GiB in testing (2026-10-10). A menu polling such an endpoint every minute
# would pin the model in memory for good while looking like it worked — the same
# trap amon-din.nix guards against for nardol with "status never uses SSH".
# GET /health and GET /props do NOT wake it, and /props reports is_sleeping.
# `status` reads only those two, plus ps.
#
# ⛔ PACKAGED, NOT A nix-darwin SERVICE, for the reason at the top of
# amon-din.nix: dol-amroth does not activate nix-darwin. `amon-din-local install`
# writes a user LaunchAgent that runs `amon-din-local-serve` from the nix
# profile, so a profile upgrade changes the server on its next restart without
# rewriting the plist.
{
  pkgs,
  lib ? pkgs.lib,
  label ? "io.saldivar.llama-server",
  port ? 8080,
  alias ? "qwen36-heretic",
  menuName ? "Qwen3.6 heretic",
  # Relative to $HOME; `local_model=` in ~/.config/amon-din/config overrides it.
  # llmfan46's Heretic v1.2.0 abliteration (KL 0.0015 against the original,
  # refusals 83/100 -> 10/100), imatrix IQ4_XS by mradermacher. Not in the store:
  # 18.7 GB, fetched once with `hf download` or LM Studio.
  model ? ".lmstudio/models/mradermacher/Qwen3.6-35B-A3B-uncensored-heretic-i1-GGUF/Qwen3.6-35B-A3B-uncensored-heretic.i1-IQ4_XS.gguf",
  # 131072 held 7-8/8 needles at 117k with no swap growth; KV costs 20 KiB per
  # token because only 10 of the 40 layers keep one (2.5 GiB at 128k).
  ctx ? 131072,
  sleepIdleSeconds ? 600,
  curl ? pkgs.curl,
}:
let
  # ⛔ PINNED TO THE BUILD MEASURED ABOVE (b11429, d812350). The fleet's
  # nixpkgs carries b9190, which predates --load-mode, the context-checkpoint
  # flags and the sleep fixes this relies on. Re-measure before moving it.
  llamaCpp = pkgs.llama-cpp.overrideAttrs (
    finalAttrs: old: {
      version = "11429";
      src = pkgs.fetchFromGitHub {
        owner = "ggml-org";
        repo = "llama.cpp";
        tag = "b${finalAttrs.version}";
        hash = "sha256-r/9cBgKB6P1XxNGv41Q/+ePgtM5X5uC++MR4+LCm1kY=";
        leaveDotGit = true;
        postFetch = ''
          git -C "$out" rev-parse --short HEAD > $out/COMMIT
          find "$out" -name .git -print0 | xargs -0 rm -rf
        '';
      };
      npmDepsHash = "sha256-a17M+L3nLdRnN6WMB6imPFmwqG2g8uv+gwN0XTAUrf8=";
    }
  );

  # What launchd runs. Every flag was in force for the measurements above.
  serve = pkgs.writeShellApplication {
    name = "amon-din-local-serve";
    runtimeInputs = with pkgs; [
      coreutils
      gnused
    ];
    text = ''
      CFG="$HOME/.config/amon-din/config"
      pref() { [ -f "$CFG" ] && sed -n "s/^$1=//p" "$CFG" | head -1 || true; }
      MODEL=$(pref local_model); MODEL=''${MODEL:-$HOME/${model}}
      CTX=$(pref local_ctx); CTX=''${CTX:-${toString ctx}}
      SLEEP=$(pref local_sleep); SLEEP=''${SLEEP:-${toString sleepIdleSeconds}}
      [ -f "$MODEL" ] || { echo "amon-din-local-serve: no model at $MODEL" >&2; exit 1; }
      args=(
        -m "$MODEL" -a ${alias}
        --host 127.0.0.1 --port ${toString port}
        -c "$CTX" -np 1 -ngl 999 -fa on --jinja
        # Real allocations, not mmap, so Activity Monitor shows what it costs.
        --load-mode none
        # Hybrid (Gated DeltaNet) layers cannot rewind, so prefix reuse needs
        # checkpoints; 1024-token spacing suits agent turns of ~1.5k tokens.
        # Measured: 26k-token cold read 33 s, each later turn ~3 s.
        --cache-ram 2048 -ctxcp 16 -cms 1024
        # Thinking off unless a request asks (opencode's mac/heretic-think does).
        --reasoning-format deepseek --chat-template-kwargs '{"enable_thinking":false}'
        # Qwen3.6 non-thinking sampling. No presence penalty: it fights the
        # repeated identifiers code is made of (see ikSampling, inference.nix).
        --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0
        --sleep-idle-seconds "$SLEEP"
      )
      exec ${llamaCpp}/bin/llama-server "''${args[@]}"
    '';
  };

  # The control surface for the menu and the shell. No llama.cpp dependency, so
  # the contract check can build it cheaply.
  control = pkgs.writeShellApplication {
    name = "amon-din-local";
    runtimeInputs = [
      curl
      pkgs.coreutils
      pkgs.gawk
      pkgs.gnused
      pkgs.jq
    ];
    text = ''
      LABEL=${label}
      PORT=${toString port}
      PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
      LOG="$HOME/Library/Logs/llama-server.log"
      CFG="$HOME/.config/amon-din/config"
      DOMAIN="gui/$(id -u)"
      URL="http://127.0.0.1:$PORT"

      loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
      # bootout returns before the job is gone; a bootstrap in that window fails
      # with "5: Input/output error" (hit 2026-10-10). Wait it out.
      unload() {
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        for _ in $(seq 1 30); do loaded || return 0; sleep 0.5; done
        echo "amon-din-local: $LABEL did not unload" >&2; return 1
      }
      pid() { launchctl print "$DOMAIN/$LABEL" 2>/dev/null | sed -n 's/^[[:space:]]*pid = \([0-9]*\).*/\1/p' | head -1; }

      # off | starting | asleep | awake | busy. Never wakes the model (see top).
      state() {
        loaded || { echo off; return; }
        local p; p=$(pid)
        [ -n "$p" ] || { echo starting; return; }
        curl -sf --max-time 2 "$URL/health" >/dev/null 2>&1 || { echo starting; return; }
        if [ "$(curl -s --max-time 2 "$URL/props" | jq -r '.is_sleeping // empty' 2>/dev/null)" = true ]; then
          echo asleep; return
        fi
        # Generation keeps a core busy; idle-but-loaded sits near zero.
        local cpu; cpu=$(ps -o %cpu= -p "$p" 2>/dev/null | awk '{printf "%d", $1}')
        if [ "''${cpu:-0}" -ge 20 ]; then echo busy; else echo awake; fi
      }
      # footprint, not ps: RSS misses Metal's GPU buffers (5.6 GB shown for a
      # 21 GB footprint, 2026-10-10).
      mem() { local p; p=$(pid); [ -n "$p" ] && footprint -p "$p" 2>/dev/null | sed -n 's/.*Footprint: \([0-9.]* [KMG]B\).*/\1/p' | head -1; }

      plist() {
        cat <<EOF
      <?xml version="1.0" encoding="UTF-8"?>
      <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
      <plist version="1.0">
      <dict>
        <key>Label</key><string>$LABEL</string>
        <key>ProgramArguments</key>
        <array><string>$HOME/.nix-profile/bin/amon-din-local-serve</string></array>
        <key>RunAtLoad</key><true/>
        <key>KeepAlive</key><true/>
        <key>ProcessType</key><string>Interactive</string>
        <key>StandardOutPath</key><string>$LOG</string>
        <key>StandardErrorPath</key><string>$LOG</string>
      </dict>
      </plist>
      EOF
      }

      case "''${1:-status}" in
        install)
          [ -x "$HOME/.nix-profile/bin/amon-din-local-serve" ] || {
            echo "install the server first: nix profile install <flake>#amon-din-local-serve" >&2; exit 1; }
          mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
          unload
          plist > "$PLIST"
          launchctl bootstrap "$DOMAIN" "$PLIST"
          echo "installed $PLIST" ;;
        uninstall) unload; rm -f "$PLIST"; echo "removed $LABEL" ;;
        start) loaded || launchctl bootstrap "$DOMAIN" "$PLIST" ;;
        # Until the next login, which loads everything in ~/Library/LaunchAgents.
        stop) unload ;;
        restart) launchctl kickstart -k "$DOMAIN/$LABEL" ;;
        status) state ;;
        memory) mem ;;
        name) echo ${lib.escapeShellArg menuName} ;;
        installed) [ -f "$PLIST" ] ;;
        # A one-token request: the only deliberate way to load the model early.
        wake)
          curl -s --max-time 180 "$URL/v1/chat/completions" -H 'content-type: application/json' \
            -d '{"model":"${alias}","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' >/dev/null ;;
        log) /usr/bin/open -a Console "$LOG" ;;
        # Remote opencode sessions die when the Mac sleeps. "on" disables system
        # sleep on AC power only (battery and lid-close unchanged) and remembers
        # the old value for "off". pmset needs root, so the menu asks via osascript.
        keep-awake)
          cur=$(pmset -g custom | awk '/^AC Power/{ac=1} ac && /^ sleep /{print $2; exit}')
          case "''${2:-status}" in
            status) [ "$cur" = 0 ] && echo on || echo off ;;
            on)
              [ "$cur" = 0 ] && exit 0
              mkdir -p "$(dirname "$CFG")"; touch "$CFG"
              sed -i '/^ac_sleep_before=/d' "$CFG"; echo "ac_sleep_before=''${cur:-1}" >> "$CFG"
              /usr/bin/osascript -e 'do shell script "/usr/bin/pmset -c sleep 0" with administrator privileges' ;;
            off)
              before=$(sed -n 's/^ac_sleep_before=//p' "$CFG" 2>/dev/null | head -1)
              before=''${before:-10}
              [[ "$before" =~ ^[0-9]+$ ]] || before=10
              /usr/bin/osascript -e "do shell script \"/usr/bin/pmset -c sleep $before\" with administrator privileges" ;;
            *) echo "usage: amon-din-local keep-awake [status|on|off]" >&2; exit 2 ;;
          esac ;;
        *) echo "usage: amon-din-local [status|memory|name|wake|start|stop|restart|install|uninstall|log|keep-awake]" >&2; exit 2 ;;
      esac
    '';
  };
in
{
  amon-din-local = control;
  amon-din-local-serve = serve;
  # Exposed so the contract check and a curious operator can see the pin.
  llama-cpp-local = llamaCpp;
}
