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
    "qwen3.8-27b" = {
      label = "Qwen3.8-27B";
      summary = "27B dense, entirely on the 4090. The fast one.";
      ggufFile = null; # inherits inference.nix's default, which is this model
      maxModelLen = null;
      kvType = null;
      specStages = null;
      mtpRequantizeOutputTensor = null;
      cpuMoe = null;
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
    # ⚠️ NO SPECULATIVE DECODING, hence specStages = [ ]. unsloth's cut carries
    # no MTP tensors — verified by reading the GGUF tensor names rather than
    # trusting the label — so an mtp stage cannot load and the inherited
    # "mtp:n_max=8" would fail. jamesrogers publishes an MXFP4 build with the
    # head merged, but it is 118 GiB against 125 GiB of RAM.
    #
    # ⚠️ IT PINS 73 GiB OF HOST MEMORY and takes 22s doing it, which is most of
    # the 50s startup. That memory cannot be swapped. Nothing else on this host
    # wants it today — gaming is already exclusive with inference — but a second
    # RAM-hungry service and this profile cannot coexist.
    "flash-next" = {
      label = "Qwen3.8-Flash-Next 125B (RAM offload)";
      summary = "Bigger and slower: ~21 tok/s, 128k context, 73 GiB in RAM.";
      ggufFile = "/srv/inference/gguf/flash-next/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf";
      maxModelLen = 131072;
      kvType = "q8_0";
      specStages = [ ];
      mtpRequantizeOutputTensor = null;
      cpuMoe = 44;
      batchSize = 4096;
      ubatchSize = 4096;
      extraArgs = [ ];
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
      maxModelLen = null;
      kvType = null;
      specStages = null;
      mtpRequantizeOutputTensor = "iq4_ks";
      cpuMoe = null;
      batchSize = null;
      ubatchSize = null;
      extraArgs = [ ];
    };
  };
}
