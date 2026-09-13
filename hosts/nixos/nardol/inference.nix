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
        "ik-llama"
      ];
      default = "vllm";
      description = ''
        Which runtime serves the model. ONE AT A TIME — 24 GB cannot hold two
        copies of a 27B, so these are alternatives rather than peers, and A/B
        means switching this option and re-running scripts/inference-ab.py.

        ik-llama is ikawrakow's fork, carrying its own IQK quantization formats
        and a published 32k config claiming 16 GB against upstream's 22 — on a
        24 GB card that difference is context. It is built from source into a
        local image because there is no published container and it is not in
        nixpkgs; see pkgs note in the runtime image's Dockerfile for why it must
        be compiled under `docker run --gpus=all` rather than `docker build`.

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
      default =
        if cfg.engine == "ik-llama" then
          "/srv/inference/gguf/Qwen3.6-27B-MTP-IQ4_KS.gguf"
        else
          "/srv/inference/gguf/Qwen3.6-27B-Q4_K_M.gguf";
      defaultText = lib.literalExpression ''
        if engine == "ik-llama"
        then "/srv/inference/gguf/Qwen3.6-27B-MTP-IQ4_KS.gguf"
        else "/srv/inference/gguf/Qwen3.6-27B-Q4_K_M.gguf"'';
      description = ''
        Path to the GGUF, used by the llama-cpp and ik-llama engines.

        ⛔ THE DEFAULT FOLLOWS THE ENGINE, AND SWAPPING ONE WITHOUT THE OTHER
        SILENTLY COSTS HALF THE THROUGHPUT. `specStages` drafts with the
        model's own MTP head, and those tensors exist only in an MTP cut — point
        ik-llama at a plain IQ4_KS or Q4_K_M file and the mtp stage cannot load.
        Measured 2026-09-13 on this card: MTP is the single largest win
        available here, 50.8 -> 106.1 tok/s on code, so losing it is not subtle.

        ⚠️ Many community GGUFs labelled "MTP" strip the MTP tensors during
        re-quantization while keeping the metadata flag, which fails at load
        rather than degrading. Verify the stage actually initialised in the
        server banner before trusting a benchmark from a new file.

        IQ4_KS is ikawrakow's own recommendation for this model family, at
        ~0.14% quantization error — and it is an ik-only format, which is part
        of why this engine exists. The llama-cpp path stays on Q4_K_M because
        mainline cannot read IQ4_KS.
      '';
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
      default = if cfg.engine == "vllm" then 32768 else 65536;
      defaultText = lib.literalExpression ''if engine == "vllm" then 32768 else 65536'';
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
        because vLLM's weights are 20 GB against llama.cpp's 16.8. The condition
        is written as "vllm or not" rather than naming one GGUF engine, so a
        fourth engine inherits the GGUF ceiling instead of vLLM's. Measured
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

    ikLlamaImage = lib.mkOption {
      type = lib.types.str;
      default = "ik-llama:local";
      description = ''
        Locally built, so this is a tag rather than a digest — the one image
        here that is not content-addressed. The git revision is baked in at
        /BUILD_REV and reported by `--version`, which is what makes a benchmark
        number traceable; record it alongside any result.
      '';
    };

    specStages = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "mtp:n_max=8" ];
      example = [ "mtp" ];
      description = ''
        Speculative decoding stages for ik-llama, one `--spec-type` each.

        ⛔ THIS IS THE BIGGEST SINGLE LEVER ON THIS HOST. Measured 2026-09-13,
        median tok/s over 12 unique code and 6 unique prose prompts, container
        recreated between configs:

          no speculation                      code  50.8   prose 50.8
          ngram-mod alone                     code  52.1   prose 51.7
          ngram-mod + mtp                     code  88.6   prose 77.1
          mtp                                 code 105.4   prose 90.1

        MTP is lossless by construction — the draft is verified against the same
        model, so greedy output is token-identical with and without it. None of
        this trades quality for speed. A cross-vocabulary DRAFT MODEL is a
        different mechanism and is NOT safe: it translates tokens between
        vocabularies and silently breaks JSON braces and tool-call boundaries,
        which is a corruption you would find in production, not in a benchmark.

        ⛔ DO NOT ADD AN ngram-mod STAGE AHEAD OF mtp. It reads like free extra
        speculation and it is a 17% LOSS (106.1 -> 88.6). Stages run in order
        and MTP is the fallback, so a weak n-gram match DISPLACES a better MTP
        draft instead of adding to it.

        ⛔ AND DO NOT BENCHMARK AN ngram STAGE BY REPEATING ONE PROMPT. It
        learns across requests, so a second run of the same prompt decodes text
        it has already seen. That artifact measured 404 tok/s here — four times
        the real rate — and briefly made the losing config look like the winner.
        Every number in this file uses each prompt at most once.

        ⛔ n_max IS NOT A "HIGHER IS BETTER" KNOB, AND ITS OPTIMUM MOVES WITH
        CONTEXT LENGTH. This is why the default here is not upstream's. Decode
        tok/s with prefill excluded (streamed, median of 4, unique synthetic
        context per request):

          occupied context     300     6000    25000
          n_max=16 (upstream) 123.2    129.4    99.5
          n_max=8             136.8    146.1    89.3
                             +11.1%   +12.9%  -10.2%

        A deeper draft amortises one expensive verification pass over more
        tokens; a shallower one wastes less work when drafts are rejected. As
        the attention history grows, verification gets costlier and the deeper
        draft wins. So 8 is right for what this host actually serves — Home
        Assistant voice commands of a few hundred tokens, and an interactive
        assistant in the low thousands — and WRONG above roughly 12k occupied
        context, where upstream's 16 is ~10% faster. If this host is ever
        pointed at whole-repository prompts, set this back to [ "mtp" ].

        ⚠️ The short-prompt sweeps are noisier than they look: run-to-run spread
        at a fixed setting reached ±15%, so treat any single-digit difference
        here as a tie. The two effects that reproduced across independent runs
        are MTP itself and the long-context inversion.

        ⛔ n_max HERE IS A CEILING, NOT JUST A DEFAULT. ik accepts a per-request
        override — a `speculative` object in the JSON body alongside `messages`
        — but only DOWNWARD. Verified against the running server 2026-09-13:

          {"speculative":{"stages":[{"type":"mtp","n_max":2}]}}   -> accepted
          {"speculative":{"stages":[{"type":"mtp","n_max":16}]}}  -> 400,
            "n_max=16 exceeds the recurrent speculative startup limit of 8"

        So a client can ask for a shallower draft than this option, never a
        deeper one, and the stage TYPES must match what the server started with.
        Setting 8 here permanently forecloses the depth that wins above ~12k
        context. That is the real cost of this default, and it is the reason to
        revisit it if the workload ever changes — not the ~10% throughput.

        Note that almost nothing sends that field: it is an ik extension, not
        OpenAI, so Home Assistant and every stock client get exactly what is
        configured here. Tune for the traffic that cannot ask.

        Lower n_max also RESERVES LESS VRAM, ~150 MiB per step, and that part is
        exact rather than noisy: 20,458 MiB at 8 against 21,654 at 16. The
        server banner reports the depth it actually chose as
        `llama_spec_ckpt_init: ... per-step (max_tokens=N)`, where N is n_max+1
        — read it there rather than trusting this comment after a version bump.

        p_min is the confidence cutoff, default 0.8. It is NOT the lever the
        VRAM curve made it look like: forcing p_min=0.0 reproduces the same
        inversion, so the depth is what matters. Pinning p_min=0.0 at upstream's
        depth of 16 is the worst of both and collapses to 74.8 tok/s.
      '';
    };

    mtpRequantizeOutputTensor = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "iq4_ks";
      description = ''
        `-mtprot`: requantize the MTP head's output tensor at load time.

        Worth 8% on code and 10% on prose here (98.0 -> 106.1, 82.6 -> 90.0),
        measured 2026-09-13, and unlike n_max this one did not invert with
        context length. The cost is exact and the server prints it:
          Creating extra output tensor of type iq4_ks for MTP usage.
          Additional memory required is 645.09 MiB
        Free speed by the standards of everything else on this list.

        null disables it. Only meaningful when `specStages` contains an mtp
        stage; it is the MTP head's tensor, not the model's.
      '';
    };

    ctxCheckpoints = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = null;
      description = ''
        `--ctx-checkpoints`: how many recurrent-state snapshots to retain.

        null keeps the upstream default. 0 disables them, which measured +3% on
        code decode (107.8 -> 111.2) at no VRAM cost.

        ⛔ THAT 3% COSTS 18x ON EVERY FOLLOW-UP TURN, AND NO DECODE BENCHMARK
        WILL SHOW IT. Measured 2026-09-13, time to first token across a
        three-turn conversation on a shared 8k prefix:

                            turn 1   turn 2   turn 3
          default            8.45s    0.25s    0.28s
          --ctx-checkpoints 0 5.95s    5.00s    5.05s

        These snapshots are what lets a hybrid model resume from a matching
        prompt prefix. This is a 48-layer GatedDeltaNet model whose recurrent
        state cannot be rebuilt from a KV cache alone, so with them off there is
        nothing to resume from and each turn re-prefills the entire history —
        and the penalty grows with the conversation, because the history does.

        A single-shot prompt pays nothing, which is exactly why the decode sweep
        rated this a win. This host serves a chat assistant. Leave it null.
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

      (lib.mkIf (cfg.engine == "ik-llama") {
        ikllama = {
          image = cfg.ikLlamaImage;
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
            "-ngl"
            "99"
            "-c"
            (toString cfg.maxModelLen)
            "--jinja"
            "-fa"
            "on"
            # ⛔ ik #1932 reports recurrent-state cross-conversation corruption
            # with three or more slots on hybrid models. Single user; there is
            # nothing to gain from a multi-user default and a correctness bug
            # to lose.
            "--parallel"
            "1"
            "-ctk"
            cfg.llamaKvType
            "-ctv"
            cfg.llamaKvType
          ]
          ++ lib.concatMap (stage: [
            "--spec-type"
            stage
          ]) cfg.specStages
          ++ lib.optionals (cfg.mtpRequantizeOutputTensor != null) [
            "-mtprot"
            cfg.mtpRequantizeOutputTensor
          ]
          ++ lib.optionals (cfg.ctxCheckpoints != null) [
            "--ctx-checkpoints"
            (toString cfg.ctxCheckpoints)
          ]
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
