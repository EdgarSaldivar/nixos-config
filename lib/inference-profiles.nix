# Servable models for nardol's inference endpoint, as data.
#
# ⛔ ONE FILE BECAUSE TWO HOSTS NEED THE SAME LIST AND THEY CANNOT SEE EACH
# OTHER. hosts/nixos/nardol/inference.nix turns these into container arguments;
# pkgs/amon-din.nix turns them into a menu on dol-amroth. A menu offering a
# model nardol cannot serve is worse than no menu, and that is exactly what two
# hand-maintained lists produce after the first edit that only touches one.
#
# ⚠️ EVERY FIELD HERE IS A PER-MODEL FACT, NOT A PREFERENCE. Context ceilings,
# KV type and draft depth were measured against ONE checkpoint and do not
# transfer: 202752 is a measured wall for the 27B on 24 GB (see `maxModelLen` in
# inference.nix), and a 125B whose experts live in system RAM has no relationship
# to it. A profile that inherits the wrong ceiling starts fine and dies when the
# context fills — the failure this fleet has already been bitten by once.
#
# null means "inherit the module-level option", which is how the GPU-only 27B
# keeps every default documented in inference.nix instead of restating it here.
#
# ⛔ A PROFILE CHOOSES ITS ENGINE, NOT JUST ITS MODEL. Switching to a vLLM
# profile replaces the ik-llama server with vLLM inside the SAME systemd unit
# (see the launcher in inference.nix), because a checkpoint only runs on the
# engine whose format it is in: IQ4_KS exists only in ik, safetensors INT4 only
# in vLLM. A field that does not apply to a profile's engine stays null, and
# nix flake check refuses a profile that sets one anyway.
let
  # Every field a consumer may read, so a profile names only what it uses.
  fields = {
    engine = "ik-llama";
    # GGUF engines (ik-llama, llama-cpp).
    ggufFile = null;
    mmproj = null;
    kvType = null;
    specStages = null;
    mtpRequantizeOutputTensor = null;
    cpuMoe = null;
    draftModel = null;
    batchSize = null;
    ubatchSize = null;
    # vLLM.
    model = null;
    quantization = null;
    gpuMemoryUtilization = null;
    enforceEager = null;
    image = null; # null: the module's pinned vLLM image
    reasoningParser = null; # null: qwen3
    toolCallParser = null; # null: the module's toolCallParser
    # All engines.
    maxModelLen = null;
    extraArgs = [ ];
  };
