# The ONE answer to "may the Dungeon Scriber worker hold the GPU right now?"
#
# The worker's ExecCondition, its guard and its resume timer all ask this
# script, so the three can never disagree about what "a game needs the GPU"
# means. It prints a reason and exits 1 when the worker must yield, and exits 0
# only when every probe positively said the GPU is free for it.
#
# ⛔ EVERY UNCERTAIN ANSWER MEANS "YIELD". A probe that errors, times out or
# returns something unparseable is a reason, never silence — the same rule
# ./idle-suspend.nix applies, for the same reason: the one unacceptable outcome
# is a game that stalls because a batch job kept VRAM it should have released.
#
# Every tool is an absolute path argument so checks/ can run this exact script
# against fakes on any platform.
{
  lib,
  writeShellApplication,
  coreutils,
  gawk,
  jq,
  curl,
  systemctl,
  docker,
  nvidiaSmi,
  container,
  gamingUnit,
  yieldUnits,
  wolfUnit,
  wolfSocket,
  thresholdMiB,
}:
writeShellApplication {
  name = "dungeon-scriber-gpu-gate";
  runtimeInputs = [
    coreutils
    gawk
    jq
  ];
  text = ''
    yield_units=(${lib.escapeShellArgs yieldUnits})

    unit_state() {
      ${systemctl} show --property=ActiveState --value "$1" 2>/dev/null || echo unknown
    }

    reason() {
      local state unit body count own_pids total apps own
      # 1. The gaming target is the explicit claim. amon-din starts it before
      #    Moonlight connects, and ANY state but inactive — activating included,
      #    which is the window the GPU handover is waiting in — belongs to it.
      state=$(unit_state ${lib.escapeShellArg gamingUnit})
      if [ "$state" != inactive ]; then
        echo "${gamingUnit} is $state"
        return 0
      fi

      # 2. Units that outrank the worker (inference). A unit that is starting
      #    is about to load weights, so yield before it tries rather than after
      #    it has run out of memory.
      for unit in "''${yield_units[@]}"; do
        state=$(unit_state "$unit")
        case "$state" in
          inactive | failed) ;;
          *)
            echo "$unit is $state"
            return 0
            ;;
        esac
      done

      # 3. Wolf's own view of sessions and lobbies. A Moonlight client can reach
      #    Wolf without anyone starting the gaming target; this is what catches
      #    it before the game has allocated anything.
      if [ -S ${lib.escapeShellArg wolfSocket} ]; then
        for kind in sessions lobbies; do
          if ! body=$(${curl} -sf --max-time 5 --unix-socket ${lib.escapeShellArg wolfSocket} \
                        "http://localhost/api/v1/$kind" 2>/dev/null); then
            echo "Wolf $kind query failed"
            return 0
          fi
          # A missing or non-array field is UNKNOWN, not zero: `null | length`
          # is 0 in jq, which would read a schema change as "no sessions".
          if ! count=$(printf '%s' "$body" \
                         | jq -e --arg k "$kind" '.[$k] | if type == "array" then length else error("not an array") end' \
                         2>/dev/null); then
            echo "Wolf $kind response unparseable"
            return 0
          fi
          if [ "$count" != 0 ]; then
            echo "Wolf reports $count $kind"
            return 0
          fi
        done
      elif [ "$(unit_state ${lib.escapeShellArg wolfUnit})" != inactive ]; then
        echo "${wolfUnit} is up but ${wolfSocket} is missing"
        return 0
      fi

      # 4. The measured rule from the hand-run safety monitor: memory held by
      #    anything OTHER than this worker's own processes must stay under the
      #    threshold. The worker's own allocation never counts against it.
      own_pids=""
      if body=$(${docker} top ${lib.escapeShellArg container} -eo pid 2>/dev/null); then
        own_pids=$(printf '%s\n' "$body" | awk 'NR > 1 { printf " %s", $1 }')
      fi
      if ! body=$(${nvidiaSmi} --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null); then
        echo "nvidia-smi memory query failed"
        return 0
      fi
      total=$(printf '%s\n' "$body" | awk '$1 ~ /^[0-9]+$/ { s += $1; n++ } END { if (n) print s; else print "none" }')
      if [ "$total" = none ]; then
        echo "nvidia-smi reported no GPU memory figure"
        return 0
      fi
      if ! apps=$(${nvidiaSmi} --query-compute-apps=pid,used_gpu_memory --format=csv,noheader,nounits 2>/dev/null); then
        echo "nvidia-smi compute-app query failed"
        return 0
      fi
      # An [N/A] figure sums as 0, which UNDER-counts our own use and so errs
      # towards yielding.
      own=$(printf '%s\n' "$apps" \
              | awk -F', *' -v pids="$own_pids " 'index(pids, " " $1 " ") { s += $2 } END { print s + 0 }')
      if [ $((total - own)) -gt ${toString thresholdMiB} ]; then
        echo "$((total - own)) MiB of GPU memory is held by other workloads (limit ${toString thresholdMiB} MiB)"
        return 0
      fi
      return 0
    }

    why=$(reason) || why="gate probe failed"
    if [ -n "$why" ]; then
      echo "worker must yield: $why"
      exit 1
    fi
    echo "GPU clear for the worker"
  '';
}
