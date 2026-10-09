# Palantír — an OpenAI-compatible agent that answers questions about videos.
#
# GLM-4.6V-Flash (the glm-4.6v-flash inference profile) plans and calls tools
# that this service runs: face recognition (InsightFace), speech (Whisper),
# search embeddings (SigLIP 2), scene detection and frame/clip extraction.
# Any OpenAI client talks to it: base URL http://nardol:8003/v1, model
# "palantir"; videos attach as {"type":"video_url","video_url":{"url":...}}.
# Procedures: docs/runbooks/nardol/palantir.md.
#
# ⛔ IT RUNS ONLY BESIDE THE GLM PROFILE. Its models need the ~7 GB the GLM
# profile leaves free (gpuMemoryUtilization 0.65). Under the 27B default even an
# idle CUDA context would eat the ~340 MiB of headroom that profile was sized
# with, so the unit is PartOf the inference unit (stops and restarts with it,
# which also means gaming stops it) and its ExecCondition skips the start
# unless glm-4.6v-flash is the selected profile.
#
# ⛔ IDENTITY COMES ONLY FROM find_person. Every vision model tested here named
# the nearest lookalike when the right person was not among its references;
# face embeddings with a threshold returned NOT FOUND for the same decoy
# (closest similarity 0.30 against a 0.45 threshold, 2026-10-08).
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.nardol.palantir;
  inference = config.nardol.inference;
  inferenceUnit = "docker-${inference.containerName}.service";
  app = ./palantir/app;
in
{
  options.nardol.palantir = {
    enable = lib.mkEnableOption "the Palantír video agent beside the GLM inference profile";

    image = lib.mkOption {
      type = lib.types.str;
      # palantir-runtime:2, built 2026-10-08 from ./palantir/Dockerfile.
      default = "sha256:709b78bc5c378aa8ccec9e459022e440773dd72cb7a1381ccbc2c01f8102aa66";
      description = ''
        The runtime image, pinned by local image ID (it is built on nardol,
        not pulled). Rebuild and re-pin as ./palantir/Dockerfile describes;
        the app code is mounted from the Nix store, so code changes need no
        rebuild.
      '';
    };

    profile = lib.mkOption {
      type = lib.types.str;
      default = "glm-4.6v-flash";
      description = "The inference profile Palantír runs beside; any other profile skips the start.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8003;
    };

    dataDir = lib.mkOption {
      type = lib.types.str;
      default = "/srv/palantir";
      description = ''
        Videos (copied in under their content hash), the SQLite index, the
        tool models' weights, and the inbox a request may name files from.
      '';
    };

    library = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "/mnt/archive/home-videos" ];
      description = ''
        Read-only host directories requests may name videos from, mounted at
        the same paths inside the container. Empty until the archive is
        mounted on nardol.
      '';
    };

    faceThreshold = lib.mkOption {
      type = lib.types.str;
      default = "0.45";
      description = ''
        Cosine similarity (InsightFace buffalo_l) at or above which a face
        counts as an enrolled person. Measured 2026-10-08 on a family clip:
        true matches 0.48-0.95, the closest lookalike decoy 0.30.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0750 root root -"
      "d ${cfg.dataDir}/inbox 0770 root users -"
    ];

    systemd.services.docker-palantir = {
      description = "Palantír video agent (beside the ${cfg.profile} inference profile)";
      # Starts with inference, stops and restarts with it (PartOf), so gaming,
      # which stops inference, stops this too and frees the GPU for the handover.
      wantedBy = [ inferenceUnit ];
      partOf = [ inferenceUnit ];
      after = [
        inferenceUnit
        "docker.service"
      ];
      path = [ config.virtualisation.docker.package ];
      preStart = "docker rm -f palantir || true";
      script = ''
        exec docker ${
          lib.escapeShellArgs (
            [
              "run"
              "--name=palantir"
              "--log-driver=journald"
              "--rm"
              "--gpus=all"
              "--network=host"
              "-v"
              "${cfg.dataDir}:/data"
              "-v"
              "${app}:/app:ro"
            ]
            ++ lib.concatMap (p: [
              "-v"
              "${p}:${p}:ro"
            ]) cfg.library
            ++ [
              "-e"
              "PALANTIR_DATA=/data"
              "-e"
              "HF_HOME=/data/models/hf"
              "-e"
              "PALANTIR_GLM=http://127.0.0.1:${toString inference.port}/v1"
              "-e"
              "PALANTIR_LIBRARY=${lib.concatStringsSep ":" cfg.library}"
              "-e"
              "PALANTIR_FACE_THRESHOLD=${cfg.faceThreshold}"
              "-w"
              "/app"
              "--entrypoint"
              "python"
              cfg.image
              "-m"
              "uvicorn"
              "palantir.server:app"
              "--host"
              "0.0.0.0"
              "--port"
              (toString cfg.port)
            ]
          )
        }
      '';
      preStop = "docker stop palantir || true";
      postStop = "docker rm -f palantir || true";
      # ⛔ NO START LIMIT. Every inference start starts this unit too, and a
      # start skipped by the ExecCondition still counts against the limit, so
      # five model switches in ten minutes left it start-limit-hit, refusing
      # to start even after GLM was selected again (2026-10-08). A crash loop
      # is still paced by RestartSec.
      startLimitIntervalSec = 0;
      serviceConfig = {
        # Skip, do not fail, when another profile is selected: a condition
        # that is false leaves the unit inactive without counting a failure.
        ExecCondition = pkgs.writeShellScript "palantir-profile-check" ''
          p=$(cat ${lib.escapeShellArg inference.profileStateFile} 2>/dev/null || true)
          [ "$p" = ${lib.escapeShellArg cfg.profile} ]
        '';
        Restart = "on-failure";
        RestartSec = "30s";
        TimeoutStopSec = 60;
      };
    };

    networking.firewall.interfaces.eth0.allowedTCPPorts = [ cfg.port ];
    networking.firewall.interfaces.tailscale0.allowedTCPPorts = [ cfg.port ];
  };
}
