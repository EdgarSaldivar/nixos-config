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
    "qwen3.8-27b" = {
      label = "Qwen3.8-27B";
      summary = "27B dense, entirely on the 4090. The fast one.";
      ggufFile = null; # inherits inference.nix's default, which is this model
      maxModelLen = null;
      kvType = null;
      specStages = null;
      mtpRequantizeOutputTensor = null;
      cpuMoe = null;
      draftModel = null;
      batchSize = null;
      ubatchSize = null;
      extraArgs = [ ];
    };

    # The RAM-offload experiment: 125B total, 6B active, 512 experts of which 10
    # fire per token. It only runs here because the ACTIVE part is small — the
    # 87 GiB of weights do not fit on the card and never will, so 73 GiB of them
    # sit in system RAM and the 4090 holds attention, the dense layers and four
    # layers' experts.
    #
    # ⛔ ITS CEILING IS DDR4 BANDWIDTH, NOT THE GPU, and that is why no flag
    # below moves decode much. Measured on nardol 2026-09-17, ik rev 3bb386e,
    # unique prompts per request, cache off, 4x32 GB DDR4-3200 dual channel:
    #
    #   config                     VRAM     PP@32k   TG@32k
    #   -ncmoe 44 -ub 2048        13.7 GB      664     21.7
    #   -ncmoe 40 -ub 2048        18.6 GB      702     23.3
    #   -ncmoe 40 -ub 4096        20.6 GB     1070     23.1
    #   -ncmoe 38 -ub 4096        22.8 GB     1122     23.9
    #   -ncmoe 42 -ub 8192        22.3 GB     1404     22.5
    #   -ncmoe 40 -ub 1024        17.6 GB      267     23.0
    #
    # Decode spans 21.7-23.9 across every one of them — a 10% band — while
    # prefill spans 267-1404, a 5.3x one. Tune -ub, never expect tuning to buy
    # decode: 51 GB/s of DDR4 divided by the ~2 GB of expert weights each token
    # reads is ~25 tok/s, and these land at 85-95% of that. The only real fix is
    # faster RAM.
    #
    # ⛔ -ub 1024 IS THE TRAP, AND IT LOOKS LIKE A MEMORY SAVING. ik offloads
    # expert matmuls to the GPU for prompt processing only when the ubatch
    # reaches 32 x n_experts / n_active = 32 x 512 / 10 = 1638. Below that every
    # expert multiply runs on the CPU and prefill collapses 4x, which is the
    # single largest measured penalty on this list and costs 0.3 GB of VRAM to
    # avoid. Predicted by ik discussion #1812 and reproduced here exactly.
    #
    # ⛔ 131072 RATHER THAN MORE BECAUSE THE COMPUTE BUFFER, NOT THE KV CACHE,
    # IS WHAT RUNS OUT. KV is trivial on this architecture — 640 MiB at 32k,
    # because only every 4th layer carries full attention — but the compute
    # buffer grew 3960 -> 7848 MiB going 32k -> 128k and OOMed at -ncmoe 40:
    #   cudaMalloc failed: out of memory
    #   llama_init_from_model: failed to allocate compute buffers
    # -ncmoe 44 is what makes 128k fit, and -amb 512 does NOT rescue a tighter
    # setting — it caps a different buffer. So the context ceiling is bought
    # with expert layers, and this profile spends them: 44 on CPU, not 38.
    #
    # Long-context behaviour, same run, is the good news — decode is nearly flat
    # because the attention is hybrid:
    #   depth      32k     64k    120k
    #   PP        1027     942     820
    #   TG        21.8    21.1    20.5
    #   TTFT     30.7s   67.1s  144.6s
    #
    # ⚠️ THAT TTFT IS THE REAL COST OF THIS MODEL, NOT THE 20 tok/s. A cold
    # 120k agent turn waits two and a half minutes before the first token. It is
    # survivable only because the prompt cache works: measured 66x on turn 2
    # (13.20s -> 0.20s) by scripts/inference-ab.py, which also passed tool
    # calling 3/3 and determinism here. An agent client that busts the cache —
    # a changing header, a mode switch, stripped reasoning — turns every turn
    # into the cold number and makes this model unusable rather than slow.
    #
    # ⛔ SPECULATIVE DECODING IS WORTH +50% HERE AND IT COSTS CONTEXT. That
    # trade is the whole design of this profile, and both halves were measured.
    #
    # unsloth's main cut carries NO MTP tensors — verified by reading the GGUF
    # tensor names rather than trusting the label. But ik takes the head as a
    # SEPARATE file (`-md`, PR #2369), and unsloth publishes one. The "shared"
    # variant borrows token_embd from the target instead of carrying its own,
    # which is why it is 1.8 GiB rather than 3.9; loading it needs PR #2403,
    # merged 2026-09-14 — i.e. the image rebuild is what made this possible.
    #
    # Measured 2026-09-17 on ik dc310244, same prompts, against a no-speculation
    # control at the SAME cpuMoe so the gain is speculation and not the layer
    # shuffle:
    #
    #   config (65k ctx)               VRAM     PP@32k   TG@32k   TG@60k
    #   no speculation, -ncmoe 46     12.8 GB     1000     21.3      —
    #   MTP shared-Q4, -ncmoe 46      20.3 GB      857     26.2     25.2
    #
    # On the real battery — prose and code rather than synthetic filler — it is
    # 34.8 tok/s against 23.2 before, with tool calls 3/3 and 74.5x cache reuse.
    # The Q8 head measured identically to the Q4 one (26.5 vs 26.7), so the
    # smaller file wins on VRAM alone.
    #
    # ⛔ 65536 AND NOT 131072 BECAUSE MTP AT 128k STARTS AND THEN DIES. The
    # recurrent checkpoints grow with OCCUPANCY, not with the configured ceiling,
    # so -c 131072 -ncmoe 48 loaded happily in 58s at 23.0 GB and then killed the
    # server mid-request at depth:
    #   RemoteDisconnected: Remote end closed connection without response
    # That is the same failure this file already documents for the 27B at 212992,
    # and it arrives precisely during the long agentic turn the context was
    # raised for. This ceiling was therefore validated at 92% occupancy (60k of
    # 65k), twice, not by watching the server start.
    #
    # ⚠️ SO THERE IS A SECOND PROFILE, "flash-next-128k", FOR WHEN CONTEXT MATTERS
    # MORE THAN SPEED. It is this model without the draft head: 20.5 tok/s at
    # 120k against 25.2 at 60k here. Pick by the job, not by the bigger number.
    #
    # ⚠️ IT PINS 73 GiB OF HOST MEMORY and takes 22s doing it, which is most of
    # the startup. That memory cannot be swapped. Nothing else on this host
    # wants it today — gaming is already exclusive with inference — but a second
    # RAM-hungry service and this profile cannot coexist.
    # ⛔ -wgt 1 IS IN extraArgs ON BOTH OFFLOAD PROFILES AND IT IS NOT COSMETIC.
    # It caps the worst-case graph at one token, which shrinks the compute
    # buffer — the thing that actually runs out on this model. Measured in
    # isolation 2026-09-17 against an otherwise identical config: 2.9 GB of VRAM
    # back (20.3 -> 17.4 GB), prefill unchanged, decode +3.7% in that one run.
    #
    # ⚠️ THE VRAM IS THE REASON, NOT THE 3.7%. A later n=4 interleaved repeat put
    # run-to-run spread on an IDENTICAL config at 26.8-28.4 tok/s, i.e. ±6%, so
    # every single-run decode difference under ~5% in this file is a TIE and
    # should be read as one. The 2.9 GB is an allocation rather than a
    # measurement, and it is what buys cpuMoe 44 here and 128k survival on the
    # sibling profile. ikawrakow's own qwen4exp sweep line uses this flag.
    #
    # The other six, same method, one flag at a time (TG at 32k, baseline 27.2):
    #
    #   --ctx-checkpoints 8 --interval 1024   27.8   tie
    #   --defer-ple                           26.9   tie
    #   GGML_CUDA_NO_PINNED_WEIGHTS=1         27.3   PREFILL -32%  <- real
    #   -ser 1,6                              26.3   tie
    #   -cuda offload-batch-size=8            26.2   tie
    #   -ub 1024 + offload-batch-size=8       27.2   PREFILL -54%  <- real
    #
    # A second round, same method, also all ties: -ictk q8_0, -dsatk 1024,
    # -mqkv, --defer-ple. -mqkv looked like +5.5% in one run and came back
    # 27.55 against 27.45 over n=4 interleaved.
    #
    # ⚠️ ONLY THE PREFILL NUMBERS ARE OUTSIDE THE NOISE. Every decode difference
    # in that table is a tie; the two prefill collapses are 5-10x the spread and
    # reproduce. GGML_CUDA_NO_PINNED_WEIGHTS is the trap worth naming: it loads
    # 19s faster and costs a THIRD of prefill on every turn thereafter, which a
    # startup-time benchmark would have called a win.
    #
    # ⚠️ -ser 1,6 DESERVES ITS OWN LINE. Cutting the active experts from 10 to 6
    # is ~40% less memory traffic, and on a decode this file calls
    # bandwidth-bound it should have been the largest win on the list. It
    # measured a tie. That is evidence the decode is NOT purely bandwidth-bound
    # here — consistent with ~30 GB/s of an achievable ~44 — and it is why the
    # quality question -ser raises never has to be asked.
    # ⛔ QUALITY TESTED 2026-09-17, AND IT DID NOT BEAT THE 27B ON ANYTHING
    # MEASURED. That is the premise this profile exists on, so it is recorded
    # here rather than in a commit message nobody re-reads.
    #
    # Identical battery, identical prompts, temperature 0, n=3 on code tasks:
    #
    #   test                         27B (q4_0)   Flash-Next
    #   codegen, 8 easy tasks           24/24        24/24
    #   codegen, 4 hard tasks            9/12         9/12   <- see below
    #   needle w/ 2 decoys, at depth      3/3          2/2
    #   tool call under 32k prefix       valid        valid
    #   strict JSON                      pass         pass
    #
    # ⚠️ THE HARD TIER'S 9/12 WAS A BUG IN THE TEST, NOT THE MODELS. All three
    # configurations failed the same task, which is the shape of a broken
    # assertion rather than a model limitation: the expected value demanded
    # [5,5] where the spec as written yields [5]. Corrected, the tier is 12/12
    # everywhere and has no discriminating power at all.
    #
    # So the battery saturated. Two real defects from commit 1461ae2 were posed
    # instead, from the pre-fix code, where the ground truth is this repo's own
    # fix. BOTH MODELS MISSED BOTH, in the same way: on the gateway each flagged
    # the lease-unreachable fallthrough, which the code comments justify as
    # deliberate, rather than the retry-exhaustion path that was the actual
    # defect; on the inhibitor probe each named a generic probe failure rather
    # than the vLLM engine mismatch that triggers it.
    #
    # ⚠️ WHAT THIS DOES AND DOES NOT SHOW. It does not show the two models are
    # equal — published full-precision evals put Flash-Next well ahead
    # (Terminal-Bench 4.0 25% against 6%). It shows that no SINGLE-PROMPT battery
    # separates them, which is a statement about the battery.
    #
    # ⛔ AN AGENTIC TASK DID SEPARATE THEM, AND IT IS THE ONLY THING THAT HAS.
    # scripts/agent-probe.py gives the model one bash tool in a network-less
    # container and asks it to parse a GGUF header by hand — a binary format with
    # length-prefixed strings, typed values and nested arrays — then iterate until
    # it works. n=3 each, 22 turns, temperature 0:
    #
    #   model         solved   failure mode
    #   Qwen3.8-27B     0/3     never past the value-type dispatch; every run
    #                           mis-sized a float64 and every later offset diverged
    #   Flash-Next      1/3     solved in 5 turns once, cross-checking itself
    #                           against general.size_label; the two failures walked
    #                           13 KV entries correctly before failing on the ARRAY
    #                           type
    #
    # Both models fail in the same PLACE. Only the 125B recovers from it. That is
    # consistent with what a sparse model is: 6B active against the 27B's fully
    # dense 27B, so on one-shot reasoning the dense model does ~4x the compute per
    # token and holds its own — while the extra 119B of sparse capacity shows up
    # as breadth, which is exactly what a binary format's type table is.
    #
    # ⚠️ 1/3 IS NOT RELIABLE, AND THE VARIANCE IS THE FINDING TOO. The same model
    # at temperature 0 ran out of turns on one attempt and solved it in five on
    # the next. Any future harness needs n>=3 per task; n=1 would have reported
    # either "125B wins decisively" or "no difference" depending on which run it
    # caught.
    #
    # ⛔ AND LANGUAGE FLIPS IT. The same GGUF-parsing task, same file, same
    # sandbox, only the language changed:
    #
    #   task                          Qwen3.8-27B            Flash-Next
    #   unfamiliar API + docs (Py)    PASS 18t  37.4s        PASS  8t  81.3s
    #   parse GGUF (Python)           0/3                    1/3
    #   parse GGUF (SWIFT)            FAIL 20t 162.9s        PASS  9t 139.6s
    #
    # In Swift the 27B did not merely fail, it LOOPED: ten identical commands in
    # a row, against a harness message telling it to change approach. A less
    # common language is where the small dense model runs out, and it is the one
    # case measured here where Flash-Next also wins on WALL CLOCK -- because the
    # 27B's speed only counts on tasks it can finish.
    #
    # ⛔ TURNS ARE NOT TIME, AND THIS IS THE NUMBER TO DECIDE ON. On the task
    # both models pass, Flash-Next needs less than half the turns (8 vs 18) and
    # still takes 2.2x the wall clock, because each of its turns costs 9.9s
    # against 1.9s. Fewer, better turns lose to more, cheaper ones -- until the
    # task is hard enough that the cheap turns never arrive at an answer.
    #
    # So: the 27B stays default and wins everything within its reach; this
    # profile is for uncommon languages, binary formats and long multi-step work
    # -- the places the 27B loops instead of finishing.
    "flash-next" = {
      label = "Qwen3.8-Flash-Next 125B (RAM offload)";
      summary = "Bigger, with speculation: ~26-35 tok/s, 65k context, 73 GiB in RAM.";
      ggufFile = "/srv/inference/gguf/flash-next/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf";
      maxModelLen = 65536;
      kvType = "q8_0";
      specStages = [ "mtp:n_max=3" ];
      mtpRequantizeOutputTensor = null;
      # 44 rather than 46 because -wgt 1 paid for the two layers: 28.4 tok/s at
      # 32k and 26.0 at 60k, against 27.2/25.2 at cpuMoe 46 without it.
      cpuMoe = 44;
      draftModel = "/srv/inference/gguf/flash-next/mtp-Qwen3.8-Flash-Next-shared-Q4_K_M.gguf";
      batchSize = 4096;
      ubatchSize = 4096;
      extraArgs = [
        "-wgt"
        "1"
      ];
    };

    # The same model with the draft head removed, trading 20% of decode for 2x
    # the context. See the ⛔ above for why these cannot be one profile: MTP at
    # 131072 is not slower, it is a server that dies when the context fills.
    #
    # Measured 2026-09-17 on ik dc310244 with -wgt 1, validated at 120k depth:
    #   depth      32k    120k
    #   PP        1060     840
    #   TG        22.0    21.2
    #   TTFT      30.2s   141s
    #
    # ⛔ NO DRAFT HEAD HERE, AND THAT IS A MEASURED CHOICE RATHER THAN A
    # LIMITATION. MTP does now survive 128k once -wgt 1 frees the compute
    # buffer — cpuMoe 48 ran to 120k depth at 21.8 tok/s — but it is a bad trade
    # at this depth, because acceptance falls as the history grows:
    #
    #                        PP@120k   TG@120k   cold TTFT
    #   no draft head (this)     840      21.2        141s
    #   MTP, cpuMoe 48           722      21.8        164s
    #
    # 6% more decode for 16% less prefill and 23s more on every cold turn. The
    # speculation profile above earns its keep at 65k, where acceptance is high;
    # here it does not. Re-test that if a future head accepts better at depth.
    #
    # ⚠️ THE 141s COLD TTFT IS THE REAL COST, NOT THE 21 tok/s, and it is
    # survivable only because the prompt cache works (66x on turn 2). An agent
    # client that busts the cache turns every turn into the cold number.
    "flash-next-128k" = {
      label = "Qwen3.8-Flash-Next 125B (128k, no speculation)";
      summary = "Same model, 2x context, ~21 tok/s. For jobs that need the window.";
      ggufFile = "/srv/inference/gguf/flash-next/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf";
      maxModelLen = 131072;
      kvType = "q8_0";
      specStages = [ ];
      mtpRequantizeOutputTensor = null;
      # ⛔ 40 AND 38 DO NOT LOAD AT THIS CONTEXT EVEN WITH -wgt 1 — both died at
      # "unable to load model". 44 is the wall here, not a preference.
      cpuMoe = 44;
      draftModel = null;
      batchSize = 4096;
      ubatchSize = 4096;
      extraArgs = [
        "-wgt"
        "1"
      ];
    };

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
    # The files live under /srv/inference/gguf/vision and are not in git;
    # fetched with `hf download` from ggml-org/Qwen3.8-27B-GGUF (mmproj) and
    # unsloth/Qwen3.8-27B-GGUF (UD-Q3_K_XL).

    # The default model plus its vision encoder. Same weights, same MTP, same
    # ~106 tok/s decode on image prompts; the encoder costs context.
    #
    # ⛔ 147456, NOT 180224: THE ENCODER DOES NOT FIT BESIDE THE FULL WINDOW.
    # With the projector loaded, -c 180224 starts (23.0 GB idle) and dies at
    # depth with `CUDA error: out of memory` — the same starts-fine-fails-full
    # trap recorded on `maxModelLen` in inference.nix. Driven to occupancy:
    #
    #   projector   -c       depth     peak VRAM   then 16 frames
    #   BF16       180224    ~176k        —         CUDA OOM
    #   BF16       147456    144,015   23,734 MiB   ok, 23,820 MiB
    #   Q8_0       147456    144,015   23,534 MiB   ok, 23,534 MiB
    #
    # ⚠️ Q8_0 RATHER THAN BF16 FOR ITS MARGIN, NOT ITS SIZE: ~290 MiB lower at
    # the peak, and its answers were the same or better on every clip (it named
    # the grandmother; BF16 counted four people). ~550 MiB of slack is thinner
    # than the text profile keeps, so do not raise this without re-driving it.
    "qwen3.8-27b-vision" = {
      label = "Qwen3.8-27B + vision";
      summary = "The 27B that can see images. 147k context, ~106 tok/s.";
      mmproj = "/srv/inference/gguf/vision/mmproj-Qwen3.8-27B-Q8_0.gguf";
      maxModelLen = 147456;
    };

    # A smaller cut of the same model, for the native 262k window WITH vision.
    # unsloth's UD-Q3_K_XL, 13.1 GB against IQ4_KS's 16.9, and it still carries
    # the MTP head (the server reports "MTP context ready").
    #
    # Driven 2026-10-07 to 255,615 tokens of 262,144 (97.5%): needles 3/3,
    # peak 23,546 MiB, then served 8 frames after it. Decode ~102 tok/s on image
    # prompts, 46.5 at full depth; cold TTFT at 255k is 300 s.
    #
    # ⚠️ "EQUAL ON MY TESTS" IS A CEILING EFFECT, NOT EQUIVALENCE. Curation
    # answers matched IQ4_KS clip for clip, but that battery is easy: every
    # candidate passed it. 3-bit is where quantization starts to cost on hard
    # reasoning and code, and nothing here measured that. Prefer the profile
    # above unless the window is the point.
    "qwen3.8-27b-q3-262k" = {
      label = "Qwen3.8-27B Q3 + vision, 262k";
      summary = "Smaller 3-bit cut: full 262k window with vision. Use when context is the point.";
      ggufFile = "/srv/inference/gguf/vision/Qwen3.8-27B-UD-Q3_K_XL.gguf";
      mmproj = "/srv/inference/gguf/vision/mmproj-Qwen3.8-27B-Q8_0.gguf";
      maxModelLen = 262144;
    };

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
      ];
    };

    # vLLM on the STOCK image: the fallback if the patched image below is ever
    # unavailable or suspect. Same job as the batch profile, slower, and the
    # best-documented quant of the lot.
    #
    # Same 16-frame curation job, unique prompts (no prefix-cache help):
    #
    #   engine                 in flight   clips/min
    #   ik (Q3, --parallel 1)      1          10.2
    #   ik                         6          13.1   (queued)
    #   vLLM, max-num-seqs 6       6          26.2
    #   vLLM, max-num-seqs 6      12          26.9
    #
    # RedHatAI/Qwen3.8-27B-INT4: AWQ smoothing then GPTQ, W4A16, group 128,
    # vision tower / embeddings / lm_head / DeltaNet a,b gates left BF16. Chosen
    # because it publishes recovery against BF16 on vLLM (IFEval 99.7%, MMLU-Pro
    # 98.8%, GPQA 98.5%, AIME25 98.7%) and is the leanest cut that does: 17.71
    # GiB resident against 20 GB for the Qwen3.6 g32 cut this host ran before.
    # `hf download RedHatAI/Qwen3.8-27B-INT4` into the HF cache under stateDir;
    # the launcher runs vLLM offline.
    #
    # ⛔ THE CONTEXT WAS BOUGHT WITH THESE FLAGS, NOT THE CHECKPOINT ALONE.
    # Single-sequence probes, fp8 KV, same checkpoint:
    #
    #   util  batched  image cap         KV tokens   note
    #   0.95    8192   none (16k tok)      ~69k      encoder profiled at max
    #   0.97    4096   max_pixels 1 MP    ~125k
    #   0.98    2048   max_pixels 1 MP    ~143k      started; not driven
    #   0.97    2048   max_pixels 1 MP    135,441    OOM ON THE FIRST 8 FRAMES
    #   0.95    4096   max_pixels 1 MP    112,252    105,135-token fill ok, but
    #                                                allocator OOM warnings
    #
    # The 0.97 row is the lesson: vLLM profiles the vision encoder once and the
    # first multi-image request blew straight past it, crashing the engine.
    # max_pixels is what made room — it caps the profiled encoder peak — and
    # home-video frames do not need more than ~1 MP.
    #
    # ⛔ 0.92 BECAUSE 0.94-0.95 LOGGED ALLOCATOR OOMs UNDER CONCURRENT IMAGES.
    # They recovered and every request returned 200, but twice in two configs
    # is a pattern. At 0.92 a 6- and 12-clip batch, a native video and a
    # 31,695-token fill ran with zero.
    #
    # ⛔ NO MTP WITH THIS CHECKPOINT. Its MTP drafter allocates its OWN BF16
    # lm_head (2.37 GiB on this 248k vocabulary) and OOMed at load; the batch
    # profile's checkpoint quantizes that head, which is the whole difference.
    "qwen3.8-27b-vllm-stock" = {
      engine = "vllm";
      label = "Qwen3.8-27B vLLM (stock image)";
      summary = "Fallback batch profile: RedHat INT4, no MTP, ~26 clips/min, 32k.";
      model = "RedHatAI/Qwen3.8-27B-INT4";
      maxModelLen = 32768;
      gpuMemoryUtilization = 0.92;
      # CUDA graphs fit with this checkpoint (0.09 GiB captured at these sizes).
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
    "glm-4.6v-flash" = {
      engine = "vllm";
      label = "GLM-4.6V-Flash 9B (first pass)";
      summary = "Non-Qwen 9B vision model, FP8, ~8 clips at once. Cheap first pass.";
      model = "zai-org/GLM-4.6V-Flash";
      quantization = "fp8";
      maxModelLen = 32768;
      gpuMemoryUtilization = 0.92;
      enforceEager = false;
      reasoningParser = "glm45";
      toolCallParser = "glm45";
      extraArgs = [
        "--max-num-seqs"
        "8"
        "--max-num-batched-tokens"
        "8192"
        "--limit-mm-per-prompt"
        ''{"image":16,"video":0}''
      ];
    };
  };
}
