{
  lib,
  pkgs,
  ...
}:

# The PinCollector release gates (notificationSender, collectorPages) render config that
# only a newer image can use, so the pinned release usually has them off and nothing else
# ever renders them on. An indentation slip in a gated line would then surface first at
# the release deploy, as an AddOn that never converges. Render every gate state here and
# assert the objects they are meant to produce.
let
  pinned = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
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
in
pkgs.runCommand "pin-collector-feature-gates"
  {
    nativeBuildInputs = [ pkgs.yq-go ];
    inherit off on onDisabled;
  }
  ''
    set -euo pipefail
    fail() { echo "pin-collector-feature-gates: $*" >&2; exit 1; }
    q() { yq -N "$1" "$2"; }

    for f in "$off" "$on" "$onDisabled"; do
      yq -N 'true' "$f" > /dev/null || fail "$f does not parse as YAML"
    done

    config='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-api-config") | .data'
    items='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec.volumes[] | select(.name == "runtime-secret") | .secret.items[] | select(.key == "collector-link-key") | .path'
    mount='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec.containers[] | select(.name == "api") | .volumeMounts[] | select(.name == "runtime-secret") | .mountPath'
    suspend='select(.kind == "CronJob" and .metadata.name == "send-notifications") | .spec.suspend'
    job='select(.kind == "Job") | .metadata.name'

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
    [ "$(q "$job" "$off")" = "$(q "$job" "$on")" ] || fail "a gate renames the migration Job"

    touch "$out"
  ''
