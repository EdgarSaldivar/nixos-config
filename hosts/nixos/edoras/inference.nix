# edoras — the always-on inference endpoint: ONE fixed vLLM profile on the
# RTX A4000, reachable only through the tailnet.
#
# The profile is `qwen3.5-9b-fp8-batch` (D1b), the PinCollector benchmark pick
# of 2026-11-11 (nixos-config #163; the measurements are recorded beside it in
# lib/inference-profiles.nix): Qwen3.5-9B with vision, 32 requests at once
# and 1 MP images (4 requests and 4 MP images on this host; see below),
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
# ⛔ THE A4000 ONLY, BY UUID. The container gets exactly one CDI device,
# nvidia.com/gpu=<A4000 UUID>; the 3090 is left for other GPU work. CDI names
# are UUIDs on this host (device-name-strategy below), so a PCI renumbering
# cannot hand the model the wrong card. The model served from the 3090 until
# 2026-11-20, when the owner gave the 3090 to other work and the model was
# moved here by a runtime override; this makes that move permanent.
#
# ⛔ 4 MP IMAGES, 4 REQUESTS AT ONCE (extraArgs below), not the profile's
# 1 MP and 32. The owner asked for the highest image quality the sheets
# allow. Measured on the A4000 at 0.90, 2026-11-21:
#   1 MP, 12 seqs: 29,719 KV tokens      3 MP, 12 seqs: 31,243
#   3 MP,  6 seqs: 32,386                4 MP,  4 seqs: 31,243
# so the bigger image cap costs no cache, and every start logged the same
# four recovered load warnings. On PinCollector's sheet eval, 4 MP read Paris
# posters 693/704 against 677/704 at 1 MP (where-sold 61/64 against 48/64)
# with no runtime out-of-memory. PinCollector sends its own cap with every
# request (1 MP for its 2026 panel reader, which was tuned at 1 MP; 4 MP for
# whole sheets; its PR #408), so this default sets what vLLM reserves memory
# for at startup and what a client sending no cap gets. A whole sheet (about
# 4.6k prompt tokens plus up to 9k
# answer) holds up to about 45% of the cache, and sheets are read one at a
# time, so 4 at once is plenty.
#
# ⛔ THE CARD MUST BE EMPTY WHEN THE MODEL STARTS. The load runs at the
# A4000's limit (2026-11-20: four allocator out-of-memory warnings while
# loading, all recovered; none on the 3090), so anything else holding a GB
# or two on the card can make it fail.
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

  a4000 = "GPU-e26e87e1-860f-0a89-7ff2-bb91cd5027a3";
  stateDir = "/srv/inference";
  containerName = "vllm";
  modelCacheDir = "${stateDir}/hub/models--${lib.replaceStrings [ "/" ] [ "--" ] profile.model}";

  # The host's values for fields the profile leaves null, named as nardol's
  # `nardol.inference` options so lib/inference-engine.nix reads both alike.
  # Everything matches nardol except the KV cache, the request cap and the
  # image cap (extraArgs).
  defaults = {
    model = null;
    quantization = null;
    # ⛔ "auto" (the model's own precision), NOT nardol's fp8. The A4000 is
    # Ampere (GA104, the 3090's generation): it has no FP8 hardware. FP8
    # *weights* still work there (vLLM falls back to weight-only Marlin
    # kernels), but an FP8 KV cache for this hybrid model on Ampere is
    # unverified. A full-precision cache is never less accurate than the
    # measured fp8 one; it only holds fewer tokens, and lookups are 2-3k tokens
    # against 16k. Measured at 0.90: on the A4000 with 4 sequences and 4 MP
    # images (2026-11-21), 31,243 KV tokens, 1.91 full 16k requests at once;
    # on the 3090 with 32 (2026-11-15), 172,222 and 10.5.
    kvCacheDtype = "auto";
    gpuMemoryUtilization = 0.90;
    enforceEager = false;
    toolCallParser = "qwen3_xml";
    chatTemplateKwargs = ''{"enable_thinking": false}'';
    # Off here and back on through the profile's own --enable-prefix-caching,
    # exactly as on nardol (the last flag wins).
    enablePrefixCaching = false;
    maxCudagraphCaptureSize = null;
    # Appended after the profile's own "--max-num-seqs 32" and its
    # "--mm-processor-kwargs" (max_pixels 1048576); the last flag wins, as
    # with prefix caching above. A repeated JSON flag replaces the whole
    # earlier object rather than merging keys, so a key the profile adds
    # later (min_pixels, say) must be repeated here. See the 4 MP note at
    # the top.
    extraArgs = [
      "--max-num-seqs"
      "4"
      "--mm-processor-kwargs"
      ''{"max_pixels":4194304}''
    ];
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
    "--device=nvidia.com/gpu=${a4000}"
    # ⛔ AMPERE NEEDS THIS, OR THE MODEL NEVER STARTS. vLLM 0.30's online FP8
    # path selects CutlassFP8ScaledMMLinearKernel on Ampere (seen on the 3090;
    # the A4000 runs with the same setting) regardless of FP8 hardware, and the first
    # forward pass dies in cutlass_scaled_mm_sm80
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
    description = "vLLM: ${profile.label}, on the RTX A4000";
    wantedBy = [ "multi-user.target" ];
    # No power-cap ordering: the 300 W cap (./power-cap.nix) is the 3090's, and
    # the A4000 is a 140 W card that needs none.
    wants = [ "network-online.target" ];
    after = [
      "docker.service"
      "docker.socket"
      "network-online.target"
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
