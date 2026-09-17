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
