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

  # Shared with pkgs/amon-din.nix so the Mac menu cannot offer a model this host
  # does not serve. See the header of that file for why it is data, not options.
  profileData = import ../../../lib/inference-profiles.nix;

  ggufRoot = "${cfg.stateDir}/gguf";
  profileStateDir = builtins.dirOf cfg.profileStateFile;

  # A profile field, or the module-level option when the profile says null. The
  # GPU-only 27B therefore keeps every default documented above instead of
  # restating it in lib/inference-profiles.nix.
  or' = a: b: if a == null then b else a;

  # ⛔ THE CONTAINER SEES /models, NOT /srv. Flash-style cuts live in
  # subdirectories, so this maps the host path rather than taking `baseNameOf`,
  # which silently flattened `flash-next/foo.gguf` to `foo.gguf` and produced a
  # file-not-found the server reports only in its own log.
  containerModelPath =
    f:
    if lib.hasPrefix "${ggufRoot}/" f then
      "/models/" + lib.removePrefix "${ggufRoot}/" f
    else
      throw "nardol.inference: GGUF ${f} is not under ${ggufRoot}, so the container cannot see it";

  ikProfileArgs =
    p:
    [
      "-m"
      (containerModelPath (or' p.ggufFile cfg.ggufFile))
      "--host"
      "0.0.0.0"
      "--port"
      "8080"
      "-ngl"
      "99"
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
      (or' p.kvType cfg.llamaKvType)
      "-ctv"
      (or' p.kvType cfg.llamaKvType)
    ]
    ++ lib.optionals (or' p.maxModelLen cfg.maxModelLen != null) [
      "-c"
      (toString (or' p.maxModelLen cfg.maxModelLen))
    ]
    # The vision encoder and projector, a separate file in GGUF land. Without it
    # the same model serves text and answers an image_url with an error.
    ++ lib.optionals (p.mmproj != null) [
      "--mmproj"
      (containerModelPath p.mmproj)
    ]
    ++ lib.concatMap (stage: [
      "--spec-type"
      stage
    ]) (or' p.specStages cfg.specStages)
    ++ lib.optionals (or' p.mtpRequantizeOutputTensor cfg.mtpRequantizeOutputTensor != null) [
      "-mtprot"
      (or' p.mtpRequantizeOutputTensor cfg.mtpRequantizeOutputTensor)
    ]
    # ⛔ A SEPARATE MTP HEAD IS A GPU RESIDENT, AND -ncmoe DOES NOT APPLY TO IT.
    # The server says so at load — "MTP draft ignores target CPU-MoE/tensor
    # placement overrides" — so this file competes with weights, KV and the
    # compute buffer for the same 24 GB. Measured 2026-09-17: adding the 1.8 GiB
    # head cost ~6.7 GB of VRAM once its own context was allocated, which is why
    # the profile that uses it also raises cpuMoe.
    ++ lib.optionals (p.draftModel != null) [
      "-md"
      (containerModelPath p.draftModel)
    ]
    # Hybrid CPU/GPU offload. Only a profile sets these: on a model that fits
    # the card they are meaningless, and --n-cpu-moe 0 is not the same as
    # omitting it.
    ++ lib.optionals (p.cpuMoe != null) [
      "--n-cpu-moe"
      (toString p.cpuMoe)
    ]
    ++ lib.optionals (p.batchSize != null) [
      "-b"
      (toString p.batchSize)
    ]
    ++ lib.optionals (p.ubatchSize != null) [
      "-ub"
      (toString p.ubatchSize)
    ]
    ++ lib.optionals (cfg.ctxCheckpoints != null) [
      "--ctx-checkpoints"
      (toString cfg.ctxCheckpoints)
    ]
    ++ lib.optionals (cfg.ctxCheckpointsInterval != null) [
      "--ctx-checkpoints-interval"
      (toString cfg.ctxCheckpointsInterval)
    ]
    ++ lib.optionals (cfg.chatTemplateKwargs != null) [
      "--chat-template-kwargs"
      cfg.chatTemplateKwargs
    ]
    ++ p.extraArgs
    ++ cfg.extraArgs;

  # mainline llama.cpp. Same GGUF directory, its own image, no ik-only flags:
  # mainline rejects --spec-type, -mtprot and the IQK formats, so a profile on
  # this engine states its file and KV type rather than inheriting ik's.
  llamaCppProfileArgs =
    p:
    [
      "-m"
      (containerModelPath p.ggufFile)
      "--host"
      "0.0.0.0"
      "--port"
      "8080"
      # All layers on the GPU. Anything less silently offloads to CPU and
      # the result is a benchmark of the wrong thing.
      "-ngl"
      "99"
      "-c"
      (toString p.maxModelLen)
      # ⛔ --jinja is REQUIRED for tool calling. Without it llama-server
      # falls back to a generic template, the model never emits its
      # <tool_call><function=...> format, and tools silently never fire.
      "--jinja"
      "-fa"
      "on"
      # ⛔ ONE SLOT, explicitly — the same hybrid-model recurrent-state
      # corruption as the ik path above.
      "--parallel"
      "1"
      "-ctk"
      (or' p.kvType "q8_0")
      "-ctv"
      (or' p.kvType "q8_0")
    ]
    ++ lib.optionals (p.mmproj != null) [
      "--mmproj"
      (containerModelPath p.mmproj)
    ]
    ++ p.extraArgs
    ++ cfg.extraArgs;

  vllmProfileArgs =
    p:
    [
      "--model"
      (or' p.model cfg.model)
      "--served-model-name"
      "default"
      "--gpu-memory-utilization"
      (toString (or' p.gpuMemoryUtilization cfg.gpuMemoryUtilization))
      "--kv-cache-dtype"
      (or' p.kvType cfg.kvCacheDtype)
      "--reasoning-parser"
      (or' p.reasoningParser "qwen3")
      "--max-model-len"
      (toString p.maxModelLen)
    ]
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
    ++ lib.optional cfg.enablePrefixCaching "--enable-prefix-caching"
    ++ p.extraArgs
    ++ cfg.extraArgs;

  # The full `docker run` argument vector for one profile, engine included.
  dockerRunArgs =
    p:
    [
      "run"
      "--name=${cfg.containerName}"
      "--log-driver=journald"
      "--rm"
      "--pull"
      "missing"
      "--gpus=all"
    ]
    ++ {
      ik-llama = [
        "-p"
        "${toString cfg.port}:8080"
        "-v"
        "${ggufRoot}:/models:ro"
        # The image's own entrypoint is not consulted; the profile's arguments
        # go straight to the server binary.
        "--entrypoint"
        cfg.ikLlamaServer
        cfg.ikLlamaImage
      ]
      ++ ikProfileArgs p;

      llama-cpp = [
        "-p"
        "${toString cfg.port}:8080"
        "-v"
        "${ggufRoot}:/models:ro"
        cfg.llamaCppImage
      ]
      ++ llamaCppProfileArgs p;

      vllm = [
        "-p"
        "${toString cfg.port}:8000"
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
        # A profile may need a different build (the embedding-quant patch in
        # ./vllm-embedq); the module's digest-pinned image otherwise.
        (or' p.image cfg.image)
      ]
      ++ vllmProfileArgs p;
    }
    .${p.engine};

  # ⛔ THE ENGINE IS CHOSEN HERE, ON THE HOST, AT EXEC TIME — NOT BY NIX.
  # Every profile's complete `docker run` is generated at build time and this
  # script only picks one, so a switch is a restart of ONE unit no matter which
  # engine either side of it uses. That is what keeps gaming arbitration, the
  # restore path, the sleep inhibitor, the lease and the power cap working: all
  # of them name docker-ikllama, and none of them can tell vLLM is behind it.
  launcher = ''
    PROFILE=${profileData.default}
    if [ -r ${lib.escapeShellArg cfg.profileStateFile} ]; then
      read -r PROFILE < ${lib.escapeShellArg cfg.profileStateFile} || PROFILE=${profileData.default}
    fi

    case "$PROFILE" in
    ${lib.concatStringsSep "\n" (
      lib.mapAttrsToList (name: p: ''
        ${name})
          echo "inference: serving profile ${name} on ${p.engine}" >&2
          exec docker ${lib.escapeShellArgs (dockerRunArgs p)}
          ;;'') profileData.profiles
    )}
      *)
        # ⛔ FALL BACK, DO NOT FAIL. A profile can vanish from the flake while
        # a name sits in the state file — a rollback, a rename, a deploy of an
        # older generation. Refusing to start would take the endpoint down for
        # a stale string, and Home Assistant would lose its backend over a
        # typo. Serving the default and saying so in the log is recoverable.
        echo "inference: unknown profile '$PROFILE'; serving ${profileData.default}" >&2
        exec docker ${lib.escapeShellArgs (dockerRunArgs profileData.profiles.${profileData.default})}
        ;;
    esac
  '';

  # The switcher, on nardol rather than in the menu, so the same operation is
  # available over SSH, from a script, and from the Mac without three copies of
  # the validation.
  nardolModel = pkgs.writeShellApplication {
    name = "nardol-model";
    runtimeInputs = with pkgs; [
      systemd
      curl
      coreutils
    ];
    text = ''
      set -euo pipefail
      STATE=${lib.escapeShellArg cfg.profileStateFile}
      DEFAULT=${profileData.default}
      PROFILES="${lib.concatStringsSep " " (lib.attrNames profileData.profiles)}"

      current() { [ -r "$STATE" ] && cat "$STATE" || echo "$DEFAULT"; }

      # ⚠️ 900s BECAUSE vLLM IS THE SLOW ONE. ik maps a GGUF and answers in
      # under a minute; vLLM loads safetensors, profiles the vision encoder and
      # compiles before /health answers at all.
      wait_ready() {
        profile="$1"
        for _ in $(seq 1 450); do
          if curl -fsS -m 2 http://127.0.0.1:${toString cfg.port}/health >/dev/null 2>&1; then
            echo "serving $profile"
            return 0
          fi
          sleep 2
        done
        echo "started $profile but /health did not answer within 900s" >&2
        return 1
      }

      case "''${1:-list}" in
        list)
          for p in $PROFILES; do
            if [ "$p" = "$(current)" ]; then echo "* $p"; else echo "  $p"; fi
          done
          ;;
        current) current ;;
        serve)
          # This command is the deliberate operator override: serving wins even
          # over a live Moonlight session. Stopping the target performs the
          # ordered GPU handoff and intentionally disconnects any current game.
          systemctl stop nardol-gaming.target
          profile=$(current)
          systemctl start docker-ikllama
          wait_ready "$profile"
          ;;
        switch)
          want="''${2:-}"
          # ⛔ VALIDATE BEFORE WRITING. An unvalidated name is accepted here,
          # falls back inside the container, and leaves the menu showing a model
          # the server is not running — a silent lie is worse than a refusal.
          for p in $PROFILES; do [ "$p" = "$want" ] && ok=1; done
          if [ "''${ok:-0}" != 1 ]; then
            echo "unknown profile '$want'; known: $PROFILES" >&2
            exit 1
          fi
          if [ "$want" = "$(current)" ] && systemctl is-active --quiet docker-ikllama; then
            echo "already serving $want"; exit 0
          fi
          mkdir -p ${profileStateDir}
          echo "$want" > "$STATE"

          # ⛔ NEVER TOUCH THE UNIT WHILE A GAME IS LIVE. nardol-gaming.target
          # Conflicts= docker-ikllama, and systemd resolves a conflict by
          # stopping the OTHER side — so `systemctl restart docker-ikllama`
          # during a session does not queue behind the game, it TEARS THE
          # SESSION DOWN mid-stream. gaming-arbitration.nix learned this the
          # expensive way; a menu item must not re-learn it.
          #
          # Recording the choice is still correct: nardol-inference-restore
          # starts the unit when gaming ends, and the entrypoint reads this file
          # at exec, so the new model arrives with the restore.
          if systemctl is-active --quiet nardol-gaming.target; then
            echo "recorded $want; a session is live, so it starts with the next restore"
            exit 0
          fi

          if ! systemctl is-active --quiet docker-ikllama; then
            echo "recorded $want; inference is not running, so it starts with the next start"
            exit 0
          fi

          # ⚠️ RESTART, NOT start: the model is chosen when the container execs,
          # so a running server keeps serving the old one until it is replaced.
          systemctl restart docker-ikllama
          # Loading 15-90 GiB is not instant and a menu that returns before the
          # endpoint answers invites a second click on a half-started server.
          wait_ready "$want"
          ;;
        *) echo "usage: nardol-model [list|current|serve|switch <profile>]" >&2; exit 2 ;;
      esac
    '';
  };
in
{
  imports = [
    (lib.mkRemovedOptionModule [ "nardol" "inference" "engine" ] ''
      The engine is chosen per profile now, by `engine` in
      lib/inference-profiles.nix, and switched at runtime by `nardol-model`.
    '')
  ];

  options.nardol.inference = {
    enable = lib.mkEnableOption "an inference server on nardol's GPU";

    # ENGINES. A profile names one of these (lib/inference-profiles.nix);
    # there is no host-wide choice any more. The reasoning that picked them:
    #
    # ONE AT A TIME — 24 GB cannot hold two copies of a 27B, so these are
    # alternatives rather than peers, and A/B means switching profile and
    # re-running scripts/inference-ab.py.
    #
    # ik-llama is ikawrakow's fork, carrying its own IQK quantization formats
    # and a published 32k config claiming 16 GB against upstream's 22 — on a
    # 24 GB card that difference is context. It is built from source into a
    # local image because there is no published container and it is not in
    # nixpkgs; see pkgs note in the runtime image's Dockerfile for why it must
    # be compiled under `docker run --gpus=all` rather than `docker build`.
    #
    # vllm is the incumbent and the control. llama-cpp is the challenger, for
    # a specific reason: every vLLM-format INT4 cut of this model is ~20-21 GB
    # because ~5.0B of 27.8B parameters stay BF16 (embeddings, lm_head, and
    # the 48 GatedDeltaNet layers). GGUF quantizes those too, so Q4_K_M is
    # 16.8 GB — about 4 GB more headroom, which is the difference between
    # running eager at 32k and running graphs at far more.
    #
    # Published 4090 figures put llama.cpp at 37-47 tok/s decode against our
    # measured 22, but those come from a harness that folds prefill into
    # decode and never exercises the tools API. Trust scripts/inference-ab.py
    # over them.
    containerName = lib.mkOption {
      type = lib.types.str;
      default = "ikllama";
      readOnly = true;
      description = ''
        The one container, and so the one unit (docker-<name>.service), that
        serves every profile on every engine.

        ⛔ FROZEN, AND THE NAME IS HISTORICAL. It says ikllama while serving
        vLLM because gaming-arbitration.nix, the restore and inhibit units,
        idle-suspend.nix, nardol-lease, the dungeon-scriber worker and
        AGENTS.md all name it. Renaming means moving every one of them in a
        single deploy; a container renamed per engine would leave them
        pointing at a unit that no longer exists, and systemd CREATES a unit
        when you set properties on a name, so the phantom would absorb the
        policy while the real server ran without it.
      '';
    };

    ggufFile = lib.mkOption {
      type = lib.types.str;
      default = "/srv/inference/gguf/Qwen3.8-27B-MTP-IQ4_KS.gguf";
      description = ''
        The GGUF an ik-llama profile serves when it names none. A llama-cpp
        profile must name its own: mainline cannot read IQ4_KS.

        ⛔ THE FILE AND THE ENGINE TRAVEL TOGETHER, AND SWAPPING ONE WITHOUT THE
        OTHER SILENTLY COSTS HALF THE THROUGHPUT. `specStages` drafts with the
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

        THE MODEL MOVED 3.6 -> 3.8 ON 2026-09-17, and the generation is worth
        more than any flag on this list. Same architecture, same 27B, same
        15.75 GiB on the card, so nothing else here had to change:

          benchmark (Qwen's own, Claude Code harness)   3.6     3.8
          SWE-bench Pro                                 53.5    61.7
          Terminal-Bench 2.1                            63.4    73.0
          DeepSWE 1.1                                   13.3    42.2
          LiveCodeBench v6                              83.9    90.3

        DeepSWE 13.3 -> 42.2 is the one that matters for this host: it is an
        agentic-coding benchmark, and that is what the endpoint is for.

        ⚠️ VENDOR NUMBERS, at BF16, on Qwen's harness — not measured here and
        not independent. What IS independently measured is that the quantization
        costs nothing: Quesma ran Terminal-Bench 2.1 (2026-08-26) and found
        Q4_K_M matching BF16, with their BF16 run reproducing Qwen's own figure.
        ubergarm's perplexity for the file below is 6.9938 against 6.9540 at
        BF16, i.e. +0.57%.

        ⛔ RE-MEASURE THROUGHPUT AFTER THIS SWAP RATHER THAN ASSUMING IT HELD.
        The numbers throughout this file were taken on 3.6 and every one of them
        is now a claim about a model that is no longer served. The MTP head was
        retrained, so draft acceptance — and therefore `specStages`, `n_max` and
        the whole speculation story — is the thing most likely to have moved.
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
      default = "RedHatAI/Qwen3.8-27B-INT4";
      description = ''
        Hugging Face model id a vLLM profile serves when it names none.

        RedHat's INT4 cut (AWQ smoothing then GPTQ, W4A16, group 128) because it
        is the one that publishes recovery against BF16 measured on vLLM and
        loads on the stock image; the reasons and the leaner alternatives that
        need a patched image are recorded on the vLLM profile in
        lib/inference-profiles.nix.

        ⚠️ HISTORY: until 2026-10-08 this was palmfuture/Qwen3.6-27B-GPTQ-Int4,
        a group_size=32 cut that was 20 GB on disk and left this host at 32k
        context, eager, on vLLM 0.21. Qwen3.6 was dropped from the host that
        day; the 3.6 numbers elsewhere in this file are records of how the
        current values were found, not claims about a servable model.
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
      default = 180224;
      description = ''
        The context ceiling an ik-llama profile inherits when it sets none.
        vLLM and llama-cpp profiles must state their own (nix flake check
        enforces it), for the reason the next paragraph measured.

        ⛔ null DOES NOT MEAN "let vLLM pick something sensible". It means the
        model's NATIVE length, which here is 262144, and on 24 GB that does not
        fit: engine init died with
          torch.OutOfMemoryError: Tried to allocate 1.53 GiB.
          GPU has 23.52 GiB total, of which 816.75 MiB is free.
        and systemd restarted it 189 times. Measured 2026-09-13.

        ⛔ THE CEILING IS ENGINE-DEPENDENT, so only ik inherits this one.
        Setting one number for both is a trap: 65536 runs fine under llama.cpp
        in 18.5 GB, and under vLLM it dies with
          ValueError: max seq len (65536) needs 2.3 GiB KV cache, larger than
          the available KV cache memory (1.56 GiB)
        because vLLM's weights are 20 GB against llama.cpp's 16.8. The condition
        Measured 2026-09-13 by flipping engines with the value still pinned.

        32768 is what the vLLM checkpoint actually starts at, found by bisection
        on the live card 2026-09-13. It is far below the 214k the KV arithmetic
        promises, for a reason the arithmetic could not know: this checkpoint is
        20 GB on disk, not the ~15 GB a 27B Int4 suggests, because group_size=32
        carries heavy scale/zero overhead and the MTP speculative-decoding
        weights are bundled in. 20 GB of weights on a 23.52 GiB card leaves ~3.5
        GB for everything else.

        ⛔ 180224 SINCE 2026-09-17, AND THE 202752 IT REPLACED WAS A CEILING
        MEASURED FOR A MODEL THIS HOST NO LONGER SERVES. The Qwen3.6 -> 3.8 swap
        earlier the same day inherited that number, and 3.8 does not fit in it:
        at -c 202752 the server dies at ~180k occupancy with

          created context checkpoint 32 of 32 (size = 150.659 MiB)
          CUDA error: out of memory
            cuMemCreate(&handle, reserve_size, &prop, 0)

        i.e. exactly the failure the table below documents, on a config that
        starts cleanly and answers short prompts forever. THE CEILING IS A
        PER-CHECKPOINT FACT AND DOES NOT SURVIVE A MODEL SWAP — re-measure it
        with the swap, not after the first crash.

        Re-established 2026-09-17 on ik dc310244 by driving real occupancy,
        `--ctx-checkpoints 8`, KV q4_0, mtp:n_max=8:

          ceiling   depth driven   occupancy   result
          202752       180,000        89%      CUDA OOM, RemoteDisconnected
          180224       176,000      97.7%      ok, 46.2 tok/s, 22,337 MiB
          163840       158,000      96.4%      ok, 44.3 tok/s, 22,013 MiB
          147456       144,000      97.7%      ok (n_max=12), 53.4 tok/s

        ⚠️ DRAFT DEPTH MOVES THIS CEILING DOWN, so the two options are coupled.
        n_max=12 is worth +9% at 32k (106.4 vs 97.2) and +11% deep, but it died
        at 176,000 on this same 180224 and at 158,000 on 163840. The measured
        pairing for speed over context is 147456 with n_max=12; this host ships
        180224 with n_max=8 because the 27B is the fleet's long-context model
        now that the offload profiles cap at 65k and 128k.

        ⛔ A PROMPT ABOVE THE CEILING IS SAFE; A PROMPT BELOW IT AT FULL
        OCCUPANCY IS NOT. Over-length requests are refused with HTTP 400, which
        is why lowering this number is a safe change and raising it is not.

        The original 3.6 measurement follows, kept because it is the method to
        repeat rather than a number to trust. Established 2026-09-13 by filling
        the context, not by starting the server — a config that STARTS at a
        given -c will still die partway in. Each ceiling below was driven to ~98.6%
        occupancy from a cold container, twice, with VRAM sampled continuously
        rather than read once after the fact:

          ceiling   peak VRAM   spare   near-full runs   steps below failure
          180224      23,188    1,376        ok                  16
          190464      23,492    1,072       2/2                  11
          202752      23,832      732       2/2                   5
          210944      24,018      546       2/2                   1
          212992         ---      ---   CUDA OOM, exit 139        0

        212992 is the measured wall: it starts, then dies after reaching the
        210,944 checkpoint. The client sees RemoteDisconnected and the container
        SIGSEGVs; systemd restarts it, losing the request. That failure arrives
        precisely when the context is genuinely full — i.e. during the long
        agentic turn the context was raised for — so it will never show up in
        light use.

        202752 rather than the maximum 210944 because of the LAST column, not
        the memory one. 210944 is reproducibly fine today and sits ONE 2048-step
        from the wall; a driver update, an ik rebuild, or an allocator change
        moves that wall and turns a working deployment into a crashing one.
        202752 keeps five steps of slack and 40% more headroom for 8,192 fewer
        tokens.

        ⚠️ Margin is NOT what makes this safe against gaming. A game takes
        gigabytes; no amount of spare megabytes survives that. Exclusivity is
        gaming-arbitration's job, and that has not been tested end to end.

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
      default = "q4_0";
      description = ''
        KV cache type for ik-llama profiles that set no kvType. llama-cpp
        profiles default to q8_0 instead; see the last paragraph.

        ⛔ q4_0 IS WHAT BUYS THE CONTEXT, AND IT MEASURABLY COSTS NOTHING.
        At q8_0 the KV cache is ~38 KiB/token and 202752 does not fit at all.

        The obvious worry is that halving KV precision quietly degrades output.
        Tested 2026-09-13 head to head at ~43k tokens — a length BOTH hold, so
        KV precision is the only variable — with identical prompts and greedy
        decoding, three trials each:

          task                          q8_0   q4_0
          multi-needle w/ 4 distractors  3/3    3/3
          verbatim quotation             3/3    3/3
          cross-document synthesis       3/3    3/3
          long-code bug find             3/3    3/3
          long-form degeneration         3/3    3/3
          tool call under load           3/3    3/3

        Recall was also perfect at 15/50/85% depth out to 174k tokens, and a
        request at 99.0% occupancy returned the right answer.

        ⚠️ 18/18 against 18/18 is a CEILING EFFECT. It shows q4_0 is not worse
        at this difficulty; it cannot prove the two are identical. A harder
        battery might separate them.

        ⛔ DO NOT "SAVE MORE" WITH iq4_nl. It has the same footprint as q4_0, so
        it buys nothing, and on a 200k coding prompt it degenerated into
        repeating `1.` for the whole budget while reporting 43.3 tok/s against
        q4_0's 28.5. A faster number for useless output. Judge a KV type by
        reading what it produced, never by its throughput.

        llama.cpp spells KV quantisation differently from vLLM: q8_0 / q4_0
        rather than fp8. q8_0 remains right for the llama-cpp path, which runs
        at 65536 where the cache is not the binding constraint.
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
      default = "vllm/vllm-openai@sha256:5f5e535216848d0c52159c8c13a0af04be5f6fe1a84e79914300610796f76d40";
      description = ''
        Digest-pinned, matching how ./wolf/image-config-policy.nix pins Wolf. A
        tag would let an upstream rebuild change inference behaviour with no
        commit here, which is precisely the drift this fleet's checks exist to
        prevent.

        This digest is v0.30.0 (transformers 5.17.0), the version every vLLM
        number in lib/inference-profiles.nix was measured on, 2026-10-07. It
        replaced v0.21.0, which predates Qwen3.8 and was pinned for the Qwen3.6
        GPTQ checkpoint's MTP weights. Both were removed from the host on
        2026-10-08 when Qwen3.6 was dropped.
      '';
    };

    ikLlamaImage = lib.mkOption {
      type = lib.types.str;
      default = "ik-llama:dc310244";
      description = ''
        Locally built, so this is a tag rather than a digest — the one image
        here that is not content-addressed. The git revision is baked in at
        /BUILD_REV and reported by `--version`, which is what makes a benchmark
        number traceable; record it alongside any result.

        ⛔ PIN THE REVISION TAG, NOT `local` OR `next`. Both of those are
        floating names that the rebuild script reassigns, so a config pinning
        one would silently change what it deploys the next time anything is
        built. `ik-llama:3bb386e` is still on the host as the rollback.

        Rebuilt to dc310244 (upstream main, 2026-09-17) from 3bb386e
        (2026-09-10) for CORRECTNESS first and speed second:

          ik #2460 — the PLE n-gram history resets to EOS after any rewind,
            corrupting the first n-1 tokens after speculative rejection OR
            plain server prefix reuse. Prefix reuse is the prompt cache, i.e.
            every warm agent turn, and the symptom is bad output rather than
            an error.
          ik #2442 — CPU flash-attention work buffer under-reserved, heap
            corruption on the decode path, thread-count dependent.
          ik #2403 — lets a predictor-only MTP companion load without
            token_embd, which is what makes the 1.8 GiB shared draft head in
            lib/inference-profiles.nix usable at all.

        ⚠️ THE SPEED CLAIMS FOR THE REBUILD DID NOT REPRODUCE HERE. #2374 (QSA)
        and #2375 (op fusions) are reported at +3-4% each; measured on this box
        the offload model went 21.8 -> 21.9 tok/s at 32k and the 27B 115.1 ->
        118.0, both inside run-to-run noise. The rebuild earned its place on the
        two correctness fixes and on unlocking MTP, not on throughput.

        Built by /root/ik-rebuild.sh on the host: compiled under
        `docker run --gpus=all` with the flags read out of the previous image's
        CMakeCache (Release, CUDA arch 89, GGML_NATIVE, LLAMA_CURL), then
        wrapped by /srv/inference/ik/Dockerfile. Verified before this bump by
        running scripts/inference-ab.py against BOTH models on the new image.
      '';
    };

    ikLlamaServer = lib.mkOption {
      type = lib.types.str;
      default = "/opt/ik/build/bin/llama-server";
      description = ''
        The server binary INSIDE the ik image.

        ⛔ NAMED BECAUSE THE ENTRYPOINT IS OVERRIDDEN. Model switching works by
        passing this path as `--entrypoint` from the host-side launcher, so the
        path the image would have used by itself is no longer consulted. Read it back out of the image after any
        rebuild that moves the build directory:
          docker image inspect ik-llama:local --format '{{.Config.Entrypoint}}'
        Verified 2026-09-17 against the deployed image (rev 3bb386e).

        ⚠️ A wrong value here fails at exec inside the container, so the unit
        flaps with no server log at all — check `docker logs ikllama` rather
        than the endpoint when a switch produces silence.
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
        draft wins — which argued, at 65536, for raising this toward upstream's
        16 if long prompts became the norm.

        ⛔ THAT ADVICE IS NOW A CRASH. maxModelLen is 202752, and at that
        context the draft depth is bounded by VRAM rather than by throughput:
        each step reserves ~150 MiB of recurrent-state checkpoints, so raising
        8 to upstream's 16 adds roughly 1.2 GB against ~730 MiB of headroom.
        The server would still START — it is a near-full request that dies, with
        CUDA OOM and exit 139. Measured on the neighbouring ceiling: at 210944,
        n_max=9 and n_max=10 both fail. 8 is the maximum that fits, not a
        preference, and it measured 96.8 tok/s at 180224 against 99.4 at the old
        65536/q8_0/n_max=8 baseline — so the context cost almost nothing.

        Lowering it is safe; raising it requires re-running the near-full
        occupancy test at the configured context before trusting it.

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
      default = null;
      description = ''
        `-mtprot`: requantize the MTP head's output tensor at load time.

        ⛔ null SINCE THE 3.8 SWAP, AND SETTING IT AGAIN WOULD COST 645 MiB FOR
        NOTHING. ubergarm's Qwen3.8 cut ships the MTP head already at iq4_ks —
        the card says so in as many words ("extra `iq4_ks` mtp head so no need
        for `-mtprot iq4_ks`") — so the flag would re-do work already baked into
        the file.

        On the 3.6 file it was worth 8% on code and 10% on prose (98.0 -> 106.1,
        82.6 -> 90.0), measured 2026-09-13, because that cut did NOT carry the
        requantized head. Kept as an option for exactly that case: a future GGUF
        without one.

        ⚠️ Whether it is needed is a PROPERTY OF THE FILE, not of the engine, so
        re-read the new model card on every swap. The server prints what it did:
          Creating extra output tensor of type iq4_ks for MTP usage.
          Additional memory required is 645.09 MiB
        Absence of that line with this option set to null is the expected state
        now; presence of it means the flag is doing work the file already did.

        Only meaningful when `specStages` contains an mtp stage; it is the MTP
        head's tensor, not the model's.
      '';
    };

    chatTemplateKwargs = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = ''{"enable_thinking": false}'';
      example = "null";
      description = ''
        JSON passed to `--chat-template-kwargs`, setting the SERVER-SIDE default
        for template variables. null omits the flag.

        ⛔ THIS EXISTS BECAUSE HOME ASSISTANT CANNOT SEND IT. Qwen3.6 is a
        reasoning model: left to itself it emits ~2000 characters of reasoning
        before answering, which is right for agentic coding and ruinous for a
        voice command. The fix is one request field, `chat_template_kwargs`,
        and HA's first-party llama_cpp integration exposes no way to send an
        arbitrary body field — its options are Instructions, Control Home
        Assistant, Model, Max tokens, Temperature and Top P, and nothing else.

        So the default belongs on the server, where the client that cannot ask
        gets the right behaviour for free. Measured 2026-09-13 on this host,
        identical prompt:

          no field sent (what HA gets)        1.4s   0 reasoning chars
          client sends enable_thinking=true   5.2s   2094 reasoning chars
          client sends enable_thinking=false  1.4s   0 reasoning chars

        ⛔ AND THE OVERRIDE DIRECTION IS THE WHOLE POINT — verify it still
        holds after an engine bump. A server default that could not be
        overridden would make reasoning unavailable to everything, which is the
        wrong trade for a host whose other job is agentic coding. Because it CAN
        be overridden, the asymmetry is free: HA gets fast answers it never has
        to configure, and any client that wants reasoning asks for it with

          "chat_template_kwargs": {"enable_thinking": true}

        Do not "fix" slow voice by disabling reasoning in the client instead.
        There is no client to configure — that is the entire problem.
      '';
    };

    ctxCheckpointsInterval = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = 1024;
      description = ''
        `--ctx-checkpoints-interval`: how many tokens between snapshots.

        Paired with `ctxCheckpoints` below: fewer snapshots covering the same
        history means each must span more tokens. null omits the flag.
      '';
    };

    ctxCheckpoints = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = 8;
      description = ''
        `--ctx-checkpoints`: how many recurrent-state snapshots to retain.

        ⛔ 8 RATHER THAN THE UPSTREAM 32 BECAUSE 32 IS A CRASH AT DEPTH, AND IT
        CRASHES ONLY WHEN THE CONTEXT IS NEARLY FULL. Each snapshot is ~150 MiB
        of VRAM on this model and they accumulate AS THE CONTEXT FILLS, so the
        server starts fine, answers short prompts forever, and then dies:

          created context checkpoint 32 of 32 (size = 150.659 MiB)
          CUDA error: out of memory
            cuMemCreate(&handle, reserve_size, &prop, 0)

        32 x 150 MiB is ~4.8 GB on a card that has ~1.8 GB spare once the
        weights, KV and compute buffers are resident. Measured 2026-09-17.

        ⚠️ THIS ALONE DID NOT RAISE THE CEILING, so do not read it as the fix
        for `maxModelLen`. With 8 checkpoints -c 202752 still died at 180k
        occupancy; the ceiling had to come down to 180224 as well. Both changes
        are needed and neither substitutes for the other.

        0 disables them, which measured +3% on
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
        rated this a win. This host serves a chat assistant, so keep snapshots —
        just not 32 of them.
      '';
    };

    powerLimitWatts = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      default = 250;
      description = ''
        GPU power cap held WHILE INFERENCE IS RUNNING, in watts. null disables
        the cap entirely and leaves the card at its factory default.

        ⛔ THE CARD NEVER ASKED FOR 450 W. Measured on this host 2026-09-17 with
        the 27B served and a fixed prompt at every limit, two passes agreeing
        within 0.5%:

          limit   decode   prefill   avg draw   tok/J
          450 W    141.0     2166      277 W    0.507
          350 W    141.1     2092      261 W    0.541
          300 W    140.6     2002      246 W    0.573
          250 W    139.1     1878      221 W    0.630
          200 W    131.6     1524      182 W    0.722
          175 W    117.6     1330      162 W    0.727

        Peak draw at the 450 W default was 335 W: the stock limit is headroom
        this workload cannot use, because DECODE IS BOUND BY VRAM BANDWIDTH and
        clocks above what the memory can feed are burned for nothing. Prefill is
        the compute-bound half and is what actually degrades as the cap falls.

        Energy for one unit of real work — a 4k prompt plus 400 generated
        tokens, counting both phases:

          450 W   4.69 s   1299 J
          250 W   5.01 s   1107 J   -15% energy, +7% time
          200 W   5.66 s   1030 J   -21% energy, +21% time
          175 W   6.41 s   1038 J   past the knee, worse on both

        250 rather than 200 because prefill is time-to-first-token in an agent
        loop, and 200 W costs 30% of it to save a further 6% of energy. Set 200
        here if the machine is running batch work nobody is waiting on.

        ⚠️ MEASURE WITH ONE PROMPT ACROSS ALL LIMITS. A first sweep varied the
        prompt per limit and produced a reproducible 13% "dip" that followed the
        THIRD POSITION IN THE SEQUENCE rather than any wattage — speculative
        decoding makes decode speed depend on draft acceptance, so a varying
        prompt measures the prompt. scripts/power-sweep.py pins the seed.
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

    profileStateFile = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/nardol-inference/profile";
      description = ''
        Durable selected-profile file shared by the model switcher, inference
        entrypoint, and admission/status service. Keep this outside stateDir so
        a model-cache replacement cannot silently reset the selected profile.
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
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0750 root root -"
      # Survives reboots on purpose: a model chosen from the menu should still be
      # the model served after nardol suspends, wakes, or reboots. /run would
      # reset the choice to the default at every wake, which on a host that
      # suspends whenever it is idle means "almost always".
      "d ${profileStateDir} 0755 root root -"
    ];

    environment.systemPackages = [ nardolModel ];

    # ⛔ HAND-WRITTEN RATHER THAN virtualisation.oci-containers, BECAUSE THE
    # IMAGE IS A RUNTIME CHOICE. oci-containers fixes one image per container at
    # build time; a vLLM profile and an ik profile need different images behind
    # the same unit name. The pre-start, stop and post-stop steps below are the
    # ones oci-containers generated for this unit, kept verbatim so docker sees
    # the same lifecycle it always has.
    #
    # ⛔ RATE-LIMIT THE RESTARTS. A misconfigured vLLM restarted 189 times on
    # 2026-09-13 before anyone looked, each attempt pulling ~15 GB of weights
    # into VRAM and dying on CUDA OOM. Unbounded retry turns a config mistake
    # into hours of GPU thrash and buries the original error under identical
    # repeats. Five failures inside ten minutes is enough to conclude it is not
    # coming up on its own.
    #
    # ⚠️ THIS ONCE NAMED THE WRONG UNIT FOR MONTHS.
    # The old expression collapsed every non-vLLM engine to "llamacpp", so with
    # engine = "ik-llama" the restart limiting was applied to
    # docker-llamacpp.service — and because setting serviceConfig on a name
    # CREATES a unit, that phantom service existed and absorbed the policy while
    # the real docker-ikllama.service ran with systemd's defaults. Verified on
    # the host 2026-09-13: StartLimitIntervalUSec=10s, not the 600s below.
    systemd.services."docker-${cfg.containerName}" = {
      description = "Inference server (engine and model chosen by the selected profile)";
      wantedBy = [ "multi-user.target" ];
      after = [
        "docker.service"
        "docker.socket"
        "network-online.target"
      ];
      wants = [ "network-online.target" ];
      path = [ config.virtualisation.docker.package ];
      preStart = "docker rm -f ${cfg.containerName} || true";
      script = launcher;
      preStop = "docker stop ${cfg.containerName} || true";
      postStop = "docker rm -f ${cfg.containerName} || true";

      # ⛔ StartLimit* ARE [Unit] DIRECTIVES AND systemd IGNORES THEM IN
      # [Service]. They lived in serviceConfig here, which rendered them into
      # the wrong section, so the rate limit was inert for every engine this
      # option has ever had — the unit ran with the 10s/5 default instead of
      # 600s/5. Found 2026-09-13 by reading the generated unit rather than the
      # Nix: `systemctl show` reported StartLimitIntervalUSec=10s while this
      # file said 600. NixOS exposes them as top-level unit options; use those
      # and they land in [Unit].
      startLimitBurst = 5;
      startLimitIntervalSec = 600;
      serviceConfig = {
        Restart = "on-failure";
        RestartSec = "30s";
        TimeoutStartSec = 0;
        TimeoutStopSec = 120;
      };
    };

    # ⛔ THE CAP IS BOUND TO INFERENCE, SO GAMING NEVER SEES ONE. bindsTo plus
    # wantedBy means this unit starts with docker-ikllama and — the part that
    # matters — STOPS with it. nardol-gaming.target Conflicts= the inference
    # unit, so claiming the GPU for a game stops inference, which stops this,
    # which restores the factory limit before the game ever renders a frame.
    # There is deliberately no gaming-side logic: two places setting the limit
    # is two places to disagree.
    #
    # ⚠️ INFERENCE MUST NOT DEPEND ON THIS. The dependency points one way: if
    # nvidia-smi is missing or the cap is refused, the endpoint still serves,
    # just at stock power. A power optimisation that can take the model offline
    # is not an optimisation.
    systemd.services.nardol-inference-powerlimit = lib.mkIf (cfg.powerLimitWatts != null) {
      description = "Hold the GPU at ${toString cfg.powerLimitWatts}W while inference runs";
      bindsTo = [ "docker-${cfg.containerName}.service" ];
      after = [ "docker-${cfg.containerName}.service" ];
      wantedBy = [ "docker-${cfg.containerName}.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        # ⚠️ RESTORE THE CARD'S OWN DEFAULT, NEVER A HARD-CODED 450. A different
        # card, a vBIOS change or a future GPU would silently be left capped at
        # someone else's number.
        ExecStart = pkgs.writeShellScript "nardol-inference-powerlimit-set" ''
          set -eu
          ${pkgs.coreutils}/bin/nproc >/dev/null
          "${config.hardware.nvidia.package.bin}/bin/nvidia-smi" -pl ${toString cfg.powerLimitWatts}
        '';
        ExecStop = pkgs.writeShellScript "nardol-inference-powerlimit-reset" ''
          set -eu
          default_w="$("${config.hardware.nvidia.package.bin}/bin/nvidia-smi" \
            --query-gpu=power.default_limit --format=csv,noheader,nounits | ${pkgs.coreutils}/bin/head -1)"
          "${config.hardware.nvidia.package.bin}/bin/nvidia-smi" -pl "''${default_w%%.*}"
        '';
      };
    };

    networking.firewall.interfaces.eth0.allowedTCPPorts = [ cfg.port ];
  };
}
