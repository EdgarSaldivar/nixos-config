{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# The Dungeon Scriber worker shares nardol's only GPU with games, and the
# failure worth a contract is not a broken build — it is a working worker that
# ends a game session, or a private name or credential in this PUBLIC
# repository. Each assertion below is one of those:
#
#   1. merging changes nothing: the worker is off unless the owner enables it;
#   2. the token, API origin and registry credential are host file paths read at
#      runtime, never literals in the unit or the store;
#   3. gaming reaches the worker one way only (Wants -> yield -> Conflicts), and
#      nothing on the worker side can stop the gaming target;
#   4. the shared gate yields on every signal, and on every uncertain answer,
#      proven by running the real script against fakes;
#   5. no tailnet hostname appears anywhere in the tree.
let
  base = nixosConfigurations.nardol;
  zeros = lib.concatStrings (lib.genList (_: "0") 64);
  enabledWith =
    extra:
    (base.extendModules {
      modules = [
        {
          nardol.dungeonScriberWorker = {
            enable = true;
            image = "ghcr.io/example/dungeon-scriber-worker@sha256:${zeros}";
            registryLogin = {
              username = "example";
              passwordFile = "/var/lib/example/registry-token";
            };
          }
          // extra;
        }
      ];
    }).config;
  failedAssertions = c: map (a: a.message) (lib.filter (a: !a.assertion) c.assertions);
  workerFailures = c: lib.filter (m: lib.hasInfix "dungeonScriberWorker" m) (failedAssertions c);

  off = base.config;
  on = enabledWith { };
  wcfg = on.nardol.dungeonScriberWorker;
  svc = on.systemd.services;
  worker = svc.docker-dungeon-scriber-worker;
  container = on.virtualisation.oci-containers.containers.dungeon-scriber-worker;
  yield = svc.dungeon-scriber-worker-yield;
  guard = svc.dungeon-scriber-worker-guard;
  resume = svc.dungeon-scriber-worker-resume;
  target = on.systemd.targets.nardol-gaming;
  workerUnit = "docker-dungeon-scriber-worker.service";
  gamingUnit = "nardol-gaming.target";
  unitText = worker.script + toString (container.environmentFiles ++ container.volumes);

  # ── 1 ──
  offProblems =
    lib.optional off.nardol.dungeonScriberWorker.enable "the worker is enabled by default"
    ++ lib.optional (
      off.virtualisation.oci-containers.containers ? dungeon-scriber-worker
    ) "the worker container exists while disabled"
    ++ lib.optional (
      builtins.match ".+@sha256:[0-9a-f]{64}" (off.nardol.dungeonScriberWorker.image or "") == null
    ) "the default worker image is not digest-pinned"
    ++ lib.optional (lib.any (n: lib.hasPrefix "dungeon-scriber-worker" n) (
      lib.attrNames off.systemd.services ++ lib.attrNames off.systemd.timers
    )) "worker units exist while disabled"
    ++ lib.optional (lib.elem "dungeon-scriber-worker-yield.service" (
      off.systemd.targets.nardol-gaming.wants or [ ]
    )) "the gaming target references the worker while disabled";

  # ── 2 ──
  fileProblems =
    lib.optional (
      workerFailures on != [ ]
    ) "the reference enabled config fails: ${toString (workerFailures on)}"
    ++ lib.optional (
      !lib.elem "${wcfg.tokenFile}:/run/secrets/worker-token:ro" container.volumes
    ) "the worker token is not a read-only mount of tokenFile"
    ++ lib.optional (
      container.environmentFiles != [ wcfg.apiEnvironmentFile ]
    ) "the API origin is not read from apiEnvironmentFile"
    ++ lib.optional (
      container.login.passwordFile != "/var/lib/example/registry-token"
    ) "the registry credential is not read from registryLogin.passwordFile"
    ++ lib.optional (lib.any (k: container.environment ? ${k}) [
      "DS_API_BASE_URL"
      "DS_WORKER_TOKEN"
      "HF_TOKEN"
      "HUGGING_FACE_HUB_TOKEN"
    ]) "a credential or the API origin is a literal container variable"
    ++ lib.optional (lib.hasInfix "DS_API_BASE_URL=" unitText) "the API origin is rendered into the unit"
    # The negative half: the guards on those options must actually fire.
    ++ lib.optional (
      workerFailures (enabledWith {
        tokenFile = "${builtins.storeDir}/x-token";
      }) == [ ]
    ) "a store-path tokenFile is accepted"
    ++ lib.optional (
      workerFailures (enabledWith {
        apiEnvironmentFile = "api.env";
      }) == [ ]
    ) "a relative apiEnvironmentFile is accepted"
    ++ lib.optional (
      workerFailures (enabledWith {
        environment.DS_API_BASE_URL = "https://example.invalid";
      }) == [ ]
    ) "a literal DS_API_BASE_URL is accepted"
    ++ lib.optional (
      workerFailures (enabledWith {
        image = "ghcr.io/example/dungeon-scriber-worker:latest";
      }) == [ ]
    ) "an undigested registry image is accepted";

  # ── 3 ──
  # Conflicts= is symmetric, and Requires/BindsTo/PartOf propagate stops too.
  # Any of them between the worker side and the target would let a timer tick,
  # a crash-restart or an activation end a live game.
  linksTo =
    unit: names:
    lib.any (f: lib.any (n: lib.elem n (unit.${f} or [ ])) names) [
      "conflicts"
      "requires"
      "requisite"
      "bindsTo"
      "partOf"
      "upholds"
    ];
  arbitrationProblems =
    lib.optional (
      !lib.elem gamingUnit yield.wantedBy
    ) "starting a game does not pull in the yield helper"
    ++ lib.optional (
      yield.conflicts != [ workerUnit ]
    ) "the yield helper does not Conflicts= exactly the worker"
    ++ lib.optional (
      !lib.elem workerUnit yield.after
    ) "the yield helper does not wait for the worker to stop"
    ++ lib.optional (
      !lib.elem "nardol-gpu-handover.service" yield.before || !lib.elem gamingUnit yield.before
    ) "the yield helper is not ordered before the GPU handover and the target"
    ++ lib.optional (yield.serviceConfig.RemainAfterExit or true
    ) "the yield helper stays active, so a worker start would stop it"
    ++ lib.optional (linksTo yield [ gamingUnit ]) "the yield helper can stop the gaming target"
    ++ lib.optional (lib.any (u: linksTo u [ gamingUnit ]) [
      worker
      guard
      resume
    ]) "a worker unit has a stop-propagating link to the gaming target"
    ++ lib.optional (linksTo target [
      workerUnit
      "dungeon-scriber-worker-guard.service"
    ]) "the gaming target links directly to the worker, which is symmetric"
    ++ lib.optional (
      !lib.hasInfix "dungeon-scriber-gpu-gate" (worker.serviceConfig.ExecCondition or "")
    ) "the worker start is not gated"
    ++ lib.optional (container.autoStart) "the worker autostarts outside the gate's resume path"
    ++ lib.optional (
      !lib.elem "dungeon-scriber-worker-guard.service" worker.bindsTo
      || !lib.elem workerUnit guard.bindsTo
    ) "the worker and its guard are not bound to each other"
    ++ lib.optional (
      !lib.hasInfix "--job-mode=fail" resume.script
    ) "the resume can replace a job gaming queued"
    ++ lib.optional (
      !lib.elem "--init" container.extraOptions
    ) "the worker runs without --init, so a yield waits out docker's SIGKILL timeout"
    ++ lib.optional (wcfg.thresholdMiB != 6144) "the non-owned GPU threshold is no longer 6 GiB"
    ++ lib.optional (
      !lib.elem "docker-ikllama.service" wcfg.yieldUnits
    ) "the worker no longer yields to inference";

  # ── 4 ── the real gate, run against fakes
  fakeSystemctl = pkgs.writeShellScript "systemctl" ''
    # FAKE_STATES="unit=state ..."; anything unnamed is inactive.
    unit="''${!#}"
    for kv in ''${FAKE_STATES:-}; do
      if [ "''${kv%%=*}" = "$unit" ]; then echo "''${kv#*=}"; exit 0; fi
    done
    echo inactive
  '';
  fakeDocker = pkgs.writeShellScript "docker" ''
    [ -n "''${FAKE_OWN_PIDS:-}" ] || exit 1
    echo PID
    for p in $FAKE_OWN_PIDS; do echo "$p"; done
  '';
  fakeNvidiaSmi = pkgs.writeShellScript "nvidia-smi" ''
    [ -z "''${FAKE_SMI_FAIL:-}" ] || exit 9
    case "$*" in
      *query-gpu*) printf '%s\n' "$FAKE_TOTAL" ;;
      *query-compute-apps*) printf '%b' "''${FAKE_APPS:-}" ;;
      *) exit 2 ;;
    esac
  '';
  fakeCurl = pkgs.writeShellScript "curl" ''
    [ -z "''${FAKE_CURL_FAIL:-}" ] || exit 7
    sessions=''${FAKE_SESSIONS:-}
    lobbies=''${FAKE_LOBBIES:-}
    [ -n "$sessions" ] || sessions='{"sessions":[]}'
    [ -n "$lobbies" ] || lobbies='{"lobbies":[]}'
    case "''${!#}" in
      */sessions) printf '%s' "$sessions" ;;
      */lobbies) printf '%s' "$lobbies" ;;
      *) exit 22 ;;
    esac
  '';
  gateUnderTest = import ../hosts/nixos/nardol/dungeon-scriber-worker-gate.nix {
    inherit lib;
    inherit (pkgs)
      writeShellApplication
      coreutils
      gawk
      jq
      ;
    curl = fakeCurl;
    systemctl = fakeSystemctl;
    docker = fakeDocker;
    nvidiaSmi = fakeNvidiaSmi;
    container = "dungeon-scriber-worker";
    gamingUnit = gamingUnit;
    yieldUnits = [ "docker-ikllama.service" ];
    wolfUnit = "docker-wolf.service";
    wolfSocket = "wolf.sock";
    thresholdMiB = 6144;
  };

  # ── 5 ──
  # Split rather than regex: every MagicDNS suffix must follow the `<tailnet>`
  # placeholder or a `*` wildcard. The suffix is assembled so this file does
  # not trip itself.
  tsNet = ".ts" + ".net";
  textSuffixes = [
    ".nix"
    ".md"
    ".yaml"
    ".yml"
    ".toml"
    ".json"
    ".py"
    ".sh"
    ".go"
    ".conf"
    ".txt"
    ".env"
  ];
  collect =
    dir:
    lib.flatten (
      lib.mapAttrsToList (
        name: type:
        let
          path = dir + "/${name}";
        in
        if type == "directory" && name != ".git" && name != "result" then
          collect path
        else if type == "regular" && lib.any (s: lib.hasSuffix s name) textSuffixes then
          [ path ]
        else
          [ ]
      ) (builtins.readDir dir)
    );
  files = collect ../.;
  namesTailnet =
    file:
    let
      parts = lib.splitString tsNet (builtins.readFile file);
    in
    lib.any (p: !(lib.hasSuffix "<tailnet>" p || lib.hasSuffix "*" p)) (lib.init parts);
  tailnetLeaks = map toString (lib.filter namesTailnet files);

  problems = offProblems ++ fileProblems ++ arbitrationProblems;
