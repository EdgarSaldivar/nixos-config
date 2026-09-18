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
{
  # The profile served when nothing has been chosen, and the fallback whenever
  # the saved choice names a profile that no longer exists.
  default = "qwen3.8-27b";

  profiles = {
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
    # So: the 27B stays default on speed, and this profile earns its slot for
    # long multi-step work rather than for answering questions.
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

    # ⛔ KEPT AS A ROLLBACK, AND IT IS THE ONLY TESTED WAY BACK. Every
    # throughput number in inference.nix was measured against this file on
    # 2026-09-13. If 3.8 disappoints on real work, switching here restores a
    # known-good deployment without a rebuild — which is the entire reason a
    # runtime switch exists rather than just editing `ggufFile`.
    #
    # ⚠️ ITS MTP HEAD IS NOT REQUANTIZED, unlike 3.8's. -mtprot was worth 8% on
    # code with this file, so the profile asks for it explicitly rather than
    # inheriting the module default, which is now null for 3.8's sake.
    "qwen3.6-27b" = {
      label = "Qwen3.6-27B (rollback)";
      summary = "The previous model. Every benchmark in inference.nix is from this file.";
      ggufFile = "/srv/inference/gguf/Qwen3.6-27B-MTP-IQ4_KS.gguf";
      # ⛔ ITS OWN CEILING, NOT THE MODULE'S. 202752 was measured against THIS
      # checkpoint on 2026-09-13 and held at 98.6% occupancy; the module default
      # dropped to 180224 for 3.8, which does not fit 202752. Inheriting would
      # have silently shrunk the rollback's context by 22k tokens for no reason.
      maxModelLen = 202752;
      kvType = null;
      specStages = null;
      mtpRequantizeOutputTensor = "iq4_ks";
      cpuMoe = null;
      draftModel = null;
      batchSize = null;
      ubatchSize = null;
      extraArgs = [ ];
    };
  };
}
