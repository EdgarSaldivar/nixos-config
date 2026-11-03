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
  previousImages = [
    "ghcr.io/edgarsaldivar/nardol-eglantine-wolf@sha256:4c2239f622b40685a48a3628969f2f26e639032695aa4bb472e3f44892d2d107"
    "ghcr.io/edgarsaldivar/nardol-eglantine-wolf@sha256:4a68643349a14c0b1baba497f0c0a514f90dc651e9dbfc4289256ad6daaa3022"
    "ghcr.io/edgarsaldivar/nardol-eglantine-wolf@sha256:5884b9425df8dec6bf0d45a6288ffd560116adfa508cbd2c9459eccfa73455e5"
    "ghcr.io/edgarsaldivar/nardol-eglantine-wolf@sha256:b46034472c56b56a7f7a06ec93de6af959383b6eadd97be440dfa92e24211a4b"
  ];
  checked =
    lib.all (image: enabled.wolfImageRewrites.${image} == testImage) previousImages
    && lib.all (image: !(builtins.hasAttr image disabled.wolfImageRewrites)) previousImages
    && disabled.validateWolfConfig disabledDoc
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
