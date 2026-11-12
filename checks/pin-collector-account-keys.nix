{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Account mail and Sign in with Apple's key, each behind its own release gate. accountMail declares
# resend_api_key in sops, adds it to the pin-collector-runtime Secret, mounts it into the API and
# points PIN_COLLECTOR_RESEND_API_KEY_FILE at it, with the verified sender in PIN_COLLECTOR_MAIL_FROM.
# appleSignInKey does the same for apple_sign_in_key, with the team and the release's Key ID. Off (or
# absent) each must change nothing at all; on, exactly that. The nightly release import gets neither,
# and neither renames the migration Job.
let
  pinned = builtins.removeAttrs (import ../hosts/nixos/minas-tirith/pin-collector-release.nix) [
    "accountMail"
    "appleSignInKey"
    "appleKeyId"
    "stardustConversion"
  ];
  render =
    release:
    (import ../hosts/nixos/pelargir/manifest-renderers.nix {
      inherit lib pkgs;
      pinCollectorReleaseSource = release;
    }).pinCollectorManifest;
  keyId = "ABCDE12345";
  absent = render pinned;
  off = render (
    pinned
    // {
      accountMail = false;
      appleSignInKey = false;
    }
  );
  on = render (
    pinned
    // {
      accountMail = true;
      appleSignInKey = true;
      appleKeyId = keyId;
    }
  );
  dryRunOn = render (
    pinned
    // {
      accountMail = true;
      appleSignInKey = true;
      appleKeyId = keyId;
      stardustConversion = "dry-run";
    }
  );
  # The contract refuses Apple's gate without a Key ID, or with one that isn't one.
  contract = import ../hosts/nixos/minas-tirith/pin-collector-release-contract.nix { inherit lib; };
  takes = release: (builtins.tryEval (builtins.seq (contract.assertValid release) true)).success;

  # The host as pinned: its sops declarations and its applier follow the release file.
  release = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
  mailGate = release.accountMail or false;
  appleGate = release.appleSignInKey or false;
  pelargir = nixosConfigurations.pelargir.config;
  script = pelargir.systemd.services.k3s-apply-secrets.script;
  declared = name: builtins.elem name (lib.attrNames pelargir.sops.secrets);
  sopsSource = builtins.readFile ../hosts/nixos/pelargir/secrets/sops.nix;
  problems =
    lib.optional (
      mailGate != declared "pin_collector_resend_api_key"
    ) "pelargir's resend_api_key declaration doesn't follow accountMail"
    ++ lib.optional (
      appleGate != declared "pin_collector_apple_sign_in_key"
    ) "pelargir's apple_sign_in_key declaration doesn't follow appleSignInKey"
    ++ lib.optional (
      mailGate != lib.hasInfix "--from-file=resend-api-key=" script
    ) "the applier's Resend key file doesn't follow accountMail"
    ++ lib.optional (
      appleGate != lib.hasInfix "--from-file=apple-sign-in-key=" script
    ) "the applier's Apple key file doesn't follow appleSignInKey"
    ++ lib.optional (
      !lib.hasInfix ''"resend_api_key"'' sopsSource
    ) "sops.nix doesn't name resend_api_key"
    ++ lib.optional (
      !lib.hasInfix ''"apple_sign_in_key"'' sopsSource
    ) "sops.nix doesn't name apple_sign_in_key"
    ++ lib.optional (takes (
      pinned // { appleSignInKey = true; }
    )) "the release contract takes appleSignInKey without appleKeyId"
    ++ lib.optional (takes (
      pinned
      // {
        appleSignInKey = true;
        appleKeyId = "abc";
      }
    )) "the release contract takes a malformed appleKeyId"
    ++ lib.optional (
      !takes (
        pinned
        // {
          appleSignInKey = true;
          appleKeyId = keyId;
        }
      )
    ) "the release contract refuses a well-formed appleKeyId";
in
if problems != [ ] then
  throw "PinCollector account keys: ${lib.concatStringsSep "; " problems}"
