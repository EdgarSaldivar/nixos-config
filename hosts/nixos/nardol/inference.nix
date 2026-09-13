# vLLM on nardol's 4090.
#
# ⛔ EXCLUSIVE WITH GAMING, and that is a VRAM fact, not a policy choice. 24 GB
# cannot hold a served model and a game at once, and consumer cards have no MIG
# to partition it with. The arbitration lives in ./gaming-arbitration.nix.
#
# Every choice a model swap touches is an option below, because the model WILL
# change: quantized checkpoints get re-cut, Qwen ships a new generation, or a
# benchmark on real work says something else is better. Swapping should be
# editing `model` and `quantization`, not rewriting a service.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.nardol.inference;
in
{
  options.nardol.inference = {
    enable = lib.mkEnableOption "an inference server on nardol's GPU";

    engine = lib.mkOption {
      type = lib.types.enum [
        "vllm"
        "llama-cpp"
      ];
      default = "vllm";
      description = ''
        Which runtime serves the model. ONE AT A TIME — 24 GB cannot hold two
        copies of a 27B, so these are alternatives rather than peers, and A/B
        means switching this option and re-running scripts/inference-ab.py.

        vllm is the incumbent and the control. llama-cpp is the challenger, for
        a specific reason: every vLLM-format INT4 cut of this model is ~20-21 GB
        because ~5.0B of 27.8B parameters stay BF16 (embeddings, lm_head, and
        the 48 GatedDeltaNet layers). GGUF quantizes those too, so Q4_K_M is
        16.8 GB — about 4 GB more headroom, which is the difference between
        running eager at 32k and running graphs at far more.

        Published 4090 figures put llama.cpp at 37-47 tok/s decode against our
        measured 22, but those come from a harness that folds prefill into
        decode and never exercises the tools API. Trust scripts/inference-ab.py
        over them.
      '';
    };

    ggufFile = lib.mkOption {
      type = lib.types.str;
      default = "/srv/inference/gguf/Qwen3.6-27B-Q4_K_M.gguf";
      description = "Path to the GGUF, used when engine = llama-cpp.";
    };

    llamaCppImage = lib.mkOption {
      type = lib.types.str;
      default = "ghcr.io/ggml-org/llama.cpp@sha256:6ac921528d613deb0fd142c654735e594a446a1c37a069eeab08d8fd974d4bec";
      description = ''
        Digest-pinned server-cuda image.

        ⚠️ The fused CUDA GATED_DELTA_NET kernel only landed in PR #19504,
        available from build b8233. An older binary runs this architecture but
        not at current speed, so a benchmark against one is not evidence about
        llama.cpp — verify the build in the server banner before trusting any
        number from it.
      '';
    };

    model = lib.mkOption {
      type = lib.types.str;
      default = "palmfuture/Qwen3.6-27B-GPTQ-Int4";
      description = ''
        Hugging Face model id, or a path under stateDir.

        A GPTQ-Int4 cut of Qwen3.6-27B, chosen for its CALIBRATION rather than
        its format: 256 domain-mixed sequences (102 allenai/c4, 77 tulu-3-sft,
        51 codeparrot, 26 MATH-500), group_size=32, 100% GPTQ success with 0%
        RTN fallback, and MTP speculative-decoding weights verified on vLLM
        0.21.0. Contrast the data-free AWQ builds in circulation, which carry
        the same "AWQ" label and none of the quality.

        ⚠️ IT IS 20 GB ON DISK, NOT THE ~15 GB A 27B INT4 IMPLIES. group_size=32
        carries far more scale/zero overhead than the usual 128, and the MTP
        weights add more. On a 23.52 GiB card that leaves ~3.5 GB for KV cache,
        Mamba state, activations and CUDA graphs — which is why this host runs
        eager at 32k context instead of with graphs at 200k+.

        A leaner cut (group_size=128, no bundled MTP) should free several GB and
        is the FIRST thing to try if context length or decode speed disappoints.
        That is the whole reason these are options.

        ⚠️ Community checkpoint, and it publishes no benchmarks of the quantized
        model against full precision. If output quality ever looks off, re-cut
        or re-source this before blaming the flags.

        Base model chosen 2026-09-12 on evidence, not vibes: Qwen3.6-27B scores
        77.2% on SWE-bench Verified, beating Qwen3.5-397B-A17B (76.2%) with
        14.7x fewer total parameters, and vLLM's own recipe lists Int4 on a
        single 24 GB GPU as a supported tier. The obvious alternative,
        Qwen3-Coder-30B-A3B, is explicitly code- and agent-targeted but scores
        only ~50-52% on the same benchmark — a gap far larger than its speed
        advantage is worth for a single user.
      '';
    };

    quantization = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        ⛔ null MEANS AUTO-DETECT, AND THAT IS FASTER THAN NAMING IT.

        vLLM reads the quantization method from the checkpoint's own config and
        will promote a GPTQ checkpoint to the gptq_marlin kernel when the
        hardware supports it. Passing --quantization gptq explicitly can pin it
        to the reference GPTQ kernel instead and quietly cost throughput, so
        leave this null unless a checkpoint genuinely cannot be detected.

        ⚠️ CALIBRATION, NOT FORMAT, DECIDES QUALITY HERE.

        "Give Me BF16 or Give Me Death" measures W4A16-INT4 at 80.5 pass@1 on
        HumanEval against W8A8-FP8 at 80.0 — 4-bit weight-only is not the
        handicap for code that folklore says it is. What DOES hurt is bad
        calibration: the same paper attributes GPTQ beating AWQ to better
        calibration data rather than the algorithm, and notes random-token
        calibration degrades accuracy at low bitwidth.

        So prefer a checkpoint whose calibration set is documented and
        code-heavy. At least one published Qwen3.6-27B AWQ build uses DATA-FREE
        quantization; the label says AWQ and the quality will not.

        FP8 is not an option on this card regardless: 27 GB of weights against
        24 GB of VRAM. vLLM's recipe puts FP8 on a 40 GB GPU.
      '';
    };

    maxModelLen = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = if cfg.engine == "llama-cpp" then 65536 else 32768;
      defaultText = lib.literalExpression ''if engine == "llama-cpp" then 65536 else 32768'';
      description = ''
        ⛔ null DOES NOT MEAN "let vLLM pick something sensible". It means the
        model's NATIVE length, which here is 262144, and on 24 GB that does not
        fit: engine init died with
          torch.OutOfMemoryError: Tried to allocate 1.53 GiB.
          GPU has 23.52 GiB total, of which 816.75 MiB is free.
        and systemd restarted it 189 times. Measured 2026-09-13.

        ⛔ THE CEILING IS ENGINE-DEPENDENT, so the default follows the engine.
        Setting one number for both is a trap: 65536 runs fine under llama.cpp
        in 18.5 GB, and under vLLM it dies with
          ValueError: max seq len (65536) needs 2.3 GiB KV cache, larger than
          the available KV cache memory (1.56 GiB)
        because vLLM's weights are 20 GB against llama.cpp's 16.8. Measured
        2026-09-13 by flipping `engine` back with the value still pinned.

        32768 is what the vLLM checkpoint actually starts at, found by bisection
        on the live card 2026-09-13. It is far below the 214k the KV arithmetic
        promises, for a reason the arithmetic could not know: this checkpoint is
        20 GB on disk, not the ~15 GB a 27B Int4 suggests, because group_size=32
        carries heavy scale/zero overhead and the MTP speculative-decoding
        weights are bundled in. 20 GB of weights on a 23.52 GiB card leaves ~3.5
        GB for everything else.

        ⛔ Raise this only against a successful start, never on arithmetic. Arithmetic says ~214k tokens is affordable — the hybrid
        architecture carries a KV cache on only 16 of its 64 layers, so
        16 x 2 x 4 heads x 256 dim x 1 byte = 32 KiB/token at FP8 — but that is
        a ceiling that ignores DeltaNet recurrent state, allocator overhead,
        activations and CUDA graphs. Set a number only to constrain it BELOW
        what vLLM measures, never to assert something above it.
      '';
    };

    kvCacheDtype = lib.mkOption {
      type = lib.types.str;
      default = "fp8";
      description = ''
        Halves KV cache against bf16, which is what buys the long context.

        ⛔ NEVER ADD --calculate-kv-scales. vLLM issue #37554 reports it
        producing a CORRUPTED fp8 KV cache on hybrid GatedDeltaNet+Attention
        models, which is exactly this architecture. Corruption, not a crash:
        the server runs and the answers quietly get worse. Use a checkpoint
        carrying calibrated scales instead.
      '';
    };

    enablePrefixCaching = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        The single highest-value flag for coding work, and it is not close.
        Measured on this architecture: a 25k-token document takes 22.4s to first
        token cold and 0.56s on a cached prefix — a 40x difference. Coding means
        resending the same files, so nearly every request is a cache hit.

        ⚠️ vLLM's recipe calls prefix caching experimental in "align" mode for
        Mamba-style components, and this model's 48 DeltaNet layers are exactly
        that. Verify it on the deployed version rather than trusting it: the
        flag doing the most work is the one worth proving.
      '';
    };

    gpuMemoryUtilization = lib.mkOption {
      type = lib.types.float;
      default = 0.95;
      description = ''
        Fraction of VRAM vLLM may claim for weights plus KV cache.

        ABOVE the 0.9 default, which is unusual and deliberate. The weights
        alone are ~20 GB of a 23.52 GiB card, so at 0.88 the budget (20.7 GB)
        barely exceeded the weights and the KV cache got a few hundred MB —
        engine init died on CUDA OOM. Running eager (below) frees the headroom
        that a high utilisation would otherwise steal from CUDA graphs.
      '';
    };

    enforceEager = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        ⛔ REQUIRED WITH THIS CHECKPOINT. CUDA graphs do not fit.

        Measured by bisection 2026-09-13: capture-size 256 and 64 both failed
        (OOM, then a start that never completed), while eager mode started
        cleanly with a 43,690-token KV cache. CUDA graphs allocate outside the
        gpu-memory-utilization budget, and there is nothing left to allocate
        from once 20 GB of weights are resident.

        The cost is decode throughput — graphs mainly help small-batch decode,
        which is exactly the single-user case. That is a real loss, and the
        right fix is a LEANER CHECKPOINT rather than more tuning here. See the
        note on `model`.
      '';
    };

    maxCudagraphCaptureSize = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      description = ''
        Ignored while enforceEager is true. Kept as an option because a leaner
        checkpoint would make CUDA graphs affordable again, at which point this
        is the knob vLLM's recipe says to reach for.
      '';
    };

    llamaKvType = lib.mkOption {
      type = lib.types.str;
      default = "q8_0";
      description = ''
        llama.cpp spells KV quantisation differently from vLLM: q8_0 / q4_0
        rather than fp8. q8_0 is the published choice up to 32k; q4_0 is what
        the 64k and 256k recipes use. Starting at q8_0 keeps the correctness
        baseline honest — quantising the cache harder is an optimisation to
        make after a clean run, not before one.
      '';
    };

    toolCallParser = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "qwen3_xml";
      description = ''
        ⛔ WITHOUT THIS, TOOL CALLING IS NOT MERELY DEGRADED — IT IS REFUSED.

        vLLM rejects `tool_choice: auto` outright with HTTP 400:
          "auto" tool choice requires --enable-auto-tool-choice and
          --tool-call-parser to be set
        so Home Assistant would fail on every request. Found 2026-09-13 by
        scripts/inference-ab.py on the first run against our own deployment;
        nothing in the published 4090 benchmark would have caught it, because
        that harness never submits a tools array.

        qwen3_xml matches what this model's chat template actually emits —
        verified by reading the template out of the checkpoint rather than
        guessing: <tool_call><function=name><parameter=x>value</parameter>.
        This build also registers qwen3_coder, the older name for the same
        family; try it if the parser ever stops matching.

        ⚠️ llama.cpp issue #26763 reports THIS format silently absorbing
        subsequent protocol text into an argument when whitespace before
        </parameter> is missing, closed as not planned. Whichever runtime wins,
        validate tool arguments rather than trusting a 200 response.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8000;
      description = "OpenAI-compatible endpoint. Home Assistant 2026.8's built-in client speaks this.";
    };

    image = lib.mkOption {
      type = lib.types.str;
      default = "vllm/vllm-openai@sha256:a230095847e93bd4df9888b33dab956fa9504537b828a23657d2b26fed57b5c9";
      description = ''
        Digest-pinned, matching how ./wolf/image-config-policy.nix pins Wolf. A
        tag would let an upstream rebuild change inference behaviour with no
        commit here, which is precisely the drift this fleet's checks exist to
        prevent. This digest is v0.21.0 — deliberately NOT the newest (v0.29.0
        exists). 0.21.0 is the version the default checkpoint's MTP speculative
        decoding weights were actually verified against, and a verified pairing
        beats a newer one for something whose failure mode is subtly worse
        output rather than a crash.
      '';
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/srv/inference";
      description = ''
        Model weights live here, on the encrypted NVMe rather than the root
        filesystem: a quantized 27B is ~15 GB and swapping models accumulates
        several. /srv had 2.5 TB free when this was written.
      '';
    };

    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [
        "--max-cudagraph-capture-size"
        "256"
      ];
      description = ''
        Escape hatch so a flag change does not need a module change.
        Known knob: vLLM's recipe says to reduce --max-cudagraph-capture-size
        (default 512) if CUDA graph capture fails, which is plausible on a card
        this full.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = [ "d ${cfg.stateDir} 0750 root root -" ];

    virtualisation.oci-containers.containers = lib.mkMerge [
      (lib.mkIf (cfg.engine == "vllm") {
        vllm = {
          image = cfg.image;
          autoStart = true;
          extraOptions = [
            "--gpus=all"
            "--ipc=host" # vLLM needs a large shared-memory segment for NCCL/worker IPC
          ];
          ports = [ "${toString cfg.port}:8000" ];
          volumes = [ "${cfg.stateDir}:/root/.cache/huggingface:rw" ];
          cmd = [
            "--model"
            cfg.model
            "--served-model-name"
            "default"
            "--gpu-memory-utilization"
            (toString cfg.gpuMemoryUtilization)
            "--kv-cache-dtype"
            cfg.kvCacheDtype
            "--reasoning-parser"
            "qwen3"
          ]
          ++ lib.optionals (cfg.quantization != null) [
            "--quantization"
            cfg.quantization
          ]
          ++ lib.optionals (cfg.maxModelLen != null) [
            "--max-model-len"
            (toString cfg.maxModelLen)
          ]
          ++ lib.optionals (cfg.maxCudagraphCaptureSize != null) [
            "--max-cudagraph-capture-size"
            (toString cfg.maxCudagraphCaptureSize)
          ]
          ++ lib.optionals (cfg.toolCallParser != null) [
            "--enable-auto-tool-choice"
            "--tool-call-parser"
            cfg.toolCallParser
          ]
          ++ lib.optional cfg.enforceEager "--enforce-eager"
          ++ lib.optional cfg.enablePrefixCaching "--enable-prefix-caching"
          ++ cfg.extraArgs;

        };
      })

      (lib.mkIf (cfg.engine == "llama-cpp") {
        llamacpp = {
          image = cfg.llamaCppImage;
          autoStart = true;
          extraOptions = [ "--gpus=all" ];
          ports = [ "${toString cfg.port}:8080" ];
          volumes = [ "/srv/inference/gguf:/models:ro" ];
          cmd = [
            "-m"
            "/models/${baseNameOf cfg.ggufFile}"
            "--host"
            "0.0.0.0"
            "--port"
            "8080"
            # All layers on the GPU. Anything less silently offloads to CPU and
            # the result is a benchmark of the wrong thing.
            "-ngl"
            "99"
            "-c"
            (toString cfg.maxModelLen)
            # ⛔ --jinja is REQUIRED for tool calling. Without it llama-server
            # falls back to a generic template, the model never emits its
            # <tool_call><function=...> format, and tools silently never fire.
            "--jinja"
            "-fa"
            "on"
            # ⛔ ONE SLOT, explicitly. ik_llama #1932 reports recurrent-state
            # cross-conversation corruption with three or more slots on hybrid
            # models. This is a single-user host; inheriting a multi-user
            # default buys nothing and risks exactly that.
            "--parallel"
            "1"
          ]
          ++ lib.optionals (cfg.kvCacheDtype != null) [
            "-ctk"
            cfg.llamaKvType
            "-ctv"
            cfg.llamaKvType
          ]
          ++ cfg.extraArgs;
        };
      })
    ];

    # ⛔ RATE-LIMIT THE RESTARTS. A misconfigured vLLM restarted 189 times on
    # 2026-09-13 before anyone looked, each attempt pulling ~15 GB of weights
    # into VRAM and dying on CUDA OOM. Unbounded retry turns a config mistake
    # into hours of GPU thrash and buries the original error under identical
    # repeats. Five failures inside ten minutes is enough to conclude it is not
    # coming up on its own.
    systemd.services."docker-${if cfg.engine == "vllm" then "vllm" else "llamacpp"}".serviceConfig = {
      RestartSec = lib.mkForce "30s";
      StartLimitBurst = 5;
      StartLimitIntervalSec = 600;
    };

    networking.firewall.interfaces.eth0.allowedTCPPorts = [ cfg.port ];
  };
}