in
{
  # The profile served when nothing has been chosen, and the fallback whenever
  # the saved choice names a profile that no longer exists.
  default = "qwen3.8-27b";

  profiles = builtins.mapAttrs (_: p: fields // p) {
    # ⛔ KV STAYS q4_0. The KLD literature says q4_0 KV does its damage on long
    # documents and tool calls — exactly this workload — and iq4_nl is the same
    # 4.5 bits per value with a better error profile, so it looked like a free
    # upgrade. Tested 2026-09-17 at 180224 with identical prompts: needles with
    # decoys 3/3 for both at 32k/100k/170k, tool calls valid for both, codegen
    # identical, and on the one subjective task q4_0 got the right answer
    # (systemd StartLimit* belong in unitConfig) while iq4_nl blamed casing and
    # was wrong.
    #
    # No measurable benefit, one qualitative point against. A free change that
    # does not measure better is not free — it is an unreviewed difference.
    #
    # ⛔ THE DEFAULT SEES. Qwen3.8 is natively vision-language, so the vision
    # encoder rides along with every request instead of living in a separate
    # profile — that profile was this one plus `mmproj` and a smaller window,
    # and two profiles for one model is a choice nobody should have to make.
    #
    # ⛔ 155648, NOT 180224, AND THE ENCODER IS WHY. Driven 2026-10-08 with the
    # projector loaded, q4_0 KV + -khad, filling to depth and THEN sending 16
    # frames (the worst order — the encoder's buffer lands on a full cache):
    #
    #   -c        depth     peak after 16 frames   headroom
    #   180224       —      OOMs at depth (BF16 projector, measured 2026-10-07)
    #   163840   161,295        23,910 MiB          ~170 MiB  too thin
    #   155648   152,655        23,748 MiB          ~340 MiB  shipped
    #
    # Text-only work loses 25k of window for it.
    "qwen3.8-27b" = {
      label = "Qwen3.8-27B";
      summary = "27B dense with vision, entirely on the 4090. 155k, ~106 tok/s.";
      ggufFile = null; # inherits inference.nix's default, which is this model
      # Q8_0 rather than BF16: ~290 MiB lower at the peak and answers the same
      # or better on every clip tested (2026-10-07).
      mmproj = "/srv/inference/gguf/vision/mmproj-Qwen3.8-27B-Q8_0.gguf";
      maxModelLen = 155648;
      kvType = null;
      specStages = null;
      mtpRequantizeOutputTensor = null;
      cpuMoe = null;
      draftModel = null;
      batchSize = null;
      ubatchSize = null;
      extraArgs = [ ];
    };

    # The same architecture with refusals removed: 0bserverx's RVN build, three
    # ARA (heretic) passes over Qwen3.8-27B; its card reports KL 0.0085 against
    # the base. Added at the owner's request 2026-10-08 after a tried-profile run.
    #
    # Everything not set here is the default's, deliberately: q4_0 KV + -khad,
    # MTP n_max=8, ctx checkpoints, Qwen sampling, non-thinking. The `-mtp` file
    # is the one with the draft head embedded; the plain RVN file served at
    # ~50 tok/s, this one at 81-132 (MTP acceptance 0.73-0.81). The projector
    # is the default's own file: the repo ships a byte-identical copy (same
    # sha256), since ARA edits only text layers.
    #
    # ⛔ 147456, NOT THE DEFAULT'S 155648. Same method as the default (fill to
    # depth, then 16 frames on the full cache), 2026-10-08:
    #
    #   -c        depth     peak after 16 frames   headroom
    #   155648       —      CUDA OOM at depth
    #   147456   145,269        23,906 MiB          ~660 MiB  shipped
    #   139264   137,169        23,678 MiB          ~890 MiB
    #
    # Needle at the start recalled and the 5 people counted at both depths.
    # Q4_K_M weighs 80 MB more than the default's IQ4_KS and its compute
    # buffers more again; the ceiling does not transfer between quants.
    #
    # Fetched with `hf download 0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF
    # RVN-Q4_K_M-multilingual-mtp.gguf --local-dir /srv/inference/gguf/heretic`.
    "qwen3.8-27b-heretic" = {
      label = "Qwen3.8-27B Heretic (abliterated)";
      summary = "The 27B without refusals (RVN ARA), MTP + vision. 147k, ~80-130 tok/s.";
      ggufFile = "/srv/inference/gguf/heretic/RVN-Q4_K_M-multilingual-mtp.gguf";
      mmproj = "/srv/inference/gguf/vision/mmproj-Qwen3.8-27B-Q8_0.gguf";
      maxModelLen = 147456;
    };

    # ⛔ ONLY MODELS THAT FIT IN VRAM. The Qwen3.8-Flash-Next 125B profiles
    # (flash-next, flash-next-128k: 73 GiB of experts in system RAM, ~21-35 tok/s,
    # up to 141 s cold TTFT at depth) were removed 2026-10-08 by request; their
    # measurements — offload tuning, MTP draft heads, the agentic comparison
    # against the 27B — are in git history before that date.
    # ── VISION ────────────────────────────────────────────────────────────
    #
    # Qwen3.8-27B is natively vision-language; these profiles add the vision
    # encoder rather than a different model. All three measured 2026-10-07 on
    # the same frames and clips (a 40 s indoor birthday, a 15 s outdoor
    # portrait-phone dance), greedy, enable_thinking off, 250 W cap.
    #
    # ⛔ SAMPLED FRAMES BEAT vLLM's NATIVE VIDEO INPUT ON THIS FOOTAGE. Native
    # video is ~3x cheaper in tokens (958 vs 2536 for the birthday) and placed
    # the candle-blowing at "00:30" against a true 25-35 s — but on the dance
    # clip it called the scene "a traditional stick game" with a ball in 2 of 3
    # server configs, with no stick and no ball in the footage. Frames got that
    # clip right every time on every engine. Use video for "when", frames for
    # "what".
    #
    # The projector lives under /srv/inference/gguf/vision and is not in git;
    # fetched with `hf download` from ggml-org/Qwen3.8-27B-GGUF.
    #
    # NARROWED 2026-10-08 from six vision-capable profiles to three: the
    # default (above, ik), the vLLM batch profile and GLM below. Dropped, with
    # their measurements in git history: a separate vision profile (folded into
    # the default), a 262k UD-Q3_K_XL profile (curation prompts are 2-5k
    # tokens; nothing needed the window) and a stock-image RedHat INT4 fallback
    # (re-download RedHatAI/Qwen3.8-27B-INT4 if the patched image ever breaks).

    # vLLM, for BATCH curation: several clips at once. The one thing vLLM does
    # that ik on this host does not — ik runs --parallel 1 for the hybrid-state
    # corruption noted above, so it is strictly one request at a time. vLLM
    # also enforces `response_format: json_schema`; ik returned an empty body.
    #
    # JRamirez-UAB/Qwen3.8-27B-GPTQ-W4A16-embed-int4-24GB: W4A16 body, INT4
    # token embeddings and lm_head, INT8 MTP head — 15.03 GiB resident WITH the
    # drafter, against 17.71 GiB without one for the stock profile's RedHat cut.
    # Quantizing the heads is what lets MTP fit on vLLM at all.
    #
    # ⛔ NEEDS THE PATCHED IMAGE (./vllm-embedq/Dockerfile beside inference.nix).
    # Stock vLLM 0.30 never passes quant_config to Qwen3.5's
    # VocabParallelEmbedding, so INT4 embeddings fail to load. Pinned by local
    # image ID: a rebuild produces a new ID and this must be updated with it.
    #
    # Measured 2026-10-08 (250 W, unique prompts, 16-frame curation job):
    #
    #   config                         KV tokens   clips/min   allocator OOMs
    #   max-num-seqs 6, 64k, MTP 3       122,880   30.1 / 31.6 (6 / 12 queued)  0
    #   single stream, 131k, MTP 3       144,584   107-126 tok/s decode         0
    #
    # Single-stream fill to 129,615 tokens: needles 3/3. MTP acceptance ~3.2 of 4.
    #
    # ⛔ MTP STAYS AND PREFIX CACHING GOES, MEASURED 2026-10-08 against two
    # audits that suspected both. vLLM #55533 reports MTP slower than none at
    # batch >= 4 on a 4090 D; not here. Prefix caching is where the open
    # hybrid-model corruption reports live (#53912, #55766) and it bought
    # nothing — every clip's images are unique:
    #
    #   MTP   prefix cache          clips/min @6 / @12   KV tokens
    #   3     on                    30.9 / 31.3          122,880
    #   3     off                   30.2 / 31.4          129,835
    #   off   off                   26.2 / 27.8          196,608
    #   1     on (vLLM default)     29.0 / 30.0          137,497
    #
    # ⚠️ "off" means --no-enable-prefix-caching, verified in the startup config
    # (enable_prefix_caching=False, no Mamba align mode). A first A/B the same
    # day merely omitted the flag; vLLM 0.30 defaults it on, so those "off"
    # rows had it on and are not in this table.
    #
    # ⚠️ LOWEST FIDELITY OF THE THREE 4-BIT CUTS. Its own card reports mean KLD
    # 0.040 against BF16 (exllamav3 qbench), and it counted 4 people where
    # RedHat and ik both counted the 5 in the same 16 frames. Use the stock
    # profile when a result matters more than throughput.
    #
    # ⛔ IMAGES ONLY, AND THE IMAGE CAP IS WHY. vLLM 0.30 applies a flat
    # `max_pixels` to images per item but to video ACROSS ALL FRAMES, so the 1 MP
    # cap that keeps encoder profiling inside 24 GB starves video to ~128x224 —
    # which is what made native video hallucinate a "stick game" (652 tokens
    # for a 15 s clip). Native video works with a video-sized budget and images
    # disabled (tested: image 0, qwen3_vl backend, fps 2, 32 frames,
    # max_pixels 3.7-7.4M, do_sample_frames false -> 1.8-3.8k tokens, all clips
    # correct). Scoped per-modality kwargs (vLLM #56372) are not in 0.30, so the
    # two cannot share one server; this one serves frames.
    "qwen3.8-27b-vllm-batch" = {
      engine = "vllm";
      label = "Qwen3.8-27B vLLM (batch vision)";
      summary = "vLLM + MTP, 6 clips at once: ~31 clips/min, 64k each. Patched image.";
      image = "sha256:e2c55b754dff1773514c0d539b4c7122ca844907df05242bdbf1fe1056b5be8b";
      model = "JRamirez-UAB/Qwen3.8-27B-GPTQ-W4A16-embed-int4-24GB";
      maxModelLen = 65536;
      gpuMemoryUtilization = 0.92;
      enforceEager = false;
      extraArgs = [
        "--max-num-seqs"
        "6"
        "--max-num-batched-tokens"
        "4096"
        "--limit-mm-per-prompt"
        ''{"image":16,"video":0}''
        "--mm-processor-kwargs"
        ''{"max_pixels":1048576}''
        "--speculative-config"
        ''{"method":"mtp","num_speculative_tokens":3}''
        # ⛔ vLLM's --generation-config auto takes the checkpoint's
        # generation_config.json, which carries Qwen's THINKING preset (temp
        # 1.0, top_p 0.95). Thinking is off here, so requests that send no
        # sampling ran hotter than Qwen's non-thinking recommendation. See
        # `ikSampling` in inference.nix for the preset and why no presence
        # penalty.
        "--override-generation-config"
        ''{"temperature":0.7,"top_p":0.8,"top_k":20,"min_p":0.0}''
      ];
    };

    # ── QWEN3.8-27B HERETIC-ARA, QUANTIZED HERE ───────────────────────────
    #
    # heretic-org/Qwen3.8-27B-heretic-ara (abliterated), quantized on nardol
    # 2026-10-10: GPTQ W4A16 g64 on the language-model linears, INT8 embed,
    # lm_head and MTP head, vision tower BF16. Calibrated on chat-template-
    # rendered agentic, coding, reasoning, chat and multilingual traces.
    # Recipe, scripts and the measurements below are on the model card.
    #
    # Fidelity against BF16, scored on assistant tokens only: mean KLD 0.077,
    # and 0.007-0.011 on code and tool use. Same layout as the batch profile's
    # checkpoint, but INT8 rather than INT4 vocabulary and better calibration
    # (that card reports 0.040 on a different harness, so not comparable).
    #
    # ⛔ --max-num-batched-tokens 4096 IN BOTH, NEVER 8192. Measured
    # 2026-10-10: with 8192-token prefill chunks the engine ran out of memory
    # partway into a long prompt at 0.95, 0.92 and 0.90 (the last 16k tokens
    # in). vLLM's startup profile says 1.44 GiB of activations; the MLP gate_up
    # marlin_gemm on a real 8192 chunk needs far more. `nardol-model try` fixes
    # 8192, which is why a tried profile of this model only held at gpu 0.85
    # and 37,888 context.
    #
    # Long, 1 sequence, measured with exactly these args:
    #
    #   KV            pool   fill to 97%                         allocator
    #   5.5e9 / 128k  132k   OK at 90%; HUNG mid-prefill at 97%  retrying at 90%
    #   5.0e9 / 112k  117k   8/8 needles at 110,823 + image      no retries
    #
    #   decode 117-121 tok/s, MTP mean acceptance 3.24-3.35; prefill
    #   1,780 tok/s; peak 23,784 MiB. Verified from a fresh download of the
    #   published repo, plus tool calls, follow-up turn, image and video.
    #
    # ⛔ PREFIX CACHING ON, AGAINST THE MODULE DEFAULT. Turned off module-wide
    # 2026-10-08 for batch vision: no gain there, and it is the trigger in the
    # open hybrid-model corruption reports. Coding resends the same files, so
    # here it is the difference. Measured on a 118k prompt: cold 67.8 s, then
    # 3.1 s for the identical repeat (byte-identical output) and 3.1 s for a
    # follow-up turn, recall 8/8 each time. Watch for corrupted output anyway;
    # dropping this flag puts the module default back.
    #
    # Images only (video 0), for the reason on the batch profile above.
    # `--kv-cache-memory-bytes` sizes the KV; the utilisation below is only
    # what vLLM checks free memory against at start (0.90 was measured).
    "qwen3.8-27b-heretic-ara" = {
      engine = "vllm";
      label = "Qwen3.8-27B heretic-ara vLLM (112k)";
      summary = "Own W4A16 quant, 112k context, prefix cache, ~120 tok/s. Patched image.";
      image = "sha256:e2c55b754dff1773514c0d539b4c7122ca844907df05242bdbf1fe1056b5be8b";
      model = "razarath/Qwen3.8-27B-heretic-ara-GPTQ-W4A16-g64-int8-embed";
      maxModelLen = 114688;
      gpuMemoryUtilization = 0.90;
      enforceEager = false;
      extraArgs = [
        "--max-num-seqs"
        "1"
        "--max-num-batched-tokens"
        "4096"
        "--kv-cache-memory-bytes"
        "5000000000"
        "--enable-prefix-caching"
        "--limit-mm-per-prompt"
        ''{"image":16,"video":0}''
        "--mm-processor-kwargs"
        ''{"max_pixels":1048576}''
        "--speculative-config"
        ''{"method":"mtp","num_speculative_tokens":3}''
        "--override-generation-config"
        ''{"temperature":0.7,"top_p":0.8,"top_k":20,"min_p":0.0}''
      ];
    };

    # The same checkpoint in the batch profile's shape. Measured 2026-10-10:
    # 101,395 KV tokens; the vtest curation job (6 clips x 16 frames at once)
    # 31.7 and 31.0 clips/min against the batch profile's ~30.5; 6 concurrent
    # 16k needles (95% of the pool) 8/8 each; peak 23,602 MiB, no allocator
    # retries. Prefix caching stays off, as on the batch profile.
    "qwen3.8-27b-heretic-ara-batch" = {
      engine = "vllm";
      label = "Qwen3.8-27B heretic-ara vLLM (batch vision)";
      summary = "Own W4A16 quant, 6 clips at once: ~31 clips/min, 64k each. Patched image.";
      image = "sha256:e2c55b754dff1773514c0d539b4c7122ca844907df05242bdbf1fe1056b5be8b";
      model = "razarath/Qwen3.8-27B-heretic-ara-GPTQ-W4A16-g64-int8-embed";
      maxModelLen = 65536;
      gpuMemoryUtilization = 0.92;
      enforceEager = false;
      extraArgs = [
        "--max-num-seqs"
        "6"
        "--max-num-batched-tokens"
        "4096"
        "--limit-mm-per-prompt"
        ''{"image":16,"video":0}''
        "--mm-processor-kwargs"
        ''{"max_pixels":1048576}''
        "--speculative-config"
        ''{"method":"mtp","num_speculative_tokens":3}''
        "--override-generation-config"
        ''{"temperature":0.7,"top_p":0.8,"top_k":20,"min_p":0.0}''
      ];
    };

    # ── NOT QWEN ──────────────────────────────────────────────────────────
    #
    # GLM-4.6V-Flash (zai-org, 9B dense, GLM backbone), quantized to FP8 at
    # load — Ada runs FP8 natively, so no checkpoint quantization is involved.
    # 10.92 GiB resident. Verified through the switcher 2026-10-08 with this
    # profile's flags: 474,096 KV tokens, and on the 16-frame curation job
    # 50.1 / 67.5 / 74.6 clips/min at 6 / 8 / 16 in flight, zero allocator
    # OOMs — ~2.4x the Qwen batch profile.
    #
    # Kept as a cheap FIRST PASS. Measured 2026-10-08 on the same battery as the
    # Qwen profiles: it was the only model to find all 15 family placements
    # (the baby in the wide shot), the only one to fill a schema with sensible
    # values unprompted, and it caught the wedding dancers. Decode is 70 tok/s,
    # slower than Qwen; its advantage is concurrency, not per-request speed.
    #
    # ⛔ LIKE EVERY MODEL TESTED, IT CANNOT SAY "NONE OF THESE". Withhold the
    # right person's reference photo and it names the nearest lookalike. Never
    # use any model here for identity without face recognition in front of it.
    #
    # ⚠️ It wraps answers in <|begin_of_box|>...<|end_of_box|>; strip them.
    # Its bigger sibling GLM-4.6V (106B-A12B) was tested via llama.cpp with
    # experts in RAM: ~9 tok/s, 30-90 s to first token, and no better at any
    # test. Not worth keeping.
    #
    # ⚠️ THE ABLITERATED BUILD, NOT STOCK, SINCE 2026-10-08:
    # 3MPER0RR/GLM-4.6V-Flash-3MPER0RR-abliterated (community upload, BF16,
    # stock layout), chosen to avoid refusals on sensitive-but-normal family
    # footage. Verified against zai-org/GLM-4.6V-Flash before adopting, with no
    # prompts involved:
    #   * weights: all 181 vision tensors byte-identical; 445 of 523 text
    #     tensors identical; the other 78 are o_proj + down_proj in 39 layers,
    #     each a ~1.5-2% rank-1 edit (99% of the change in one direction) —
    #     the signature of removing one direction, not a fine-tune;
    #   * behaviour on this file's normal battery (people, curation, schema,
    #     tools): indistinguishable from stock, same 69.5-69.9 clips/min at 8,
    #     same 10.9 GiB. The profile name is unchanged so the menu and the
    #     saved selection carry over. zai-org/GLM-4.6V-Flash stays on disk;
    #     put it back in `model` to return to stock.
    "glm-4.6v-flash" = {
      engine = "vllm";
      label = "GLM-4.6V-Flash 9B (abliterated)";
      summary = "Non-Qwen 9B vision model, FP8, ~8 clips at once. Fewer refusals.";
      model = "3MPER0RR/GLM-4.6V-Flash-3MPER0RR-abliterated";
      quantization = "fp8";
      maxModelLen = 32768;
      # ⛔ 0.65, NOT 0.92: THE REST OF THE CARD IS PALANTÍR'S. Its face,
      # speech and search models stay resident beside GLM so the agent can call
      # them without swapping — the reason a 9B is the agent's model. Measured
      # 2026-10-08 at 0.70: 203,264 KV tokens, 19.2 GB peak with video.
      gpuMemoryUtilization = 0.65;
      enforceEager = false;
      reasoningParser = "glm45";
      toolCallParser = "glm45";
      extraArgs = [
        "--max-num-seqs"
        "8"
        "--max-num-batched-tokens"
        "8192"
        "--limit-mm-per-prompt"
        ''{"image":16,"video":1}''
        # ⛔ CAP THE VIDEO PIXEL BUDGET WITH `size`, NOT `max_pixels`. GLM's
        # video processor defaults to a 100M-pixel area per clip (9-18k tokens
        # for a 15-40 s clip, a 66k-token encoder cache reserved at start).
        # `max_pixels` shrinks vLLM's reservation but the processor ignores it,
        # so every clip then overflowed the smaller cache and was rejected;
        # `size.longest_edge` is what the processor honours. 7.37M: 4.6-4.9k
        # tokens per clip, all four test clips described correctly. Images at
        # <= 1 MP are unaffected (the same area cap applies per image).
        "--mm-processor-kwargs"
        ''{"size":{"longest_edge":7372800,"shortest_edge":12544}}''
      ];
    };
  };
}
