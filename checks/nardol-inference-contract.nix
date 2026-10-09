{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# The inference endpoint gained runtime model switching on 2026-09-17, and with
# it three couplings that are invisible at the point of edit and expensive at
# the point of failure. Each assertion below exists because breaking it produces
# a WORKING deployment that is wrong, not a build error.
let
  cfg = nixosConfigurations.nardol.config;
  inference = cfg.nardol.inference;
  profileData = import ../lib/inference-profiles.nix;
  amonDinPackages = import ../pkgs/amon-din.nix { inherit pkgs; };
  profileNames = lib.attrNames profileData.profiles;

  # The unit the arbitration, restore and inhibit paths all name by hand.
  ikUnit = cfg.systemd.services."docker-ikllama" or null;
  launcherText = if ikUnit == null then "" else ikUnit.script;

  # The Mac menu is built from the same file. If someone replaces that import
  # with a hand-written list, the menu starts offering models nardol cannot
  # serve -- the exact drift lib/inference-profiles.nix exists to prevent.
  amonDinText = builtins.readFile ../pkgs/amon-din.nix;
  localProbeText = builtins.readFile ../pkgs/nardol-local-seat-probe.nix;
  fakeCurl = pkgs.writeShellApplication {
    name = "curl";
    text = ''
      status="''${FAKE_CURL_STATUS:-0}"
      if [ "$status" = 0 ]; then
        response="''${FAKE_CURL_RESPONSE:-}"
        if [ -z "$response" ]; then
          response='{"state":"ready"}'
        fi
        printf '%s\n' "$response"
      else
        exit "$status"
      fi
    '';
  };
  localProbeUnderTest = pkgs.callPackage ../pkgs/nardol-local-seat-probe.nix {
    curl = fakeCurl;
  };
  leaseSource = builtins.readFile ../pkgs/nardol-lease/main.go;
  leaseExec = cfg.systemd.services.nardol-lease.serviceConfig.ExecStart;

  # Every servable GGUF must sit under the directory the container mounts, or
  # the server fails at load with a path only its own log mentions.
  ggufRoot = "${inference.stateDir}/gguf";
  badPaths = lib.filter (p: p != null && !lib.hasPrefix "${ggufRoot}/" p) (
    lib.mapAttrsToList (_: p: p.ggufFile) profileData.profiles
    ++ lib.mapAttrsToList (_: p: p.draftModel) profileData.profiles
    ++ lib.mapAttrsToList (_: p: p.mmproj) profileData.profiles
  );

  # A field that belongs to another engine is ignored by this one, so setting
  # it reads as a choice and does nothing. Each engine's foreign fields:
  ggufOnly = [
    "ggufFile"
    "mmproj"
    "specStages"
    "mtpRequantizeOutputTensor"
    "cpuMoe"
    "draftModel"
    "batchSize"
    "ubatchSize"
  ];
  vllmOnly = [
    "model"
    "quantization"
    "gpuMemoryUtilization"
    "enforceEager"
    "image"
    "reasoningParser"
    "toolCallParser"
  ];

  # A profile's own image must be content-addressed like the module's: a
  # registry digest, or a local build pinned by its image ID. A tag on a local
  # build is reassigned by the next `docker build -t` without a commit here.
  unpinnedImages = lib.filter (
    name:
    let
      i = profileData.profiles.${name}.image;
    in
    i != null && !(lib.hasInfix "@sha256:" i || lib.hasPrefix "sha256:" i)
  ) profileNames;
  foreign = {
    ik-llama = vllmOnly;
    llama-cpp = vllmOnly ++ [
      "specStages"
      "mtpRequantizeOutputTensor"
      "cpuMoe"
      "draftModel"
      "batchSize"
      "ubatchSize"
    ];
    vllm = ggufOnly;
  };
  badEngine = lib.filter (name: !(foreign ? ${profileData.profiles.${name}.engine})) profileNames;
  misplaced = lib.concatMap (
    name:
    let
      p = profileData.profiles.${name};
    in
    map (f: "${name}.${f}") (lib.filter (f: p.${f} != null) (foreign.${p.engine} or [ ]))
  ) profileNames;

  # ⛔ ONLY ik INHERITS A CONTEXT CEILING. The module default was measured for
  # the ik 27B; vLLM's weights are heavier and the same number OOMs at init
  # there (measured 2026-09-13). Each non-ik profile states its own.
  noCeiling = lib.filter (
    name:
    let
      p = profileData.profiles.${name};
    in
    p.engine != "ik-llama" && p.maxModelLen == null
  ) profileNames;
  noModel = lib.filter (
    name:
    let
      p = profileData.profiles.${name};
    in
    (p.engine == "vllm" && p.model == null) || (p.engine == "llama-cpp" && p.ggufFile == null)
  ) profileNames;

  # A profile asking for an mtp stage without a draft head only works when the
  # MODEL file carries the tensors. That is a property of the file, so it cannot
  # be checked here -- but the inverse can: a draft head with no mtp stage is
  # 1.8 GiB of VRAM bought for nothing.
  draftWithoutStage = lib.filter (
    name:
    let
      p = profileData.profiles.${name};
      stages = if p.specStages == null then inference.specStages else p.specStages;
    in
    p.draftModel != null && !lib.any (s: lib.hasPrefix "mtp" s) stages
  ) profileNames;
in

if !lib.elem profileData.default profileNames then
  throw "nardol inference: default profile '${profileData.default}' is not in lib/inference-profiles.nix"

else if badPaths != [ ] then
  throw "nardol inference: GGUF outside ${ggufRoot}, the container cannot see it: ${toString badPaths}"

else if badEngine != [ ] then
  throw "nardol inference: profile(s) ${toString badEngine} name an engine the launcher does not know"

else if misplaced != [ ] then
  throw "nardol inference: field(s) ${toString misplaced} do not apply to that profile's engine and would be ignored"

else if noCeiling != [ ] then
  throw "nardol inference: non-ik profile(s) ${toString noCeiling} must set maxModelLen; the inherited one is ik's"

else if unpinnedImages != [ ] then
  throw "nardol inference: profile(s) ${toString unpinnedImages} name an image by tag; pin a digest or a local image ID"

else if noModel != [ ] then
  throw "nardol inference: profile(s) ${toString noModel} do not name a checkpoint their engine can read"

else if draftWithoutStage != [ ] then
  throw "nardol inference: profile(s) ${toString draftWithoutStage} load a draft head with no mtp stage"

# ⛔ THE UNIT NAME IS LOAD-BEARING AND IS SPELLED IN THREE OTHER PLACES.
# gaming-arbitration.nix conflicts with docker-ikllama by name,
# nardol-inference-restore starts it by name, and nardol-inference-inhibit binds
# to it. A container renamed per model would leave all three pointing at a unit
# that no longer exists -- and systemd CREATES a unit when you set properties on
# a name, so the phantom would absorb the policy while the real server ran
# without it. That exact failure is already recorded in inference.nix.
else if ikUnit == null || inference.containerName != "ikllama" then
  throw "nardol inference: docker-ikllama unit is gone; arbitration and restore name it directly"

# The engine and model are chosen by the launcher at exec time, from the state
# file. A unit that stops reading it serves the default after every switch while
# the menu reports the new choice.
else if !lib.hasInfix "read -r PROFILE < /var/lib/nardol-inference/profile" launcherText then
  throw "nardol inference: the launcher no longer reads the profile state file; every switch would silently serve the default"

else if lib.any (name: !lib.hasInfix "\n${name})\n" ("\n" + launcherText)) profileNames then
  throw "nardol inference: a profile has no branch in the launcher, so selecting it serves the default"

else if
  lib.any (n: !lib.hasInfix "--name=ikllama" n) (
    lib.filter (l: lib.hasInfix "exec docker" l) (lib.splitString "\n" launcherText)
  )
then
  throw "nardol inference: a launcher branch runs a container not named ikllama; stop and rm would miss it"

# ⛔ A TAG ON A PUBLISHED IMAGE LETS UPSTREAM CHANGE THE ENGINE WITH NO COMMIT.
else if
  !lib.hasInfix "@sha256:" inference.image || !lib.hasInfix "@sha256:" inference.llamaCppImage
then
  throw "nardol inference: the vLLM and llama.cpp images must be digest-pinned"

# ⚠️ A FLOATING TAG HERE MEANS THE NEXT REBUILD SILENTLY CHANGES THE ENGINE.
# /root/ik-rebuild.sh reassigns both `local` and `next`; only a revision tag
# pins what is actually deployed.
else if inference.ikLlamaImage == "ik-llama:local" || inference.ikLlamaImage == "ik-llama:next" then
  throw "nardol inference: ikLlamaImage pins a floating tag; use the revision tag the rebuild script printed"

else if !lib.hasInfix "import ../lib/inference-profiles.nix" amonDinText then
  throw "nardol inference: pkgs/amon-din.nix no longer reads the shared profile list; the Mac menu will drift"

else if
  !lib.hasInfix "nardol-local-seat-probe" amonDinText || !(amonDinPackages ? nardol-local-seat-probe)
then
  throw "nardol local seat: Amon Din must expose the standalone read-only availability probe"

else if
  !(amonDinPackages ? amon-din-serve)
  || !lib.hasInfix ''echo "Serve inference | bash='' amonDinText
  || !lib.hasInfix "nardol-model serve" amonDinText
  || !lib.hasInfix "systemctl stop nardol-gaming.target" (
    builtins.readFile ../hosts/nixos/nardol/inference.nix
  )
  || !lib.hasInfix "systemctl start docker-ikllama" (
    builtins.readFile ../hosts/nixos/nardol/inference.nix
  )
  || !lib.hasInfix ''wait_ready "$profile"'' (builtins.readFile ../hosts/nixos/nardol/inference.nix)
  || !lib.hasInfix "serve)" (builtins.readFile ../hosts/nixos/nardol/inference.nix)
then
  throw "nardol inference: Amon Din serve must wake the host and override gaming ownership"

# A unit that hit its restart limit refuses every start, including the switch
# back to a working profile, until reset-failed (hit 2026-10-08). The menu must
# offer the way out, and the lease must say when it is needed.
else if
  !(amonDinPackages ? amon-din-restore)
  || !lib.hasInfix "nardol-model restore" amonDinText
  || !lib.hasInfix "restore)" (builtins.readFile ../hosts/nixos/nardol/inference.nix)
  || !lib.hasInfix "systemctl reset-failed docker-ikllama.service" (
    builtins.readFile ../hosts/nixos/nardol/inference.nix
  )
  || !lib.hasInfix "start-limit-hit" leaseSource
  || !lib.hasInfix "--serving-state" leaseExec
then
  throw "nardol inference: the restart-limit recovery path (restore, lease reporting) is incomplete"

# Tried profiles (nardol-model try) live outside git, so the guards that keep
# them from becoming a second, unreviewed fleet config are checked here: a
# failing one falls back to the default, the HF token never reaches a command
# line, remote code is refused, and the lease only accepts validated names.
else if
  !lib.hasInfix "try)" (builtins.readFile ../hosts/nixos/nardol/inference.nix)
  || !lib.hasInfix "forget)" (builtins.readFile ../hosts/nixos/nardol/inference.nix)
  || !lib.hasInfix ''user_profile "$PROFILE" || true'' (
    builtins.readFile ../hosts/nixos/nardol/inference.nix
  )
  || !lib.hasInfix "--env-file" (builtins.readFile ../hosts/nixos/nardol/inference.nix)
  || !lib.hasInfix "-H @" (builtins.readFile ../hosts/nixos/nardol/inference.nix)
  || !lib.hasInfix "auto_map" (builtins.readFile ../hosts/nixos/nardol/inference.nix)
  || !lib.hasInfix ''SuccessExitStatus = "143 137"'' (
    builtins.readFile ../hosts/nixos/nardol/inference.nix
  )
  || !lib.hasInfix "--user-profiles-dir" leaseExec
  || !lib.hasInfix "triedName.MatchString" leaseSource
  || !(amonDinPackages ? amon-din-try)
then
  throw "nardol inference: tried profiles lost a guard (fallback, token handling, remote-code refusal, or name validation)"

else if
  !lib.hasInfix "http://nardol:8002/status" localProbeText
  || !lib.hasInfix ''"state":"asleep"'' localProbeText
  || !lib.hasInfix ''"state":"degraded"'' localProbeText
  || !lib.hasInfix "28)" localProbeText
  || !lib.hasInfix "6)" localProbeText
  || !lib.hasInfix "7)" localProbeText
  || !lib.hasInfix ''HandleFunc("/status"'' leaseSource
  || !lib.hasInfix ''"state": "gaming"'' leaseSource
  || !lib.hasInfix ''"state": "busy"'' leaseSource
  || !lib.hasInfix ''"state": "loading"'' leaseSource
  || !lib.hasInfix ''"state": "ready"'' leaseSource
then
  throw "nardol local seat: status must distinguish unreachable, HTTP failure, gaming, busy, loading, and ready without waking the host"

else if
  !lib.hasInfix "--health-url" leaseExec
  || !lib.hasInfix "--model-state" leaseExec
  || !lib.hasInfix inference.profileStateFile leaseExec
  || !lib.hasInfix "--known-profiles" leaseExec
  || !lib.hasInfix "--gaming-unit" leaseExec
  || !lib.hasInfix "--inference-unit" leaseExec
then
  throw "nardol local seat: nardol-lease status inputs are not bound to the selected inference service"

else
  pkgs.runCommand "nardol-inference-contract-ok"
    {
      nativeBuildInputs = [
        localProbeUnderTest
        pkgs.gnugrep
      ];
    }
    ''
      assert_state() {
        expected="$1"
        status="$2"
        actual=$(FAKE_CURL_STATUS="$status" nardol-local-seat-probe)
        printf '%s\n' "$actual" | grep -F '"state":"'"$expected"'"' >/dev/null
      }

      forwarded=$(FAKE_CURL_STATUS=0 FAKE_CURL_RESPONSE='{"state":"ready","detail":"fixture"}' nardol-local-seat-probe)
      test "$forwarded" = '{"state":"ready","detail":"fixture"}'
      assert_state asleep 28
      assert_state degraded 6
      assert_state degraded 7
      assert_state degraded 22
      assert_state degraded 55
      touch "$out"
    ''
