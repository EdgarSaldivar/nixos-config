# edoras — the always-on inference endpoint: ONE fixed vLLM profile on the
# RTX 3090, reachable only through the tailnet.
#
# The profile is `qwen3.5-9b-fp8-batch` (D1b), the PinCollector benchmark pick
# of 2026-11-11 (nixos-config #163; the measurements are recorded beside it in
# lib/inference-profiles.nix): Qwen3.5-9B with vision, 32 requests at once,
# 16k each, prefix caching. Unlike nardol there is no model menu, no gaming
# arbitration and no wake lease: this host serves one thing and stays up.
# Changing the model is editing `profileName` and measuring again.
#
# ⛔ NOTHING LISTENS ON THE LAN. Docker publishes on 127.0.0.1 only, and
# `tailscale serve` terminates HTTPS for https://edoras.<tailnet>.ts.net and
# proxies to it over loopback. Docker-published ports bypass the NixOS
# firewall (nardol's port 8000 answers on its LAN that way), and this box sits
# on someone else's network. HTTPS is also what PinCollector's Suggest-details
# reader requires. The tailnet must have MagicDNS and HTTPS certificates
# enabled, which minas's Dungeon Scriber serve already depends on. Serve also
# forwards the tailnet address's port 8000, raw TCP, to the same loopback port
# (vllm-tailnet-serve below), for a client given the address instead of the
# name.
#
# ⛔ THE 3090 ONLY, BY UUID. The container gets exactly one CDI device,
# nvidia.com/gpu=<3090 UUID>; the A4000 is left for its own workloads. CDI
# names are UUIDs on this host (device-name-strategy below), so a PCI
# renumbering cannot hand the model the wrong card.
#
# Starts are offline (HF_HUB_OFFLINE, from lib/inference-engine.nix), so the
# checkpoint must be downloaded once beforehand; until it is, the unit is
# skipped rather than restarting forever. See docs/runbooks/edoras/install.md.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  profileName = "qwen3.5-9b-fp8-batch";
  profile = (import ../../../lib/inference-profiles.nix).profiles.${profileName};
  engine = import ../../../lib/inference-engine.nix { inherit lib; };

  rtx3090 = "GPU-fe6604a2-e3a8-7eb0-2716-8fb9d8327cba";
  stateDir = "/srv/inference";
  containerName = "vllm";
  modelCacheDir = "${stateDir}/hub/models--${lib.replaceStrings [ "/" ] [ "--" ] profile.model}";

  # The host's values for fields the profile leaves null, named as nardol's
  # `nardol.inference` options so lib/inference-engine.nix reads both alike.
  # Everything matches nardol except the KV cache.
  defaults = {
    model = null;
    quantization = null;
    # ⛔ "auto" (the model's own precision), NOT nardol's fp8. The 3090 is
    # Ampere: it has no FP8 hardware. FP8 *weights* still work there (vLLM
    # falls back to weight-only Marlin kernels, which gpu-unlock speeds up),
    # but an FP8 KV cache for this hybrid model on Ampere is unverified. A
    # full-precision cache is never less accurate than the measured fp8 one;
    # it only holds fewer tokens, and lookups are 2-3k tokens against 16k.
    # Measured 2026-11-15 at 0.90: 172,222 KV tokens, 10.5 full 16k requests.
    kvCacheDtype = "auto";
    gpuMemoryUtilization = 0.90;
    enforceEager = false;
    toolCallParser = "qwen3_xml";
    chatTemplateKwargs = ''{"enable_thinking": false}'';
    # Off here and back on through the profile's own --enable-prefix-caching,
    # exactly as on nardol (the last flag wins).
    enablePrefixCaching = false;
    maxCudagraphCaptureSize = null;
    extraArgs = [ ];
    # The digest nardol pins, so both hosts run the image the benchmark ran.
    image = "vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40";
    inherit stateDir;
  };

  runArgs = [
    "run"
    "--name=${containerName}"
    "--log-driver=journald"
    "--rm"
    "--pull"
    "missing"
    "--device=nvidia.com/gpu=${rtx3090}"
    # ⛔ AMPERE NEEDS THIS, OR THE MODEL NEVER STARTS. vLLM 0.30's online FP8
    # path selects CutlassFP8ScaledMMLinearKernel on the 3090 regardless of FP8
    # hardware, and the first forward pass dies in cutlass_scaled_mm_sm80
    # (observed 2026-11-15; VLLM_TEST_FORCE_FP8_MARLIN=1 did not change the
    # choice). Disabling that kernel by name makes it select
    # MarlinFP8ScaledMMLinearKernel, weight-only FP8, which vLLM itself names as
    # the path for GPUs without FP8 compute. nardol's Ada card never needs it.
    "-e"
    "VLLM_DISABLED_KERNELS=CutlassFP8ScaledMMLinearKernel"
  ]
  ++ engine.vllmContainerArgs defaults "127.0.0.1:8000" profile;
