{
  lib,
  pkgs,
  ...
}:

# The PinCollector release gates (notificationSender, collectorPages, stardust, plus) and
# the stardust conversion Job render config that only a newer image can use, or that runs
# once, so the pinned release usually has them off and nothing else ever renders them on.
# An indentation slip in a gated line would then surface first at the release deploy, as
# an AddOn that never converges. Render every gate state here and assert the objects they
# are meant to produce.
let
  # The pinned release without the Wave 2 fields, so every state below starts from
  # "absent" whatever the release file currently declares.
  pinned = builtins.removeAttrs (import ../hosts/nixos/minas-tirith/pin-collector-release.nix) [
    "stardust"
    "plus"
    "stardustConversion"
  ];
  render =
    release:
    (import ../hosts/nixos/pelargir/manifest-renderers.nix {
      inherit lib pkgs;
      pinCollectorReleaseSource = release;
    }).pinCollectorManifest;
  off = render (
    pinned
    // {
      notificationSender = false;
      collectorPages = false;
    }
  );
  on = render (
    pinned
    // {
      notificationSender = true;
      collectorPages = true;
    }
  );
  # The sender stays suspended while the release itself is not enabled.
  onDisabled = render (
    pinned
    // {
      enabled = false;
      notificationSender = true;
      collectorPages = true;
    }
  );
  absent = render pinned;
  starOff = render (
    pinned
    // {
      stardust = false;
      plus = false;
    }
  );
  starOn = render (
    pinned
    // {
      stardust = true;
      plus = true;
    }
  );
  stardustOnly = render (pinned // { stardust = true; });
  dryRun = render (pinned // { stardustConversion = "dry-run"; });
  apply = render (pinned // { stardustConversion = "apply"; });
  refresh = render (pinned // { stardustConversion = "refresh"; });
  dryRunStarOn = render (
    pinned
    // {
      stardust = true;
      plus = true;
      stardustConversion = "dry-run";
    }
  );
  dryRunPagesOff = render (
    pinned
    // {
      collectorPages = false;
      stardustConversion = "dry-run";
    }
  );
  apiDigest12 = builtins.substring 0 12 (lib.last (lib.splitString "@sha256:" pinned.apiImage));
in
pkgs.runCommand "pin-collector-feature-gates"
  {
    nativeBuildInputs = [ pkgs.yq-go ];
    inherit
      off
      on
      onDisabled
      absent
      starOff
      starOn
      stardustOnly
      dryRun
      apply
      refresh
      dryRunStarOn
      dryRunPagesOff
      apiDigest12
      ;
  }
  ''
    set -euo pipefail
    fail() { echo "pin-collector-feature-gates: $*" >&2; exit 1; }
    q() { yq -N "$1" "$2"; }
    # Compact JSON, for comparing whole subtrees.
    j() { yq -N -o=json -I=0 "$1" "$2"; }

    for f in "$off" "$on" "$onDisabled" "$absent" "$starOff" "$starOn" "$stardustOnly" \
      "$dryRun" "$apply" "$refresh" "$dryRunStarOn" "$dryRunPagesOff"; do
      yq -N 'true' "$f" > /dev/null || fail "$f does not parse as YAML"
    done

    config='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-api-config") | .data'
    items='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec.volumes[] | select(.name == "runtime-secret") | .secret.items[] | select(.key == "collector-link-key") | .path'
    mount='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec.containers[] | select(.name == "api") | .volumeMounts[] | select(.name == "runtime-secret") | .mountPath'
    suspend='select(.kind == "CronJob" and .metadata.name == "send-notifications") | .spec.suspend'
    job='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-migrate") | .metadata.name'

    # Gates off: no link key, no "Get the app" target, the sender suspended.
    [ "$(q "$config | has(\"PIN_COLLECTOR_COLLECTOR_LINK_KEY_FILE\")" "$off")" = false ] || fail "link key configured with collectorPages off"
    [ "$(q "$config | has(\"PIN_COLLECTOR_GET_APP_URL\")" "$off")" = false ] || fail "GET_APP_URL set with collectorPages off"
    [ -z "$(q "$items" "$off")" ] || fail "link key mounted with collectorPages off"
    [ "$(q "$suspend" "$off")" = true ] || fail "sender not suspended with notificationSender off"

    # Gates on: the configured key file is exactly the mounted item.
    keyfile=$(q "$config | .PIN_COLLECTOR_COLLECTOR_LINK_KEY_FILE" "$on")
    [ "$keyfile" = "$(q "$mount" "$on")/$(q "$items" "$on")" ] || fail "link key file $keyfile is not the mounted item"
    case "$(q "$config | .PIN_COLLECTOR_GET_APP_URL" "$on")" in
      https://*) ;;
      *) fail "GET_APP_URL is not an https URL" ;;
    esac
    [ "$(q "$suspend" "$on")" = false ] || fail "sender suspended with notificationSender on"
    [ "$(q "$suspend" "$onDisabled")" = true ] || fail "sender runs while the release is not enabled"

    # Flipping a gate must not rename (and so rerun) the migration Job.
    migration=$(q "$job" "$absent")
    [ -n "$migration" ] || fail "no migration Job rendered"
    [ "$(q "$job" "$off")" = "$(q "$job" "$on")" ] || fail "a gate renames the migration Job"

    # Stardust and Plus off, absent or false: byte for byte the manifest without them, so
    # merging the gates restarts nothing. No switch, no restart annotation, no conversion.
    cmp -s "$absent" "$starOff" || fail "stardust = false and plus = false change the manifest"
    for f in "$absent" "$starOff"; do
      [ "$(q "$config | has(\"PIN_COLLECTOR_STARDUST_ENABLED\")" "$f")" = false ] || fail "STARDUST_ENABLED set with stardust off"
      [ "$(q "$config | has(\"PIN_COLLECTOR_PLUS_ENABLED\")" "$f")" = false ] || fail "PLUS_ENABLED set with plus off"
    done
    gates='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.metadata.annotations["pin-collector.saldivar.io/feature-gates"]'
    [ "$(q "$gates" "$absent")" = null ] || fail "feature-gates annotation rendered with stardust and plus off"

    # On: each switch is the string "true" (ConfigMap data takes strings only), the API's
    # Pod template names them so the API restarts onto them, and the migration Job keeps
    # its name.
    for var in PIN_COLLECTOR_STARDUST_ENABLED PIN_COLLECTOR_PLUS_ENABLED; do
      [ "$(q "$config | .$var" "$starOn")" = true ] || fail "$var is not true with the gate on"
      [ "$(q "$config | .$var | tag" "$starOn")" = '!!str' ] || fail "$var is not a string"
    done
    [ "$(q "$gates" "$starOn")" = stardust,plus ] || fail "feature-gates annotation is not stardust,plus"
    [ "$(q "$gates" "$stardustOnly")" = stardust ] || fail "feature-gates annotation is not stardust"
    [ "$(q "$config | has(\"PIN_COLLECTOR_PLUS_ENABLED\")" "$stardustOnly")" = false ] || fail "PLUS_ENABLED set with only stardust on"
    [ "$(q "$job" "$starOn")" = "$migration" ] || fail "stardust or plus renames the migration Job"
    [ "$(q "$job" "$stardustOnly")" = "$migration" ] || fail "stardust renames the migration Job"

    # No conversion unless the release names a mode.
    convJob='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-stardust-conversion")'
    convPolicy='select(.kind == "NetworkPolicy" and .metadata.name == "postgres-ingress-stardust-conversion")'
    for f in "$absent" "$starOn" "$off" "$on"; do
      [ "$(yq ea -N '[select(.kind == "Job")] | length' "$f")" = 1 ] || fail "a Job other than the migration rendered without stardustConversion"
      [ -z "$(q "$convPolicy | .metadata.name" "$f")" ] || fail "conversion NetworkPolicy rendered without stardustConversion"
    done

    # Each mode: one Job in the API image with the API container's config, secret files and
    # entrypoint (args, not command), the mode's flag, no retry, appended after an
    # unchanged manifest.
    apiPod='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec'
    api="$apiPod | .containers[] | select(.name == \"api\")"
    names=""
    for mode in dry-run apply refresh; do
      case "$mode" in
        dry-run) f="$dryRun"; args='["python","-m","app.maintenance.convert_stardust"]' ;;
        apply) f="$apply"; args='["python","-m","app.maintenance.convert_stardust","--apply"]' ;;
        refresh) f="$refresh"; args='["python","-m","app.maintenance.convert_stardust","--refresh"]' ;;
      esac
      [ "$(yq ea -N "[$convJob] | length" "$f")" = 1 ] || fail "$mode: not exactly one conversion Job"
      [ "$(yq ea -N '[select(.kind == "Job")] | length' "$f")" = 2 ] || fail "$mode: unexpected Jobs"
      head -c "$(wc -c < "$absent")" "$f" | cmp -s - "$absent" || fail "$mode: the conversion changes the manifest before it"
      [ "$(q "$job" "$f")" = "$migration" ] || fail "$mode renames the migration Job"

      name=$(q "$convJob | .metadata.name" "$f")
      case "$name" in
        "pin-collector-stardust-$mode-$apiDigest12-"????????) ;;
        *) fail "$mode: Job name $name lacks the mode, the API digest or the spec hash" ;;
      esac
      [ "''${#name}" -le 63 ] || fail "$mode: Job name $name is longer than a label value"
      names="$names $name"

      main="$convJob | .spec.template.spec.containers[] | select(.name == \"convert-stardust\")"
      init="$convJob | .spec.template.spec.initContainers[] | select(.name == \"wait-for-migration-head\")"
      [ "$(j "$main | .args" "$f")" = "$args" ] || fail "$mode: args are $(j "$main | .args" "$f")"
      [ "$(q "$main | has(\"command\")" "$f")" = false ] || fail "$mode: command would bypass the entrypoint"
      [ "$(q "$init | has(\"command\")" "$f")" = false ] || fail "$mode: init command would bypass the entrypoint"
      [ "$(j "$init | .args" "$f")" = '["sh","-c","until python -m alembic current --check-heads; do sleep 15; done"]' ] \
        || fail "$mode: the init container does not wait for the migration head"
      for c in "$main" "$init"; do
        [ "$(q "$c | .image" "$f")" = "$(q "$api | .image" "$f")" ] || fail "$mode: not the API image"
        [ "$(j "$c | .envFrom" "$f")" = "$(j "$api | .envFrom" "$f")" ] || fail "$mode: envFrom differs from the API's"
        [ "$(j "$c | .volumeMounts" "$f")" = "$(j "$api | .volumeMounts" "$f")" ] || fail "$mode: mounts differ from the API's"
        [ "$(j "$c | .securityContext" "$f")" = "$(j "$api | .securityContext" "$f")" ] || fail "$mode: container securityContext differs from the API's"
      done
      for field in volumes securityContext nodeSelector imagePullSecrets; do
        [ "$(j "$convJob | .spec.template.spec.$field" "$f")" = "$(j "$apiPod | .$field" "$f")" ] || fail "$mode: $field is not the API's"
      done
      [ "$(q "$convJob | .spec.backoffLimit" "$f")" = 0 ] || fail "$mode: backoffLimit is not 0"
      [ "$(q "$convJob | .spec.template.spec.restartPolicy" "$f")" = Never ] || fail "$mode: restartPolicy is not Never"
      [ "$(q "$convJob | .spec.suspend" "$f")" = false ] || fail "$mode: suspended in an enabled release"
      [ "$(q "$convJob | .spec | has(\"ttlSecondsAfterFinished\")" "$f")" = false ] || fail "$mode: a TTL could rerun a declared Job"

      # Its NetworkPolicy admits exactly the Job's Pods to PostgreSQL.
      [ "$(q "$convPolicy | .spec.podSelector.matchLabels.app" "$f")" = pin-collector-postgres ] || fail "$mode: policy does not select PostgreSQL"
      [ "$(q "$convPolicy | .spec.ingress[0].from[0].podSelector.matchLabels.app" "$f")" = pin-collector-stardust-conversion ] \
        || fail "$mode: policy does not admit the conversion Pods"
    done
    [ "$(echo $names | tr ' ' '\n' | sort -u | wc -l)" = 3 ] || fail "the three modes do not render three distinct Jobs:$names"

    # Flipping stardust or plus does not rename (and so rerun) the conversion either.
    [ "$(q "$convJob | .metadata.name" "$dryRunStarOn")" = "$(q "$convJob | .metadata.name" "$dryRun")" ] \
      || fail "stardust or plus renames the conversion Job"
    # Without collectorPages the API mounts no link key, and neither does the Job.
    [ "$(j "$convJob | .spec.template.spec.volumes" "$dryRunPagesOff")" = "$(j "$apiPod | .volumes" "$dryRunPagesOff")" ] \
      || fail "conversion secret items differ from the API's with collectorPages off"

    touch "$out"
  ''
