{
  lib,
  pkgs,
}:
let
  # Multi-architecture GoW manifests resolved on 2026-08-07; the Nardol Steam
  # toolbox was built, scanned, and attested on 2026-08-08. Update the
  # controller, template, and child pins together, then run a Moonlight smoke
  # test instead of letting any of these root-equivalent containers drift.
  wolfImage = "ghcr.io/games-on-whales/wolf@sha256:ff82c125c9b79b2e9443de2b0eaec40c904edb03291680d408cccd57c1d59c76";
  steamToolsTag = "ghcr.io/edgarsaldivar/nardol-steam-tools:git-214fce8091fc0524d64996a3b225ee3a98251c36";
  steamToolsImage = "ghcr.io/edgarsaldivar/nardol-steam-tools@sha256:629951ab9461def4aa78424d45a5748c7a114b421a46c68a86609126cb1238d8";
  upstreamSteamImage = "ghcr.io/games-on-whales/steam@sha256:ded0b1b47acd9adb8af9f068342f26ac31008904d9bbb91045d1a04e7d66a632";
  wolfImagePins = {
    "ghcr.io/games-on-whales/es-de:edge" =
      "ghcr.io/games-on-whales/es-de@sha256:f5d1037e9dd6ff7406e190e00457152d0a9dcb4adbc32fe2132585cb5bbe7829";
    "ghcr.io/games-on-whales/firefox:edge" =
      "ghcr.io/games-on-whales/firefox@sha256:1ea7331934d31d346079fb67462b371586d65b5ebb792acee8c0e64e87c185b1";
    "ghcr.io/games-on-whales/kodi:edge" =
      "ghcr.io/games-on-whales/kodi@sha256:e3db2ca9492b85f98c253436c22d47c841009d278b7e8dc7f3f349aca2ebfe8a";
    "ghcr.io/games-on-whales/lutris:edge" =
      "ghcr.io/games-on-whales/lutris@sha256:207005d9e1a839814c7c2b91fa25190d40c388c7dc004eec593556bd807f99f2";
    "ghcr.io/games-on-whales/pegasus:edge" =
      "ghcr.io/games-on-whales/pegasus@sha256:29e7ab082f1c73a92ff25dff66983a83790d109ea49e0826cb0279b7fe5eacd8";
    "ghcr.io/games-on-whales/prismlauncher:edge" =
      "ghcr.io/games-on-whales/prismlauncher@sha256:e2c610f666b019a2e31482641cab6c3330a24add41fd88f939a15f327bf9dda0";
    "ghcr.io/games-on-whales/pulseaudio:master" =
      "ghcr.io/games-on-whales/pulseaudio@sha256:5f05a7102bdb6c464a96cb33770eb10c7fb6ca0c007961e3edd5915907643bed";
    "ghcr.io/games-on-whales/retroarch:edge" =
      "ghcr.io/games-on-whales/retroarch@sha256:bbcf4523e589fc7177b522ce56ba9507c6530caaf1999e37b37062a189f18cf2";
    "${steamToolsTag}" = steamToolsImage;
    "ghcr.io/games-on-whales/wolf-ui:main" =
      "ghcr.io/games-on-whales/wolf-ui@sha256:cd6de1158b29068e4a4d4ce6312976067517239be97200a391be758a6ddfcf9b";
    "ghcr.io/games-on-whales/xfce:edge" =
      "ghcr.io/games-on-whales/xfce@sha256:2ce1db7432bcb60caf5b3da23ea0ad5a24f300f3e7f346045fd6ba74a477ebcd";
  };
  # Normalise a generated pre-toolbox config without rewriting unrelated app
  # state. These sources are migration inputs, not valid template identities.
  wolfImageMigrations = {
    "ghcr.io/games-on-whales/steam:edge" = steamToolsImage;
    "${upstreamSteamImage}" = steamToolsImage;
  };
  wolfPaths = import ../wolf-paths.nix;
  wolfImageTags = builtins.attrNames wolfImagePins;
  wolfConfigText = builtins.replaceStrings wolfImageTags (map (
    tag: wolfImagePins.${tag}
  ) wolfImageTags) (builtins.readFile ../wolf-config.template.toml);
  wolfConfigImageLines = builtins.filter (
    line: builtins.match "[[:space:]]*image[[:space:]]*=.*" line != null
  ) (lib.splitString "\n" wolfConfigText);
  wolfConfigData = builtins.fromTOML wolfConfigText;
  validateWolfConfig = import ../wolf-validator.nix {
    inherit lib;
    paths = wolfPaths;
  };
  wolfConfigTemplate = pkgs.writeText "nardol-wolf-config.toml" wolfConfigText;
  wolfImageRewrites = wolfImagePins // wolfImageMigrations;
  wolfPathsJson = pkgs.writeText "nardol-wolf-paths.json" (builtins.toJSON wolfPaths);
  wolfImageRewritesJson = pkgs.writeText "nardol-wolf-image-rewrites.json" (
    builtins.toJSON wolfImageRewrites
  );
  wolfState = "/srv/wolf";
  wolfHostStatePath = "${wolfState}/data";
  wolfContainerStatePath = "/var/lib/wolf";
  wolfPython = pkgs.python3.withPackages (pythonPackages: [ pythonPackages.tomlkit ]);
  wolfConfigPolicy = pkgs.writeShellApplication {
    name = "nardol-wolf-config-policy";
    text = ''
      exec ${wolfPython}/bin/python ${../wolf-reconcile.py} \
        --template ${wolfConfigTemplate} \
        --config ${wolfHostStatePath}/cfg/config.toml \
        --paths-json ${wolfPathsJson} \
        --rewrites-json ${wolfImageRewritesJson}
    '';
  };
in
{
  inherit
    validateWolfConfig
    wolfConfigData
    wolfConfigImageLines
    wolfConfigPolicy
    wolfContainerStatePath
    wolfHostStatePath
    wolfImage
    wolfImagePins
    ;

  configuration = {
    # Wolf derives both cfg/ (configuration, certificate, and pairing) and
    # profile-data/ from HOST_APPS_STATE_FOLDER, so keep that combined state on
    # the encrypted SSD and separate only the large shared game library.
    systemd.tmpfiles.rules = [
      "d /srv/docker 0710 root docker - -"
      "d /srv/games 0750 1000 1000 - -"
      "d ${wolfPaths.user.nonsteam} 0750 1000 1000 - -"
      "d ${wolfPaths.user.steamapps} 0750 1000 1000 - -"
      # Hardlink/atomic-rename deployers need staging inside the same container
      # mount as their game targets, not merely on the same host filesystem.
      "d ${wolfPaths.user.modStaging} 0750 1000 1000 - -"
      "d ${wolfPaths.user.mods} 0750 1000 1000 - -"
      "d ${wolfPaths.user.backups} 0750 1000 1000 - -"
      "d ${wolfPaths.user.downloads} 0750 1000 1000 - -"
      "d ${wolfPaths.user.logs} 0750 1000 1000 - -"
      "d ${wolfPaths.user.manifests} 0750 1000 1000 - -"
      "d ${wolfPaths.user.tools} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.nonsteam} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.steamapps} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.modStaging} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.mods} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.backups} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.downloads} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.logs} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.manifests} 0750 1000 1000 - -"
      "d ${wolfPaths.guest.tools} 0750 1000 1000 - -"
      "d ${wolfState} 0750 root root - -"
      "d ${wolfHostStatePath} 0750 1000 1000 - -"
      "d /run/wolf 0755 root root - -"
    ];
  };
}
