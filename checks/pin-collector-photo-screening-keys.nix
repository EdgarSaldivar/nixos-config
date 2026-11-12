{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Photo screening (PinCollector #290), behind three release gates. photoScreeningKey declares
# openai_api_key in sops, adds it to the pin-collector-runtime Secret, mounts it into the API and
# points PIN_COLLECTOR_OPENAI_API_KEY_FILE at it. photoChecks sets PIN_COLLECTOR_PHOTO_CHECKS_ENABLED
# and names itself in an annotation of the API's Pod template, so flipping it restarts the API; the
# contract refuses it without the key. peopleReportsAlert does what photoScreeningKey does for
# people_reports_alert_url, as PIN_COLLECTOR_PEOPLE_REPORTS_ALERT_URL_FILE, and the contract refuses
# it on the host while that secret isn't in secrets/pin-collector.yaml. Off (or absent) the gates
# change nothing at all; on, exactly that. The nightly release import and the migration get none of
# it, and no gate renames the migration Job.
let
  pinned = builtins.removeAttrs (import ../hosts/nixos/minas-tirith/pin-collector-release.nix) [
    "photoScreeningKey"
    "photoChecks"
    "peopleReportsAlert"
    "stardustConversion"
  ];
  render =
    release:
    (import ../hosts/nixos/pelargir/manifest-renderers.nix {
      inherit lib pkgs;
      pinCollectorReleaseSource = release;
    }).pinCollectorManifest;
  allOn = {
    photoScreeningKey = true;
    photoChecks = true;
    peopleReportsAlert = true;
  };
  absent = render pinned;
  off = render (
    pinned
    // {
      photoScreeningKey = false;
      photoChecks = false;
      peopleReportsAlert = false;
    }
  );
  keyOnly = render (pinned // { photoScreeningKey = true; });
  on = render (pinned // allOn);
  dryRunOn = render (pinned // allOn // { stardustConversion = "dry-run"; });

  contract = import ../hosts/nixos/minas-tirith/pin-collector-release-contract.nix { inherit lib; };
  takes = release: (builtins.tryEval (builtins.seq (contract.assertValid release) true)).success;
  hostTakes =
    release: (builtins.tryEval (builtins.seq (contract.assertSecretsPresent release) true)).success;
  # Whether the SOPS document names a key, read another way than the contract reads it.
  sopsText = "\n" + builtins.readFile ../secrets/pin-collector.yaml;
  inSops = name: lib.hasInfix "\n${name}: " sopsText || lib.hasInfix "\n${name}:\n" sopsText;

  # The host as pinned: its sops declarations and its applier follow the release file.
  release = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
  keyGate = release.photoScreeningKey or false;
  alertGate = release.peopleReportsAlert or false;
  pelargir = nixosConfigurations.pelargir.config;
  script = pelargir.systemd.services.k3s-apply-secrets.script;
  declared = name: builtins.elem name (lib.attrNames pelargir.sops.secrets);
  sopsSource = builtins.readFile ../hosts/nixos/pelargir/secrets/sops.nix;
  problems =
    lib.optional (
      keyGate != declared "pin_collector_openai_api_key"
    ) "pelargir's openai_api_key declaration doesn't follow photoScreeningKey"
    ++ lib.optional (
      alertGate != declared "pin_collector_people_reports_alert_url"
    ) "pelargir's people_reports_alert_url declaration doesn't follow peopleReportsAlert"
    ++ lib.optional (
      keyGate != lib.hasInfix "--from-file=openai-api-key=" script
    ) "the applier's OpenAI key file doesn't follow photoScreeningKey"
    ++ lib.optional (
      alertGate != lib.hasInfix "--from-file=people-reports-alert-url=" script
    ) "the applier's alert URL file doesn't follow peopleReportsAlert"
    ++ lib.optional (
      !lib.hasInfix ''"openai_api_key"'' sopsSource
    ) "sops.nix doesn't name openai_api_key"
    ++ lib.optional (
      !lib.hasInfix ''"people_reports_alert_url"'' sopsSource
    ) "sops.nix doesn't name people_reports_alert_url"
    # The switch needs the key; all three are booleans.
    ++ lib.optional (takes (
      pinned // { photoChecks = true; }
    )) "the release contract takes photoChecks without photoScreeningKey"
    ++ lib.optional (takes (
      pinned
      // {
        photoChecks = true;
        photoScreeningKey = false;
      }
    )) "the release contract takes photoChecks with photoScreeningKey off"
    ++ lib.optional (!takes (pinned // allOn)) "the release contract refuses every gate on"
    ++ lib.optional (takes (
      pinned // { photoScreeningKey = "true"; }
    )) "the release contract takes a non-boolean photoScreeningKey"
    ++ lib.optional (takes (
      pinned
      // {
        photoScreeningKey = true;
        photoChecks = 1;
      }
    )) "the release contract takes a non-boolean photoChecks"
    ++ lib.optional (takes (
      pinned // { peopleReportsAlert = "yes"; }
    )) "the release contract takes a non-boolean peopleReportsAlert"
    # On the host, each gate is taken exactly when its secret is in the SOPS document.
    ++ lib.optional (
      contract.hasPinCollectorSecret "openai_api_key" != inSops "openai_api_key"
    ) "the contract and the SOPS document disagree about openai_api_key"
    ++ lib.optional (
      contract.hasPinCollectorSecret "people_reports_alert_url" != inSops "people_reports_alert_url"
    ) "the contract and the SOPS document disagree about people_reports_alert_url"
    ++ lib.optional (
      !(contract.hasPinCollectorSecret "resend_api_key")
    ) "the contract doesn't find resend_api_key, which the SOPS document holds"
    ++ lib.optional (contract.hasPinCollectorSecret "no_such_secret") "the contract finds a key the SOPS document doesn't hold"
    ++ lib.optional (
      hostTakes (pinned // { photoScreeningKey = true; }) != inSops "openai_api_key"
    ) "the host takes photoScreeningKey regardless of openai_api_key"
    ++ lib.optional (
      hostTakes (pinned // { peopleReportsAlert = true; }) != inSops "people_reports_alert_url"
    ) "the host takes peopleReportsAlert regardless of people_reports_alert_url"
    ++ lib.optional (
      !hostTakes (
        pinned
        // {
          photoScreeningKey = false;
          peopleReportsAlert = false;
        }
      )
    ) "the host refuses the gates off";
in
if problems != [ ] then
  throw "PinCollector photo screening keys: ${lib.concatStringsSep "; " problems}"
else
  pkgs.runCommand "pin-collector-photo-screening-keys"
    {
      nativeBuildInputs = [ pkgs.yq-go ];
      inherit
        absent
        off
        keyOnly
        on
        dryRunOn
        ;
    }
    ''
      set -euo pipefail
      fail() { echo "pin-collector-photo-screening-keys: $*" >&2; exit 1; }
      q() { yq -N "$1" "$2"; }
      j() { yq -N -o=json -I=0 "$1" "$2"; }

      for f in "$absent" "$off" "$keyOnly" "$on" "$dryRunOn"; do
        yq -N 'true' "$f" > /dev/null || fail "$f does not parse as YAML"
      done

      # Off or absent: byte for byte the same manifest, so merging the gates off restarts nothing.
      cmp -s "$absent" "$off" || fail "photoScreeningKey/photoChecks/peopleReportsAlert = false change the manifest"

      config='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-api-config") | .data'
      apiPod='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec'
      annotations='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.metadata.annotations'
      items="$apiPod | .volumes[] | select(.name == \"runtime-secret\") | .secret.items[]"
      mount="$apiPod | .containers[] | select(.name == \"api\") | .volumeMounts[] | select(.name == \"runtime-secret\") | .mountPath"
      job='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-migrate") | .metadata.name'
      vars="PIN_COLLECTOR_OPENAI_API_KEY_FILE PIN_COLLECTOR_PHOTO_CHECKS_ENABLED PIN_COLLECTOR_PEOPLE_REPORTS_ALERT_URL_FILE"

      for var in $vars; do
        [ "$(q "$config | has(\"$var\")" "$off")" = false ] || fail "$var set with the gates off"
      done
      for key in openai-api-key people-reports-alert-url; do
        [ -z "$(q "$items | select(.key == \"$key\") | .path" "$off")" ] || fail "$key mounted with its gate off"
      done
      [ "$(q "$annotations | has(\"pin-collector.saldivar.io/photo-checks\")" "$off")" = false ] \
        || fail "the photo-checks annotation is rendered with photoChecks off"

      # The key alone: mounted and configured, and nothing else of photo screening.
      for f in "$keyOnly" "$on"; do
        path=$(q "$items | select(.key == \"openai-api-key\") | .path" "$f")
        [ "$path" = openai-api-key ] || fail "openai-api-key is not mounted ($f)"
        [ "$(q "$items | select(.key == \"openai-api-key\") | .mode" "$f")" = "$(q "$items | select(.key == \"database-url\") | .mode" "$f")" ] \
          || fail "openai-api-key's mode is not the other items' ($f)"
        [ "$(q "$config | .PIN_COLLECTOR_OPENAI_API_KEY_FILE" "$f")" = "$(q "$mount" "$f")/$path" ] \
          || fail "PIN_COLLECTOR_OPENAI_API_KEY_FILE is not the mounted key ($f)"
      done
      for var in PIN_COLLECTOR_PHOTO_CHECKS_ENABLED PIN_COLLECTOR_PEOPLE_REPORTS_ALERT_URL_FILE; do
        [ "$(q "$config | has(\"$var\")" "$keyOnly")" = false ] || fail "$var set with only photoScreeningKey on"
      done
      [ -z "$(q "$items | select(.key == \"people-reports-alert-url\") | .path" "$keyOnly")" ] \
        || fail "the alert URL is mounted with only photoScreeningKey on"
      [ "$(q "$annotations | has(\"pin-collector.saldivar.io/photo-checks\")" "$keyOnly")" = false ] \
        || fail "the photo-checks annotation is rendered with only photoScreeningKey on"

      # All on: the switch is the string "true" and restarts the API through its own annotation,
      # leaving the feature-gates one as it was; the alert URL is exactly a mounted item.
      [ "$(q "$config | .PIN_COLLECTOR_PHOTO_CHECKS_ENABLED" "$on")" = true ] || fail "PHOTO_CHECKS_ENABLED is not true with photoChecks on"
      [ "$(q "$config | .PIN_COLLECTOR_PHOTO_CHECKS_ENABLED | tag" "$on")" = '!!str' ] || fail "PHOTO_CHECKS_ENABLED is not a string"
      [ "$(q "$annotations | .[\"pin-collector.saldivar.io/photo-checks\"]" "$on")" = on ] || fail "the photo-checks annotation is not on"
      [ "$(q "$annotations | .[\"pin-collector.saldivar.io/feature-gates\"]" "$on")" = "$(q "$annotations | .[\"pin-collector.saldivar.io/feature-gates\"]" "$absent")" ] \
        || fail "photoChecks changes the feature-gates annotation"
      path=$(q "$items | select(.key == \"people-reports-alert-url\") | .path" "$on")
      [ "$path" = people-reports-alert-url ] || fail "people-reports-alert-url is not mounted"
      [ "$(q "$items | select(.key == \"people-reports-alert-url\") | .mode" "$on")" = "$(q "$items | select(.key == \"database-url\") | .mode" "$on")" ] \
        || fail "people-reports-alert-url's mode is not the other items'"
      [ "$(q "$config | .PIN_COLLECTOR_PEOPLE_REPORTS_ALERT_URL_FILE" "$on")" = "$(q "$mount" "$on")/$path" ] \
        || fail "PIN_COLLECTOR_PEOPLE_REPORTS_ALERT_URL_FILE is not the mounted URL"

      # The API's alone: neither the nightly release import nor the migration gets any of it.
      importConfig='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-release-import-config") | .data'
      migrationConfig='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-migration-config") | .data'
      for var in $vars; do
        [ "$(q "$importConfig | has(\"$var\")" "$on")" = false ] || fail "$var reaches the release import's config"
        [ "$(q "$migrationConfig | has(\"$var\")" "$on")" = false ] || fail "$var reaches the migration's config"
      done
      importKeys=$(q 'select(.kind == "CronJob" and .metadata.name == "release-import") | .spec.jobTemplate.spec.template.spec.volumes[] | select(.name == "runtime-secret") | .secret.items[].key' "$on")
      echo "$importKeys" | grep -Fxq database-url || fail "the release import's secret items didn't read"
      for key in openai-api-key people-reports-alert-url; do
        if echo "$importKeys" | grep -Fxq "$key"; then fail "$key is mounted into the release import"; fi
      done

      # The migration Job keeps its name, and the conversion Job carries the API's secret items exactly.
      for f in "$keyOnly" "$on"; do
        [ "$(q "$job" "$f")" = "$(q "$job" "$absent")" ] || fail "the gates rename the migration Job ($f)"
      done
      conv='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-stardust-conversion") | .spec.template.spec'
      [ "$(j "$conv | .volumes" "$dryRunOn")" = "$(j "$apiPod | .volumes" "$dryRunOn")" ] \
        || fail "the conversion Job's secret items differ from the API's"

      touch "$out"
    ''
