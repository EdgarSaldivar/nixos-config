# Dungeon Scriber's GPU processing worker, always on, always second.
#
# The worker leases transcription jobs from the Dungeon Scriber API over
# authenticated HTTPS and holds the 4090 while it runs one. It is the lowest
# priority GPU tenant on this host: a game always wins, inference outranks it,
# and anything else holding more than the threshold makes it yield. Queued jobs
# simply wait. Procedure: docs/runbooks/nardol/dungeon-scriber-worker.md.
#
# ⛔ NOTHING HERE MAY Conflicts= THE GAMING TARGET, AND THAT IS THE WHOLE DESIGN.
#
# Conflicts= is symmetric: starting EITHER side stops the other. That is fine
# for inference, which only an operator starts. The worker is started by a
# timer, restarted on failure and restarted by activation, and every one of
# those would tear down a live Moonlight session the way AGENTS.md records a
# `nixos-rebuild switch` doing through docker-ikllama. So the relationship is
# built one-directionally instead:
#
#   gaming target  --Wants-->  dungeon-scriber-worker-yield  --Conflicts-->  worker
#     starting a game pulls in the yield helper, whose Conflicts= stops the
#     worker; the helper is ordered after the worker (so it waits for the stop)
#     and before nardol-gpu-handover (so the handover sees the memory freed).
#     The helper is a oneshot that is inactive again a moment later, so a later
#     worker start "stops" an already-stopped helper and touches nothing else.
#
#   worker  --ExecCondition-->  the gate
#     a start while a game, a Wolf session or inference is live is SKIPPED, not
#     failed and not allowed to stop anything.
#
#   worker  <--BindsTo-->  dungeon-scriber-worker-guard
#     the ported hand-run safety monitor: every few seconds it re-asks the gate
#     and stops the worker the moment the answer changes. If the guard dies, the
#     worker stops with it — an unguarded worker is not an option.
#
#   dungeon-scriber-worker-resume.timer
#     brings the worker back once the gate is clear again, with
#     --job-mode=fail so it can never displace a job gaming queued.
#
# An interrupted job is not lost: on SIGTERM the worker hands its lease back
# (POST .../release, reason "preempted") and exits 0 within a 7 s deadline. A
# released job is requeued at once without spending an attempt, up to 50 free
# releases per job (ADR 0011). Only a SIGKILL or an unreachable API falls back to
# the old path: the lease expires (DS_LEASE_SECONDS, default 300 s) and the
# retry consumes an attempt.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.nardol.dungeonScriberWorker;
  inference = config.nardol.inference;

  containerName = "dungeon-scriber-worker";
  workerUnit = "docker-${containerName}.service";
  yieldUnit = "dungeon-scriber-worker-yield.service";
  guardUnit = "dungeon-scriber-worker-guard.service";
  gamingUnit = "nardol-gaming.target";

  inferenceUnit = "docker-${inference.containerName}.service";

  uid = config.users.users.${cfg.user}.uid;
  gid = config.users.groups.${config.users.users.${cfg.user}.group}.gid;

  systemctl = "${pkgs.systemd}/bin/systemctl";

  gate = import ./dungeon-scriber-worker-gate.nix {
    inherit lib;
    inherit (pkgs)
      writeShellApplication
      coreutils
      gawk
      jq
      ;
    curl = "${pkgs.curl}/bin/curl";
    inherit systemctl;
    docker = "${config.virtualisation.docker.package}/bin/docker";
    nvidiaSmi = "${config.hardware.nvidia.package.bin}/bin/nvidia-smi";
    container = containerName;
    inherit gamingUnit;
    inherit (cfg) yieldUnits thresholdMiB;
    wolfUnit = "docker-wolf.service";
    # Wolf's API socket, as ./idle-suspend.nix and wolf-config.template.toml use it.
    wolfSocket = "/run/wolf/wolf.sock";
  };
  gateBin = "${gate}/bin/dungeon-scriber-gpu-gate";

  # Host paths the owner provides. They are strings, never Nix paths: a path
  # literal would be copied into the world-readable store.
  fileOptions = {
    inherit (cfg)
      tokenFile
      apiEnvironmentFile
      modelCacheDir
      homeDir
      ;
  }
  // lib.optionalAttrs (cfg.hfTokenFile != null) { inherit (cfg) hfTokenFile; }
  // lib.optionalAttrs (cfg.registryLogin.passwordFile != null) {
    registryPasswordFile = cfg.registryLogin.passwordFile;
  };
  badFileOptions = lib.attrNames (
    lib.filterAttrs (_: p: !lib.hasPrefix "/" p || lib.hasPrefix builtins.storeDir p) fileOptions
  );

  # Variables that would carry a credential or the private API origin as a
  # literal in the unit file and the world-readable store.
  secretEnvironment = [
    "DS_API_BASE_URL"
    "DS_WORKER_TOKEN"
    "HF_TOKEN"
    "HUGGING_FACE_HUB_TOKEN"
  ];

  pathOption =
    default: description:
    lib.mkOption {
      type = lib.types.str;
      inherit default description;
    };