in
{
  assertions = [
    {
      assertion = profile.engine == "vllm";
      message = "edoras serves vLLM profiles only; ${profileName} is ${profile.engine}";
    }
  ];

  virtualisation.docker = {
    enable = true;
    daemon.settings.log-driver = "journald";
    autoPrune = {
      enable = true;
      dates = "weekly";
      flags = [ "--filter=until=720h" ];
    };
  };

  hardware.nvidia-container-toolkit.device-name-strategy = "uuid";

  systemd.tmpfiles.rules = [ "d ${stateDir} 0750 root root -" ];

  systemd.services."docker-${containerName}" = {
    description = "vLLM: ${profile.label}, on the RTX 3090";
    wantedBy = [ "multi-user.target" ];
    wants = [
      "network-online.target"
      "rtx3090-power-cap.service"
    ];
    after = [
      "docker.service"
      "docker.socket"
      "network-online.target"
      # Never load a model onto the card before its 300 W cap is in place.
      "rtx3090-power-cap.service"
    ];
    requires = [ "docker.service" ];
    unitConfig.ConditionPathIsDirectory = "${modelCacheDir}/snapshots";
    path = [ config.virtualisation.docker.package ];
    preStart = "docker rm -f ${containerName} || true";
    script = "exec docker ${lib.escapeShellArgs runArgs}";
    preStop = "docker stop ${containerName} || true";
    postStop = "docker rm -f ${containerName} || true";
    # [Unit] options, not serviceConfig: see nardol's inference.nix for how
    # that mistake once left its rate limit inert.
    startLimitBurst = 5;
    startLimitIntervalSec = 600;
    serviceConfig = {
      Restart = "on-failure";
      RestartSec = "30s";
      # A first start pulls the image and compiles CUDA graphs.
      TimeoutStartSec = 0;
      TimeoutStopSec = 120;
    };
  };

  # Tailnet HTTPS in front of the loopback port, the same shape as minas's
  # dungeon-scriber-tailnet-serve: a oneshot that re-asserts the serve config,
  # which persists in tailscaled's state, and turns it off when removed.
  #
  # And a raw TCP forward of tailnet port 8000 to the same loopback port, for
  # PinCollector's sheet reader, http://100.80.226.32:8000/v1 (the shape
  # nardol's port 8000 has: plain HTTP inside WireGuard). It is an address
  # because the MagicDNS name would write the tailnet's name into this public
  # repository. tailscaled itself listens on 8000, as on 443, bound to the
  # tailnet addresses only (100.80.226.32 and its fd7a: address; never
  # 0.0.0.0 or the LAN address). Its ts-input chain accepts tailscale0 ahead of
  # nixos-fw, and nixos-fw doesn't open 8000 on any other interface, so a LAN
  # packet addressed to 100.80.226.32:8000 is dropped. Which peers may connect
  # is the tailnet policy's call (the admin console), the same as for 443.
  # HTTPS first, so a failing TCP forward leaves it up; stopping removes both.
  systemd.services.vllm-tailnet-serve = {
    description = "Serve vLLM over tailnet HTTPS and tailnet TCP 8000";
    wantedBy = [ "multi-user.target" ];
    requires = [ "tailscaled.service" ];
    after = [ "tailscaled.service" ];
    partOf = [ "tailscaled.service" ];
    path = [ pkgs.tailscale ];
    script = ''
      set -eu
      tailscale serve --bg --https=443 http://127.0.0.1:8000
      tailscale serve --bg --tcp=8000 tcp://127.0.0.1:8000
    '';
    preStop = ''
      tailscale serve --tcp=8000 off || true
      tailscale serve --https=443 off || true
    '';
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      Restart = "on-failure";
      RestartSec = "10s";
    };
  };
}
