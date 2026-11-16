# Pure renderers for serving one inference profile (lib/inference-profiles.nix)
# with vLLM in Docker, shared by every host that serves one:
#   nardol  menu-switched profiles     hosts/nixos/nardol/inference.nix
#   edoras  one fixed profile          hosts/nixos/edoras/inference.nix
#
# `cfg` supplies the host's values for whatever a profile leaves null, under the
# same names as nardol's `nardol.inference` options: model, quantization,
# kvCacheDtype, gpuMemoryUtilization, enforceEager, toolCallParser,
# chatTemplateKwargs, enablePrefixCaching, maxCudagraphCaptureSize, extraArgs,
# image and stateDir.
#
# Moved verbatim out of nardol's module, comments included, when edoras became
# the second host to serve a profile; closure-equiv proved nardol unchanged.
{ lib }:
let
  or' = a: b: if a == null then b else a;
in
rec {
  # The vLLM server's own arguments for profile `p`.
  vllmArgs =
    cfg: p:
    [
      "--model"
      (or' p.model cfg.model)
      "--served-model-name"
      "default"
      "--kv-cache-dtype"
      (or' p.kvType cfg.kvCacheDtype)
      "--reasoning-parser"
      (or' p.reasoningParser "qwen3")
      "--max-model-len"
      (toString p.maxModelLen)
    ]
    ++ (
      if p.kvCacheMemoryBytes == null then
        [
          "--gpu-memory-utilization"
          (toString (or' p.gpuMemoryUtilization cfg.gpuMemoryUtilization))
        ]
      else
        [
          "--kv-cache-memory-bytes"
          (toString p.kvCacheMemoryBytes)
        ]
    )
    ++ lib.optionals (or' p.quantization cfg.quantization != null) [
      "--quantization"
      (or' p.quantization cfg.quantization)
    ]
    ++ lib.optionals (cfg.maxCudagraphCaptureSize != null) [
      "--max-cudagraph-capture-size"
      (toString cfg.maxCudagraphCaptureSize)
    ]
    ++ lib.optionals (or' p.toolCallParser cfg.toolCallParser != null) [
      "--enable-auto-tool-choice"
      "--tool-call-parser"
      (or' p.toolCallParser cfg.toolCallParser)
    ]
    # The same server-wide template default the ik path passes, so switching
    # engine does not silently switch thinking back on. Request values win.
    ++ lib.optionals (cfg.chatTemplateKwargs != null) [
      "--default-chat-template-kwargs"
      cfg.chatTemplateKwargs
    ]
    ++ lib.optional (or' p.enforceEager cfg.enforceEager) "--enforce-eager"
    # ⛔ OFF MUST BE SAID OUT LOUD. vLLM 0.30 defaults prefix caching ON, so
    # omitting the flag left it enabled — found 2026-10-08 when a deployed
    # "off" profile still logged Mamba align mode, and every "off" arm of that
    # day's first A/B had in fact run with it on.
    ++ [ (if cfg.enablePrefixCaching then "--enable-prefix-caching" else "--no-enable-prefix-caching") ]
    ++ p.extraArgs
    ++ cfg.extraArgs;

  # The vLLM engine's part of `docker run` for profile `p`: the published
  # port, IPC, the Hugging Face cache, environment, image and server arguments.
  # `publish` is everything before ":8000" in docker's `-p`: nardol passes its
  # port, which publishes on every interface; edoras passes "127.0.0.1:8000".
  vllmContainerArgs =
    cfg: publish: p:
    [
      "-p"
      "${publish}:8000"
      "--ipc=host" # vLLM needs a large shared-memory segment for NCCL/worker IPC
      "-v"
      "${cfg.stateDir}:/root/.cache/huggingface:rw"
      # ⛔ OFFLINE, SO A START NEVER BECOMES A DOWNLOAD. Without this a missing
      # or partial checkpoint is fetched at start — 18 GB over the WAN inside
      # a unit whose restart limit assumes a load takes minutes. Download
      # deliberately (`hf download` in the same image) and let a missing model
      # fail at once, with a name, instead.
      "-e"
      "HF_HUB_OFFLINE=1"
    ]
    ++ lib.optionals (p.allowLongContext == true) [
      "-e"
      "VLLM_ALLOW_LONG_MAX_MODEL_LEN=1"
    ]
    ++ lib.optionals (p.expandableSegments == true) [
      "-e"
      "PYTORCH_ALLOC_CONF=expandable_segments:True"
    ]
    ++ [
      # A profile may need a different build (the embedding-quant patch in
      # hosts/nixos/nardol/vllm-embedq); the host's digest-pinned image otherwise.
      (or' p.image cfg.image)
    ]
    ++ vllmArgs cfg p;
}
