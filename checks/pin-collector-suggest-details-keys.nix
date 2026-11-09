{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Suggest details' outside lookups (PinCollector Step 3) need two keys, SerpApi's and the
# reader's. The release gate suggestDetailsKeys declares them in sops, adds them to the
# pin-collector-runtime Secret, mounts them into the API and points the API's two _FILE
# settings at them. Off (or absent) it must change nothing at all, and on it must render
# exactly that. Turning outside lookups on is the admin's separate switch in the app.
let
  # releaseImport needs suggestDetailsKeys (the release contract refuses it without), so it is
  # left out too: the states below render suggestDetailsKeys absent, off and on from one start.
  pinned = builtins.removeAttrs (import ../hosts/nixos/minas-tirith/pin-collector-release.nix) [
    "suggestDetailsKeys"
    "stardustConversion"
    "releaseImport"
  ];
  render =
    release:
    (import ../hosts/nixos/pelargir/manifest-renderers.nix {
      inherit lib pkgs;
      pinCollectorReleaseSource = release;
    }).pinCollectorManifest;
  absent = render pinned;
  off = render (pinned // { suggestDetailsKeys = false; });
  on = render (pinned // { suggestDetailsKeys = true; });
  onPagesOff = render (
    pinned
    // {
      suggestDetailsKeys = true;
      collectorPages = false;
    }
  );
  dryRunOn = render (
    pinned
    // {
      suggestDetailsKeys = true;
      stardustConversion = "dry-run";
    }
  );

  # The host as pinned: its sops declarations and its applier follow the release file.
  release = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
  gate = release.suggestDetailsKeys or false;
  pelargir = nixosConfigurations.pelargir.config;
  script = pelargir.systemd.services.k3s-apply-secrets.script;
  declared = lib.filter (
    name:
    builtins.elem name [
      "pin_collector_serpapi_api_key"
      "pin_collector_detail_reader_api_key"
    ]
  ) (lib.attrNames pelargir.sops.secrets);
  applierSource = builtins.readFile ../hosts/nixos/pelargir/secrets/k3s-apply-secrets.nix;
  problems =
    lib.optional (
      gate && builtins.length declared != 2
    ) "suggestDetailsKeys is set but pelargir doesn't declare both keys"
    ++ lib.optional (
      !gate && declared != [ ]
    ) "Suggest details keys are declared before suggestDetailsKeys"
    ++ lib.optional (
      gate != lib.hasInfix "--from-file=serpapi-api-key=" script
    ) "the applier's SerpApi key file doesn't follow suggestDetailsKeys"
    ++ lib.optional (
      gate != lib.hasInfix "--from-file=detail-reader-api-key=" script
    ) "the applier's reader key file doesn't follow suggestDetailsKeys"
    ++ lib.optional (
      !lib.hasInfix ''"serpapi_api_key"'' (builtins.readFile ../hosts/nixos/pelargir/secrets/sops.nix)
    ) "sops.nix doesn't name serpapi_api_key"
    ++ lib.optional (
      !lib.hasInfix "--from-file=detail-reader-api-key=" applierSource
    ) "the applier has no reader key file to add";
in
if problems != [ ] then
  throw "PinCollector Suggest details keys: ${lib.concatStringsSep "; " problems}"
else
  pkgs.runCommand "pin-collector-suggest-details-keys"
    {
      nativeBuildInputs = [ pkgs.yq-go ];
      inherit
        absent
        off
        on
        onPagesOff
        dryRunOn
        ;
    }
    ''
      set -euo pipefail
      fail() { echo "pin-collector-suggest-details-keys: $*" >&2; exit 1; }
      q() { yq -N "$1" "$2"; }
      j() { yq -N -o=json -I=0 "$1" "$2"; }

      for f in "$absent" "$off" "$on" "$onPagesOff" "$dryRunOn"; do
        yq -N 'true' "$f" > /dev/null || fail "$f does not parse as YAML"
      done

      # Off or absent: byte for byte the same manifest, so merging this restarts nothing.
      cmp -s "$absent" "$off" || fail "suggestDetailsKeys = false changes the manifest"

      config='select(.kind == "ConfigMap" and .metadata.name == "pin-collector-api-config") | .data'
      apiPod='select(.kind == "Deployment" and .metadata.name == "api") | .spec.template.spec'
      items="$apiPod | .volumes[] | select(.name == \"runtime-secret\") | .secret.items[]"
      mount="$apiPod | .containers[] | select(.name == \"api\") | .volumeMounts[] | select(.name == \"runtime-secret\") | .mountPath"
      job='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-migrate") | .metadata.name'

      for var in PIN_COLLECTOR_SERPAPI_API_KEY_FILE PIN_COLLECTOR_DETAIL_READER_API_KEY_FILE; do
        [ "$(q "$config | has(\"$var\")" "$off")" = false ] || fail "$var set with suggestDetailsKeys off"
      done
      [ -z "$(q "$items | select(.key == \"serpapi-api-key\") | .path" "$off")" ] || fail "SerpApi key mounted with the gate off"

      # On: each configured file is exactly a mounted item, alongside the link key, which
      # stays where it was; and with collectorPages off the items still render.
      for f in "$on" "$onPagesOff"; do
        for pair in PIN_COLLECTOR_SERPAPI_API_KEY_FILE:serpapi-api-key PIN_COLLECTOR_DETAIL_READER_API_KEY_FILE:detail-reader-api-key; do
          var=''${pair%%:*}; key=''${pair#*:}
          path=$(q "$items | select(.key == \"$key\") | .path" "$f")
          [ "$path" = "$key" ] || fail "$key is not mounted ($f)"
          [ "$(q "$items | select(.key == \"$key\") | .mode" "$f")" = "$(q "$items | select(.key == \"database-url\") | .mode" "$f")" ] \
            || fail "$key's mode is not the other items' ($f)"
          [ "$(q "$config | .$var" "$f")" = "$(q "$mount" "$f")/$path" ] || fail "$var is not the mounted $key ($f)"
        done
      done
      [ "$(q "$items | select(.key == \"collector-link-key\") | .path" "$on")" = collector-link-key ] || fail "the link key item went missing"
      [ -z "$(q "$items | select(.key == \"collector-link-key\") | .path" "$onPagesOff")" ] || fail "the link key is mounted with collectorPages off"

      # The migration Job keeps its name (it isn't rerun), and the conversion Job carries the
      # API's secret items exactly.
      [ "$(q "$job" "$on")" = "$(q "$job" "$absent")" ] || fail "suggestDetailsKeys renames the migration Job"
      conv='select(.kind == "Job" and .spec.template.metadata.labels.app == "pin-collector-stardust-conversion") | .spec.template.spec'
      [ "$(j "$conv | .volumes" "$dryRunOn")" = "$(j "$apiPod | .volumes" "$dryRunOn")" ] \
        || fail "the conversion Job's secret items differ from the API's"

      touch "$out"
    ''
