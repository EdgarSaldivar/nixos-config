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
  container = cfg.virtualisation.oci-containers.containers.ikllama or null;

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
  );

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

else if draftWithoutStage != [ ] then
  throw "nardol inference: profile(s) ${toString draftWithoutStage} load a draft head with no mtp stage"

# ⛔ THE UNIT NAME IS LOAD-BEARING AND IS SPELLED IN THREE OTHER PLACES.
# gaming-arbitration.nix conflicts with docker-ikllama by name,
# nardol-inference-restore starts it by name, and nardol-inference-inhibit binds
# to it. A container renamed per model would leave all three pointing at a unit
# that no longer exists -- and systemd CREATES a unit when you set properties on
# a name, so the phantom would absorb the policy while the real server ran
# without it. That exact failure is already recorded in inference.nix.
else if ikUnit == null || container == null then
  throw "nardol inference: docker-ikllama unit or ikllama container is gone; arbitration and restore name it directly"

# The model is chosen inside the container, so the unit must carry no -m of its
# own and must run the generated entrypoint instead of the image's.
else if container.cmd != [ ] then
  throw "nardol inference: ikllama passes cmd arguments; they append AFTER the profile's and a duplicate -m or -c silently wins"

else if !lib.elem "--entrypoint" container.extraOptions then
  throw "nardol inference: ikllama no longer overrides the entrypoint, so the profile switch cannot take effect"

else if !lib.any (v: lib.hasInfix "/var/lib/nardol-inference" v) container.volumes then
  throw "nardol inference: the profile state directory is not mounted; every switch would silently serve the default"

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
