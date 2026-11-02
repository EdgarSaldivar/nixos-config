{
  pkgs,
  lib,
  ...
}:
let
  policyFor =
    enable: image:
    import ../hosts/nixos/nardol/wolf/image-config-policy.nix {
      inherit lib pkgs;
      config.nardol.eglantine = {
        inherit enable image;
      };
    };
  testImage = "ghcr.io/example/test-only@sha256:${
    builtins.concatStringsSep "" (builtins.genList (_: "a") 64)
  }";
  disabled = policyFor false testImage;
  enabled = policyFor true testImage;
  invalid = policyFor true null;
  user = doc: builtins.head (builtins.filter (profile: profile.id == "user") doc.profiles);
  guest = doc: builtins.head (builtins.filter (profile: profile.id == "guest") doc.profiles);
  eglantine = builtins.head (
    builtins.filter (app: app.title == "Eglantine") (user enabled.wolfConfigData).apps
  );
  mapApp =
    doc: profileId: title: f:
    doc
    // {
      profiles = map (
        profile:
        if profile.id == profileId then
          profile
          // {
            apps = map (app: if app.title == title then f app else app) profile.apps;
          }
        else
          profile
      ) doc.profiles;
    };
  enabledDoc = enabled.wolfConfigData;
  disabledDoc = disabled.wolfConfigData;
  checked =
    disabled.validateWolfConfig disabledDoc
    && enabled.validateWolfConfig enabledDoc
    && !(lib.any (app: app.title == "Eglantine") (user disabledDoc).apps)
    && lib.all (app: lib.elem app (user enabledDoc).apps) (user disabledDoc).apps
    && (builtins.length (builtins.filter (app: app.title == "Eglantine") (user enabledDoc).apps) == 1)
    && !(lib.any (app: app.title == "Eglantine") (guest enabledDoc).apps)
    && !(disabled.validateWolfConfig enabledDoc)
    && !(enabled.validateWolfConfig disabledDoc)
    && !(enabled.validateWolfConfig (
      mapApp enabledDoc "user" "Eglantine" (
        app:
        app
        // {
          runner = app.runner // {
            image = "unreviewed:latest";
          };
        }
      )
    ))
    && !(enabled.validateWolfConfig (
      mapApp enabledDoc "user" "Eglantine" (
        app:
        app
        // {
          runner = app.runner // {
            mounts = [ "/srv/games/switch:/home/retro/Games/Switch:rw" ];
          };
        }
      )
    ))
    && !(enabled.validateWolfConfig (
      enabledDoc
      // {
        profiles = map (
          profile:
          if profile.id == "guest" then profile // { apps = profile.apps ++ [ eglantine ]; } else profile
        ) enabledDoc.profiles;
      }
    ))
    && !(builtins.head invalid.configuration.assertions).assertion;
in
if checked then
  pkgs.runCommand "nardol-eglantine-contract" { } ''
    touch "$out"
  ''
else
  throw "Eglantine Wolf identity or opt-in contract failed"
