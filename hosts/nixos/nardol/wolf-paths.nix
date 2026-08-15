# One source of truth for profile-owned host storage and the mount strings
# consumed by both the runtime module and the flake's independent contract.
let
  mkProfile =
    {
      steamapps,
      nonsteam,
      mods,
      namedVolumes ? [ ],
    }:
    {
      inherit
        steamapps
        nonsteam
        mods
        namedVolumes
        ;
      modStaging = "${steamapps}/.nardol-mod-staging";
      backups = "${mods}/backups";
      downloads = "${mods}/downloads";
      logs = "${mods}/logs";
      manifests = "${mods}/manifests";
      tools = "${mods}/tools";
      writableSources = [
        steamapps
        nonsteam
        mods
        "${mods}/downloads"
      ];
      mounts = {
        steamLibrary = "${steamapps}:/home/retro/.steam/steam/steamapps:rw";
        graphicalSteamLibrary = "${steamapps}:/home/retro/Games/Steam/steamapps:rw";
        nonsteam = "${nonsteam}:/home/retro/Games/NonSteam:rw";
        mods = "${mods}:/home/retro/Mods:rw";
        modding = "${mods}:/home/retro/Modding:rw";
        downloads = "${mods}/downloads:/home/retro/Downloads:rw";
      };
    };
in
{
  user = mkProfile {
    steamapps = "/srv/games/steamapps";
    nonsteam = "/srv/games/nonsteam";
    mods = "/srv/mods";
    namedVolumes = [ "lutris" ];
  };
  guest = mkProfile {
    steamapps = "/srv/games/guest-steamapps";
    nonsteam = "/srv/games/guest-nonsteam";
    mods = "/srv/mods-guest";
  };
}