else
  pkgs.runCommand "pin-collector-account-keys"
    {
      nativeBuildInputs = [ pkgs.yq-go ];
      inherit
        absent
        off
        on
        dryRunOn
        keyId
        ;
    }
    ''
      set -euo pipefail
      fail() { echo "pin-collector-account-keys: $*" >&2; exit 1; }
      q() { yq -N "$1" "$2"; }
      j() { yq -N -o=json -I=0 "$1" "$2"; }

      for f in "$absent" "$off" "$on" "$dryRunOn"; do
        yq -N 'true' "$f" > /dev/null || fail "$f does not parse as YAML"
      done

      # Off or absent: byte for byte the same manifest, so merging this restarts nothing.
      cmp -s "$absent" "$off" || fail "accountMail/appleSignInKey = false changes the manifest"

      config='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-api-config") | .data'
      importConfig='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-release-import-config") | .data'
      apiPod='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec'
      items="$apiPod | .volumes[] | select(.name == \"runtime-secret\") | .secret.items[]"
      mount="$apiPod | .containers[] | select(.name == \"api\") | .volumeMounts[] | select(.name == \"runtime-secret\") | .mountPath"
      job='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-migrate") | .metadata.name'

      for var in PIN_COLLECTOR_RESEND_API_KEY_FILE PIN_COLLECTOR_MAIL_FROM PIN_COLLECTOR_APPLE_TEAM_ID PIN_COLLECTOR_APPLE_KEY_ID PIN_COLLECTOR_APPLE_PRIVATE_KEY_FILE; do
        [ "$(q "$config | has(\"$var\")" "$off")" = false ] || fail "$var set with the gates off"
        [ "$(q "$importConfig | has(\"$var\")" "$on")" = false ] || fail "$var reaches the release import's config"
      done
      for key in resend-api-key apple-sign-in-key; do
        [ -z "$(q "$items | select(.key == \"$key\") | .path" "$off")" ] || fail "$key mounted with its gate off"
      done

      # On: each configured file is exactly a mounted item, with the other items' mode.
      for pair in PIN_COLLECTOR_RESEND_API_KEY_FILE:resend-api-key PIN_COLLECTOR_APPLE_PRIVATE_KEY_FILE:apple-sign-in-key; do
        var=''${pair%%:*}; key=''${pair#*:}
        path=$(q "$items | select(.key == \"$key\") | .path" "$on")
        [ "$path" = "$key" ] || fail "$key is not mounted"
        [ "$(q "$items | select(.key == \"$key\") | .mode" "$on")" = "$(q "$items | select(.key == \"database-url\") | .mode" "$on")" ] \
          || fail "$key's mode is not the other items'"
        [ "$(q "$config | .$var" "$on")" = "$(q "$mount" "$on")/$path" ] || fail "$var is not the mounted $key"
      done
      [ "$(q "$config | .PIN_COLLECTOR_MAIL_FROM" "$on")" = "PinStellation <no-reply@pinstellation.com>" ] || fail "the sender isn't PinStellation's"
      [ "$(q "$config | .PIN_COLLECTOR_APPLE_TEAM_ID" "$on")" = N449SAELDJ ] || fail "the Apple team isn't N449SAELDJ"
      [ "$(q "$config | .PIN_COLLECTOR_APPLE_KEY_ID" "$on")" = "$keyId" ] || fail "the Apple Key ID isn't the release's"

      # The migration Job keeps its name, and the conversion Job carries the API's secret items exactly.
      [ "$(q "$job" "$on")" = "$(q "$job" "$absent")" ] || fail "the gates rename the migration Job"
      conv='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-stardust-conversion") | .spec.template.spec'
      [ "$(j "$conv | .volumes" "$dryRunOn")" = "$(j "$apiPod | .volumes" "$dryRunOn")" ] \
        || fail "the conversion Job's secret items differ from the API's"

      touch "$out"
    ''