in
if lib.length files < 50 then
  throw "nardol-dungeon-scriber-worker-contract scanned only ${toString (lib.length files)} files; discovery is broken and the tailnet scan would pass vacuously"
else if tailnetLeaks != [ ] then
  throw "A tailnet hostname is committed; replace it with <tailnet>: ${toString tailnetLeaks}"
else if problems != [ ] then
  throw "nardol Dungeon Scriber worker contract: ${lib.concatStringsSep "; " problems}"
else
  pkgs.runCommand "nardol-dungeon-scriber-worker-contract-ok"
    {
      nativeBuildInputs = [ pkgs.python3 ];
    }
    ''
      set -u
      gate=${gateUnderTest}/bin/dungeon-scriber-gpu-gate
      fails=0
      # expect <0|1> <label> [VAR=value ...]
      expect() {
        want=$1 label=$2
        shift 2
        if env -i PATH="$PATH" "$@" "$gate" >out 2>&1; then got=0; else got=1; fi
        if [ "$got" != "$want" ]; then
          echo "FAIL: $label: exit $got, wanted $want: $(cat out)"
          fails=$((fails + 1))
        else
          echo "ok: $label: $(cat out)"
        fi
      }

      # The gate probes Wolf only when its socket exists, as on the host.
      python3 -c 'import socket; socket.socket(socket.AF_UNIX).bind("wolf.sock")'
      clear="FAKE_TOTAL=700 FAKE_APPS=5993,\x20459\n"

      expect 0 "all clear" $clear
      expect 1 "gaming target active" $clear FAKE_STATES=nardol-gaming.target=active
      expect 1 "gaming target activating (handover window)" $clear FAKE_STATES=nardol-gaming.target=activating
      expect 1 "gaming target deactivating" $clear FAKE_STATES=nardol-gaming.target=deactivating
      expect 1 "inference starting" $clear FAKE_STATES=docker-ikllama.service=activating
      expect 1 "inference active" $clear FAKE_STATES=docker-ikllama.service=active
      expect 0 "inference failed holds nothing" $clear FAKE_STATES=docker-ikllama.service=failed
      expect 1 "Wolf session live" $clear 'FAKE_SESSIONS={"sessions":[{"id":1}]}'
      expect 1 "Wolf lobby live" $clear 'FAKE_LOBBIES={"lobbies":[{"id":1}]}'
      expect 1 "Wolf sessions field missing is unknown, not zero" $clear 'FAKE_SESSIONS={}'
      expect 1 "Wolf answers garbage" $clear 'FAKE_SESSIONS=<html>'
      expect 1 "Wolf unreachable" $clear FAKE_CURL_FAIL=1
      expect 1 "nvidia-smi fails" $clear FAKE_SMI_FAIL=1
      expect 1 "no memory figure" FAKE_TOTAL=N/A
      expect 0 "exactly 6 GiB held elsewhere" FAKE_TOTAL=6144
      expect 1 "6 GiB + 1 MiB held elsewhere" FAKE_TOTAL=6145
      expect 0 "the worker's own 8 GiB does not count" FAKE_TOTAL=9000 FAKE_OWN_PIDS="100 101" 'FAKE_APPS=100, 8000\n5993, 459\n'
      expect 1 "a game's 7 GiB beside the worker does count" FAKE_TOTAL=9000 FAKE_OWN_PIDS=100 'FAKE_APPS=100, 2000\n7000, 7000\n'
      expect 1 "a foreign pid is not ours by prefix" FAKE_TOTAL=9000 FAKE_OWN_PIDS=10 'FAKE_APPS=100, 8000\n'

      rm wolf.sock
      expect 1 "Wolf up without its socket" $clear FAKE_STATES=docker-wolf.service=active
      expect 0 "Wolf stopped on purpose" $clear

      [ "$fails" = 0 ] || exit 1
      touch $out
    ''
