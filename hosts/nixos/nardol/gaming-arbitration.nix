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
in
lib.mkIf cfg.enable {
  systemd.targets.nardol-gaming = {
    description = "Nardol is in gaming mode; the GPU belongs to Wolf";
    # Conflicts stops vLLM when this target starts, and — crucially — starting
    # the target WAITS for that stop to complete before the target is reached.
    conflicts = [ "docker-vllm.service" ];
    after = [ "docker-vllm.service" ];
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
    after = [ "docker-vllm.service" ];
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

  # ⛔ A SERVING MODEL MUST BLOCK SUSPEND, or the idle loop sleeps the host
  # mid-request. ./idle-suspend.nix already treats any systemd sleep inhibitor
  # as a reason to stay awake, so this plugs straight into it.
  #
  # The inhibitor is held for as long as vLLM is UP, not per-request. Per-request
  # would be tighter but needs a hook into vLLM's request lifecycle that does not
  # exist; and the cost of the coarse version is only that an idle-but-loaded
  # model keeps the box awake. That is the wrong trade for a machine whose whole
  # point is sleeping, so ./inference.nix is expected to be stopped when gaming
  # and the idle loop still wins whenever inference is not running.
  systemd.services.nardol-inference-inhibit = {
    description = "Hold a sleep inhibitor while vLLM is serving";
    bindsTo = [ "docker-vllm.service" ];
    after = [ "docker-vllm.service" ];
    wantedBy = [ "docker-vllm.service" ];
    serviceConfig = {
      Type = "simple";
      ExecStart = "${pkgs.systemd}/bin/systemd-inhibit --what=sleep --who=vllm --why=serving --mode=block ${pkgs.coreutils}/bin/sleep infinity";
    };
  };
}
