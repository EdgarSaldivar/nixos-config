# Who owns the GPU right now.
#
# ⚠️ THE EXCLUSIVITY IS WITH A SESSION, NOT WITH WOLF.
#
# The obvious model — "Wolf and vLLM conflict" — is wrong, and measuring says
# so: with docker-wolf active and no session, nvidia-smi reports 24 MiB used.
# Wolf idle costs no VRAM. Making them conflict would mean inference could only
# run with game streaming switched off entirely, so a Moonlight client could
# never connect without a manual step on the host. That defeats the point of
# waking on demand.
#
# What actually cannot coexist is a RUNNING GAME and a loaded model: 24 GB holds
# one or the other, and consumer cards have no MIG to partition with.
#
# So Wolf stays up always, and nardol-gaming.target is the thing that conflicts
# with inference. `amon-din` starts that target before launching Moonlight; the
# idle loop drops it when the session ends and vLLM comes back on its own.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.nardol.inference;
  # The systemd unit for whichever container the engine option selected.
  inferenceUnit =
    {
      vllm = "docker-vllm.service";
      llama-cpp = "docker-llamacpp.service";
      ik-llama = "docker-ikllama.service";
    }
    .${cfg.engine};
in
lib.mkIf cfg.enable {
  systemd.targets.nardol-gaming = {
    description = "Nardol is in gaming mode; the GPU belongs to Wolf";
    # Conflicts stops vLLM when this target starts, and — crucially — starting
    # the target WAITS for that stop to complete before the target is reached.
    conflicts = [ inferenceUnit ];
    after = [ inferenceUnit ];
  };

  # ⛔ Conflicts= ALONE IS NOT A HANDOFF.
  #
  # It stops the unit, but "stopped" is not "the GPU is free": the container can
  # be gone while the CUDA context is still tearing down and VRAM is still
  # allocated. Starting a game into that state is how you get an out-of-memory
  # failure that looks like a game bug.
  #
  # This waits for the driver to actually report the memory back, and fails
  # rather than proceeding on a timeout — a gaming session that refuses to start
  # is recoverable, a half-released GPU is a confusing mess.
  systemd.services.nardol-gpu-handover = {
    description = "Prove the GPU is actually free before gaming starts";
    requiredBy = [ "nardol-gaming.target" ];
    before = [ "nardol-gaming.target" ];
    after = [ inferenceUnit ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = false;
      TimeoutStartSec = "120s";
    };
    script = ''
      set -eu
      smi=${config.hardware.nvidia.package.bin}/bin/nvidia-smi

      # Wait for every compute process to disappear. Checking processes rather
      # than free bytes because a graphics context can hold memory without being
      # a compute app, and it is the compute apps that vLLM leaves behind.
      for i in $(seq 1 60); do
        procs=$($smi --query-compute-apps=pid --format=csv,noheader | ${pkgs.gnugrep}/bin/grep -c . || true)
        used=$($smi --query-gpu=memory.used --format=csv,noheader,nounits)
        if [ "$procs" = "0" ] && [ "$used" -lt 1024 ]; then
          echo "GPU released: $used MiB in use, no compute processes"
          exit 0
        fi
        echo "waiting for GPU: $procs compute process(es), $used MiB still allocated"
        sleep 2
      done

      echo "GPU still held after 120s; refusing to hand it to gaming" >&2
      $smi --query-compute-apps=pid,process_name,used_memory --format=csv >&2
      exit 1
    '';
  };

  # ⛔ Conflicts= STOPS INFERENCE AND NOTHING EVER BRINGS IT BACK.
  #
  # This file used to claim inference "comes back on its own" when the session
  # ends. It does not, and exercising the handover end to end on 2026-09-13
  # proved it: after `systemctl stop nardol-gaming.target`, docker-ikllama sat
  # inactive indefinitely and had to be started by hand. Every Home Assistant
  # request after a gaming session would simply fail, silently, until a human
  # noticed.
  #
  # systemd has no "on stop, start that other thing", so this is the idiomatic
  # shape: a unit that is PartOf the target, whose ExecStop runs when the target
  # goes away.
  systemd.services.nardol-inference-restore = {
    description = "Bring ${cfg.engine} back when gaming mode ends";
    partOf = [ "nardol-gaming.target" ];
    wantedBy = [ "nardol-gaming.target" ];
    # ⛔ Before=, NOT After=, AND THE DIFFERENCE IS THE WHOLE MECHANISM.
    # Stop order is the reverse of start order. With After=, this unit stopped
    # BEFORE the target, so at ExecStop time the target still read "active" and
    # the is-active guard below bailed out every single time — the restore
    # silently never fired. Measured: inference stayed inactive for 200s after
    # leaving gaming. Before= makes this stop AFTER the target is already down,
    # so "is the target still active?" finally distinguishes "gaming ended"
    # from "somebody restarted this helper".
    before = [ "nardol-gaming.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = "${pkgs.coreutils}/bin/true";
      ExecStop = pkgs.writeShellScript "nardol-inference-restore" ''
        # ⛔ DO NOT RESURRECT INFERENCE DURING SHUTDOWN. Halting the machine also
        # stops the gaming target, which fires this ExecStop; without this guard
        # a reboot would start loading a 16 GB model on the way down.
        # ⛔ ALLOWLIST, NOT DENYLIST. This previously excluded stopping|offline
        # and started inference for everything else — including the empty string
        # a failed or unresponsive `is-system-running` returns, which is
        # fail-open at exactly the moment the manager is least healthy. It also
        # resurrected the model under `systemctl isolate rescue.target`, where
        # the manager stays "running" but the isolation meant to stop it.
        state=$(${pkgs.systemd}/bin/systemctl is-system-running 2>/dev/null || true)
        case "$state" in
          running | degraded) ;;
          *) exit 0 ;;
        esac

        # ⛔ ExecStop RUNS WHENEVER THIS UNIT STOPS, NOT ONLY WHEN GAMING ENDED.
        # `systemctl restart nardol-inference-restore`, or an activation
        # restarting it because the unit changed, both fire this hook. Starting
        # inference then trips the target's Conflicts= and TEARS DOWN A LIVE
        # GAMING SESSION from what looks like a harmless helper restart.
        if ${pkgs.systemd}/bin/systemctl is-active --quiet nardol-gaming.target; then
          exit 0
        fi
        # --no-block: this runs inside the target's own transaction, and waiting
        # on a unit that orders itself after that transaction deadlocks.
        exec ${pkgs.systemd}/bin/systemctl start --no-block ${inferenceUnit}
      '';
    };
  };

  # ⛔ A SERVING MODEL MUST BLOCK SUSPEND, or the idle loop sleeps the host
  # mid-request. ./idle-suspend.nix already treats any systemd sleep inhibitor
  # as a reason to stay awake, so this plugs straight into it.
  #
  # ⛔ BUT HOLDING IT FOR THE WHOLE UPTIME DEFEATS IDLE-SUSPEND ENTIRELY. The
  # container has autoStart, so a permanently-held inhibitor means the host can
  # never sleep — on a machine whose entire purpose is sleeping. That was the
  # old behaviour and it is why restoring inference after gaming could not be
  # wired up: "HA works after a game" and "the box sleeps" were mutually
  # exclusive.
  #
  # So hold it only while a request is actually in flight. The server publishes
  # /slots, where a slot with state != 0 is processing — no request-lifecycle
  # hook required, which is what made this impractical under vLLM.
  #
  # An idle-but-loaded model therefore does NOT keep the box awake, and that is
  # safe here specifically because suspend is S3 with the NVIDIA VRAM
  # preservation this host already verifies: the weights survive the sleep and
  # are still resident on resume. On a host without that, this would trade a
  # wasted idle for a 40-second reload on every wake.
  # ⛔ THE INHIBITOR MUST FOLLOW THE ENGINE, and for a while it did not.
  #
  # This was written bound to docker-vllm.service, before llama-cpp and
  # ik-llama existed as options. Switching engine left it inactive — verified
  # 2026-09-13, `systemctl is-active nardol-inference-inhibit` returning
  # inactive while ik-llama served happily — which means the idle loop saw no
  # inhibitor and was free to suspend the host in the middle of a request.
  # Naming the unit after the selected engine keeps them from drifting apart
  # again.
  systemd.services.nardol-inference-inhibit = {
    description = "Hold a sleep inhibitor while ${cfg.engine} has a request in flight";
    bindsTo = [ inferenceUnit ];
    after = [ inferenceUnit ];
    wantedBy = [ inferenceUnit ];
    serviceConfig = {
      Type = "simple";
      Restart = "always";
      RestartSec = "5s";
      ExecStart = pkgs.writeShellScript "nardol-inference-inhibit" ''
        set -u
        url="http://127.0.0.1:${toString cfg.port}/slots"

        # Keep the inhibitor for this long after the last observed activity.
        # Polling alone is racy: a request can arrive in the gap between two
        # polls. The idle loop needs 15 consecutive idle minutes before it
        # suspends anything, so a couple of minutes of hysteresis here costs no
        # real sleep and closes the window.
        grace=120

        # ⛔ THE HELD INHIBITOR IS TRACKED IN A VARIABLE, NEVER A PIDFILE.
        # A pidfile in /run outlives the process: systemd tears down the cgroup
        # on stop, so the child dies and the file remains, and with
        # Restart=always the next start would kill whatever pid had been
        # recycled onto that number — as root. The script is long-lived, so a
        # variable is both simpler and correct, and systemd reaps the child with
        # the rest of the cgroup.
        inhibitor=""

        release() {
          if [ -n "$inhibitor" ]; then
            kill "$inhibitor" 2>/dev/null || true
            inhibitor=""
          fi
        }
        trap 'release; exit 0' TERM INT

        last_busy=$(${pkgs.coreutils}/bin/date +%s)
        while :; do
          now=$(${pkgs.coreutils}/bin/date +%s)

          # ⛔ FAIL CLOSED, AND "jq SAID FALSE" IS NOT PROOF OF IDLE.
          # The first version of this only branched on curl's exit status, so a
          # 200 carrying truncated JSON, an HTML error page, null, or a future
          # /slots schema made jq fail — which read exactly like "all slots
          # idle" and dropped the inhibitor 120s later. The server is least
          # able to answer precisely when it is most loaded, so that inverted
          # the safety property the comment claimed.
          #
          # Three outcomes, and ONLY a positively-proven idle may age the lock.
          verdict=unknown
          if body=$(${pkgs.curl}/bin/curl -sf -m 3 "$url" 2>/dev/null); then
            verdict=$(printf '%s' "$body" | ${pkgs.jq}/bin/jq -r '
              if type == "array" and all(.[]; has("state") and (.state | type == "number"))
              then (if any(.[]; .state != 0) then "busy" else "idle" end)
              else "unknown" end' 2>/dev/null) || verdict=unknown
          fi
          case "$verdict" in
            idle) : ;;
            *) last_busy=$now ;;
          esac

          if [ $((now - last_busy)) -lt $grace ]; then
            # Re-take it if the child died for any reason, rather than assuming
            # a pid we once recorded is still holding anything.
            if [ -z "$inhibitor" ] || ! kill -0 "$inhibitor" 2>/dev/null; then
              ${pkgs.systemd}/bin/systemd-inhibit --what=sleep --who=${cfg.engine} --why=serving --mode=block ${pkgs.coreutils}/bin/sleep infinity &
              inhibitor=$!
            fi
          else
            release
          fi
          ${pkgs.coreutils}/bin/sleep 2
        done
      '';
    };
  };
}