in
{
  options.nardol.dungeonScriberWorker = {
    enable = lib.mkEnableOption "the always-on Dungeon Scriber GPU worker, yielding to gaming and inference";

    image = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      # CI build of dungeon-scriber c813c43f97971ca61b11ba9e94a02efe195baeb8.
      # A private package: pulling it needs `registryLogin`.
      default = "ghcr.io/edgarsaldivar/dungeon-scriber-worker@sha256:3b703e1c67c6c9ba6f4a11bb4e75bafcf983b62560b0c2315d11c0041a32dfc2";
      description = ''
        The worker image. With `localImage = false` it must be digest-pinned
        (`name@sha256:...`), matching how ./inference.nix and Wolf pin theirs:
        a tag would let a republish change what runs here with no commit.
      '';
    };

    localImage = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        The image is a locally built tag (for example `dungeon-scriber-worker:dev`
        from `docker build -f workers/ml/Dockerfile`). Docker then never pulls, so
        a missing tag fails loudly instead of fetching a same-named image from
        Docker Hub. Pin a revision tag, not a floating one.
      '';
    };

    registryLogin = {
      registry = lib.mkOption {
        type = lib.types.str;
        default = "ghcr.io";
        description = "Registry to log in to before pulling a private image.";
      };
      username = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "Registry user. Not a secret; the token is `passwordFile`.";
      };
      passwordFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "/home/edgar/dungeon-scriber-worker/ghcr-token";
        description = ''
          Host file holding a registry token with only `read:packages`, created
          by the owner outside this repository. `docker login --password-stdin`
          reads it at start, and Docker then keeps the credential in root's
          Docker config on the host. Null when the image is local or public.
        '';
      };
    };

    stateDir = pathOption "/home/edgar/dungeon-scriber-worker" ''
      Owner-managed directory holding the worker's host-local files. Only the
      defaults below derive from it.
    '';

    tokenFile = pathOption "${cfg.stateDir}/worker-token-minas" ''
      Host file holding this worker's bearer token (mode 0600, owned by `user`).
      Mounted read-only at /run/secrets/worker-token, where the image's
      DS_WORKER_TOKEN_FILE points.
    '';

    apiEnvironmentFile = pathOption "${cfg.stateDir}/api.env" ''
      Host env file holding exactly `DS_API_BASE_URL=https://<api host>`.
      ⛔ The API origin is the private tailnet name and must never be committed,
      so it is passed to Docker as an --env-file and never enters the store.
    '';

    hfTokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "${cfg.stateDir}/hf-token";
      description = "Host file holding a Hugging Face read token for gated model downloads, or null.";
    };

    modelCacheDir = pathOption "${cfg.stateDir}/cache/models" ''
      Pinned model weights, mounted at /var/cache/dungeon-scriber/models so they
      download once and survive image upgrades.
    '';

    homeDir = pathOption "${cfg.stateDir}/home" ''
      Writable home for the container user: Triton and Torch compile caches.
    '';

    user = lib.mkOption {
      type = lib.types.str;
      default = "edgar";
      description = "Host user whose uid/gid the container runs as, so it can read the owner's files.";
    };

    gpus = lib.mkOption {
      type = lib.types.str;
      default = "all";
      description = "Value for `docker run --gpus`. Placement is configuration, never code.";
    };

    thresholdMiB = lib.mkOption {
      type = lib.types.ints.positive;
      default = 6144;
      description = ''
        GPU memory, in MiB summed over the host's GPUs, that other workloads may
        hold before the worker yields. 6 GiB is the rule the hand-run safety
        monitor enforced: idle Wolf holds well under 1 GiB, a game or a loaded
        model far more.
      '';
    };

    yieldUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = lib.optional inference.enable inferenceUnit;
      defaultText = lib.literalExpression "[ <the inference unit, every engine> ]";
      description = ''
        Units that outrank the worker. While any is active or starting the worker
        does not start, and the guard stops a running worker.
      '';
    };

    guardIntervalSec = lib.mkOption {
      type = lib.types.ints.positive;
      default = 2;
      description = "How often the guard re-asks the gate while the worker runs.";
    };

    resumeIntervalSec = lib.mkOption {
      type = lib.types.ints.positive;
      default = 60;
      description = "How often a stopped worker is offered the GPU again.";
    };

    scratchSize = lib.mkOption {
      type = lib.types.str;
      default = "4g";
      description = "tmpfs size for decoded audio (~600 MB per five-hour source).";
    };

    memoryLimit = lib.mkOption {
      type = lib.types.strMatching "[1-9][0-9]*[kmg]";
      default = "24g";
      description = ''
        Container memory cap, also used as the swap cap so the limit cannot be
        dodged by swapping. Five-hour sources peaked at ~17-18 GiB RSS with
        Nemotron and ~15 GiB with Community-1; a decode that runs away is killed
        inside the container instead of pressuring Wolf and the host.
      '';
    };

    pidsLimit = lib.mkOption {
      type = lib.types.ints.positive;
      default = 512;
      description = "Maximum processes and threads inside the container.";
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        DS_DIARIZATION_BACKEND = "community-1";
      };
      description = ''
        Extra worker settings (see the worker's runner.py). Credentials and the
        API origin are refused here; they come from the files above.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = inference.enable;
        message = "nardol.dungeonScriberWorker plugs into the gaming arbitration in ./gaming-arbitration.nix, which exists only with nardol.inference.enable";
      }
      {
        assertion = cfg.image != null;
        message = "nardol.dungeonScriberWorker.image must be set";
      }
      {
        assertion =
          cfg.image == null || cfg.localImage || builtins.match ".+@sha256:[0-9a-f]{64}" cfg.image != null;
        message = "nardol.dungeonScriberWorker.image must be pinned by digest unless localImage = true";
      }
      {
        assertion = !(cfg.localImage && cfg.registryLogin.passwordFile != null);
        message = "nardol.dungeonScriberWorker: a local image never pulls, so registryLogin.passwordFile would be dead configuration";
      }
      {
        assertion = (cfg.registryLogin.username == null) == (cfg.registryLogin.passwordFile == null);
        message = "nardol.dungeonScriberWorker.registryLogin needs both username and passwordFile, or neither";
      }
      {
        assertion = badFileOptions == [ ];
        message = "nardol.dungeonScriberWorker: ${toString badFileOptions} must be absolute host paths outside the Nix store";
      }
      {
        assertion = lib.intersectLists secretEnvironment (lib.attrNames cfg.environment) == [ ];
        message = "nardol.dungeonScriberWorker.environment must not carry ${toString secretEnvironment}; use the file options";
      }
    ];

    # ./idle-suspend.nix treats every container it does not recognise as a live
    # game, so re-enabling it would keep the host awake whenever the worker is up.
    warnings =
      lib.optional (config.systemd.timers ? nardol-idle-suspend)
        "nardol.dungeonScriberWorker: idle-suspend counts the worker container as a game and will never suspend while it runs";

    virtualisation.oci-containers.containers.${containerName} = {
      image = cfg.image;
      # The resume timer is the only thing that starts the worker, so
      # activation never starts it behind the gate's back.
      autoStart = false;
      pull = if cfg.localImage then "never" else "missing";
      login = lib.mkIf (cfg.registryLogin.passwordFile != null) {
        inherit (cfg.registryLogin) registry username passwordFile;
      };
      user = "${toString uid}:${toString gid}";
      # No `cmd`: the entrypoint without --once is the long-running lease loop.
      cmd = [ ];
      environment = {
        HF_HOME = "/var/cache/dungeon-scriber/models/hf";
        TRITON_CACHE_DIR = "/var/lib/worker/triton";
        TORCH_HOME = "/var/lib/worker/torch";
      }
      // lib.optionalAttrs (cfg.hfTokenFile != null) { HF_TOKEN_PATH = "/run/secrets/hf-token"; }
      // cfg.environment;
      environmentFiles = [ cfg.apiEnvironmentFile ];
      volumes = [
        "${cfg.modelCacheDir}:/var/cache/dungeon-scriber/models"
        "${cfg.homeDir}:/var/lib/worker"
        "${cfg.tokenFile}:/run/secrets/worker-token:ro"
      ]
      ++ lib.optional (cfg.hfTokenFile != null) "${cfg.hfTokenFile}:/run/secrets/hf-token:ro";
      extraOptions = [
        "--gpus=${cfg.gpus}"
        # Host networking so MagicDNS resolves the tailnet API name; Docker's
        # bridge replaces the host's stub resolver with public DNS.
        "--network=host"
        "--tmpfs=/tmp/worker:size=${cfg.scratchSize},mode=1777"
        # Equal memory and memory-swap: no swap beyond the cap.
        "--memory=${cfg.memoryLimit}"
        "--memory-swap=${cfg.memoryLimit}"
        "--pids-limit=${toString cfg.pidsLimit}"
        # ⛔ --init, or a yield takes ten seconds. Python as PID 1 has no SIGTERM
        # handler and the kernel drops unhandled signals to PID 1, so
        # `docker stop` would wait out its timeout and SIGKILL while a game's
        # GPU handover sat waiting for the memory.
        "--init"
        # Pin docker stop's SIGTERM-to-SIGKILL grace (its default is 10 s): the
        # worker's release on SIGTERM has a 7 s overall deadline inside it.
        "--stop-timeout=10"
      ];
    };

    systemd.services.${lib.removeSuffix ".service" workerUnit} = {
      bindsTo = [ guardUnit ];
      # [Unit] directives; ./inference.nix records serviceConfig silently
      # dropping them.
      startLimitBurst = 5;
      startLimitIntervalSec = 600;
      serviceConfig = {
        # Skipped, not failed: a start while the GPU is spoken for is normal.
        ExecCondition = gateBin;
        RestartSec = "30s";
        # The GPU handover waits for this stop; `docker stop` needs ~1 s with
        # --init, so anything longer is a hung container, not a slow one.
        TimeoutStopSec = lib.mkForce 30;
      };
      # Fail at start, readably, rather than letting Docker create a missing
      # mount source as an empty root-owned directory.
      preStart = ''
        for f in ${
          lib.escapeShellArgs (
            [
              cfg.tokenFile
              cfg.apiEnvironmentFile
            ]
            ++ lib.optional (cfg.hfTokenFile != null) cfg.hfTokenFile
          )
        }; do
          if [ ! -s "$f" ]; then
            echo "missing or empty $f; see docs/runbooks/nardol/dungeon-scriber-worker.md" >&2
            exit 1
          fi
        done
        if ! ${pkgs.gnugrep}/bin/grep -qE '^DS_API_BASE_URL=https://[^[:space:]]+$' ${lib.escapeShellArg cfg.apiEnvironmentFile}; then
          echo "${cfg.apiEnvironmentFile} must hold DS_API_BASE_URL=https://..." >&2
          exit 1
        fi
        for d in ${
          lib.escapeShellArgs [
            cfg.modelCacheDir
            cfg.homeDir
          ]
        }; do
          [ -d "$d" ] || ${pkgs.coreutils}/bin/install -d -m 0700 -o ${toString uid} -g ${toString gid} "$d"
        done
      '';
    };

    systemd.services.dungeon-scriber-worker-guard = {
      description = "Stop the Dungeon Scriber worker the moment the GPU is needed elsewhere";
      bindsTo = [ workerUnit ];
      after = [ workerUnit ];
      wantedBy = [ workerUnit ];
      serviceConfig.Type = "simple";
      script = ''
        while :; do
          if ! why=$(${gateBin}); then
            echo "$why; stopping the worker"
            # --no-block: this unit is bound to the worker and is stopped with
            # it, so waiting here would wait on our own teardown.
            exec ${systemctl} stop --no-block ${workerUnit}
          fi
          ${pkgs.coreutils}/bin/sleep ${toString cfg.guardIntervalSec}
        done
      '';
    };

    systemd.services.dungeon-scriber-worker-yield = {
      description = "Take the GPU back from the Dungeon Scriber worker for gaming";
      wantedBy = [ gamingUnit ];
      conflicts = [ workerUnit ];
      after = [ workerUnit ];
      before = [
        gamingUnit
        "nardol-gpu-handover.service"
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = false;
        ExecStart = "${pkgs.coreutils}/bin/true";
      };
    };

    systemd.services.dungeon-scriber-worker-resume = {
      description = "Offer the GPU back to the Dungeon Scriber worker";
      serviceConfig.Type = "oneshot";
      script = ''
        ${systemctl} is-active --quiet ${workerUnit} && exit 0
        ${gateBin} >/dev/null || exit 0
        # ⛔ --job-mode=fail: if gaming has queued a job in the meantime, this
        # start is refused instead of replacing the stop gaming asked for.
        exec ${systemctl} start --no-block --job-mode=fail ${workerUnit}
      '';
    };

    systemd.timers.dungeon-scriber-worker-resume = {
      description = "Offer the GPU back to the Dungeon Scriber worker";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "${toString cfg.resumeIntervalSec}s";
        AccuracySec = "10s";
      };
    };
  };
}
