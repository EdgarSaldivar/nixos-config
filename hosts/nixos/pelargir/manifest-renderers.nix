{
  lib,
  pkgs,
  # Only checks/pin-collector-feature-gates.nix passes this, to render a release that
  # has not been pinned yet. Every host renders the pinned release file.
  pinCollectorReleaseSource ? import ../minas-tirith/pin-collector-release.nix,
}:
let
  pinCollectorReleaseContract = import ../minas-tirith/pin-collector-release-contract.nix {
    inherit lib;
  };
  pinCollectorRelease = pinCollectorReleaseContract.assertValid pinCollectorReleaseSource;
  # Wave 4 collector pages (see collectorPagesApiConfig below).
  pinCollectorCollectorPages = pinCollectorRelease.collectorPages or false;
  # Wave 2 stardust and Plus (see collectorPagesApiConfig and apiFeatureGatesAnnotation).
  pinCollectorStardust = pinCollectorRelease.stardust or false;
  pinCollectorPlus = pinCollectorRelease.plus or false;
  # The one-time stardust conversion: null, "dry-run", "apply" or "refresh".
  pinCollectorStardustConversion = pinCollectorRelease.stardustConversion or null;

  # Keep the manifest permanently owned after its first activation. k3s does not
  # prune a removed auto-deploy file, so `staged = false` must render an inert
  # object set rather than dropping the file and leaving the previous release live.
  pinCollectorInertImage = "registry.invalid/pin-collector/inert@sha256:${lib.concatStrings (lib.replicate 64 "0")}";
  pinCollectorApiImage =
    if pinCollectorRelease.staged then pinCollectorRelease.apiImage else pinCollectorInertImage;
  pinCollectorModelImage =
    if pinCollectorRelease.staged then pinCollectorRelease.modelImage else pinCollectorInertImage;
  pinCollectorApiDigest =
    if pinCollectorRelease.staged then
      lib.removePrefix "ghcr.io/edgarsaldivar/pin-collector-api@sha256:" pinCollectorApiImage
    else
      lib.concatStrings (lib.replicate 64 "0");
  pinCollectorGitRevision =
    if pinCollectorRelease.staged then
      pinCollectorRelease.gitRevision
    else
      lib.concatStrings (lib.replicate 40 "0");
  # The migration Job's Pod template is immutable once applied, and a completed Job never
  # reruns. Its name therefore carries a hash of its own template text, of every ConfigMap
  # in the manifest (the config it reads, such as the S3 endpoint), and the API digest: any
  # edit renders a new Job instead of an apply k3s would reject or a stale completion.
  # Migrations and bootstraps are idempotent, so an extra run is harmless. The push
  # sender's ConfigMap is not config the migration reads either, so it is left out the
  # same way: adding the APNs settings must not rerun the migration.
  pinCollectorTemplate = builtins.readFile ../minas-tirith/manifests/pin-collector.yaml.in;
  pinCollectorMigrationJobDocs = lib.filter (lib.hasInfix "name: @migrationJobName@") (
    lib.splitString "\n---\n" pinCollectorTemplate
  );
  pinCollectorMigrationJobHash =
    assert lib.assertMsg (
      builtins.length pinCollectorMigrationJobDocs == 1
    ) "pin-collector.yaml.in must contain exactly one migration Job";
    builtins.substring 0 8 (
      builtins.hashString "sha256" (
        builtins.head pinCollectorMigrationJobDocs
        + lib.concatStrings (
          # The backup's scripts are not config the migration reads; leaving them out keeps
          # a backup-only change from renaming (and so rerunning) the migration Job.
          lib.filter (
            doc:
            lib.hasInfix "\nkind: ConfigMap\n" doc
            && !(lib.hasPrefix "# backup-scripts\n" doc)
            && !(lib.hasPrefix "# notifications-config\n" doc)
          ) (lib.splitString "\n---\n" pinCollectorTemplate)
        )
      )
    );
  # Scripts shipped in ConfigMaps live as real files; indent them under their `|` keys.
  pinCollectorIndent =
    path:
    lib.concatMapStringsSep "\n" (line: if line == "" then "" else "    ${line}") (
      lib.splitString "\n" (lib.removeSuffix "\n" (builtins.readFile path))
    );
  # Garage lives in its own auto-deploy file (minas-pin-collector-garage.yaml): a render of
  # the app manifest can never drop the store. Moving it there relied on its durable
  # objects' objectset.rio.cattle.io/prune=false label (see the template's header).
  pinCollectorGarageTemplate = builtins.readFile ../minas-tirith/manifests/pin-collector-garage.yaml.in;
  pinCollectorGarageBootstrapScript = ../minas-tirith/manifests/garage_bootstrap.py;
  # Same immutability rule for the Garage bootstrap Job: its name follows its own spec,
  # its script and the image it runs. Key rotation is a manual, ordered procedure
  # (docs/runbooks/minas-tirith/pin-collector-garage.md), not a side effect of a deploy.
  pinCollectorGarageBootstrapHash = builtins.substring 0 8 (
    builtins.hashString "sha256" (
      # Trailing newlines are dropped so a chunk hashes the same whether or not it is the
      # last document of its file (it was not, before Garage moved to its own file).
      lib.concatStrings (
        map (lib.removeSuffix "\n") (
          lib.filter (lib.hasPrefix "# garage-bootstrap\n") (
            lib.splitString "\n---\n" pinCollectorGarageTemplate
          )
        )
      )
      + builtins.readFile pinCollectorGarageBootstrapScript
    )
  );
  # Scripts' ConfigMaps are named by their content, so a Job (or one stage of one) can
  # never run a script revision other than the one it was rendered with.
  # The nightly backup's scripts, one ConfigMap key per file, named by the ConfigMap's
  # template text plus every file's name and content.
  pinCollectorBackupScripts = [
    "backup_lock.py"
    "backup_guard.sh"
    "backup_remote.sh"
    "backup_meta_1.sh"
    "backup_copy_1.sh"
    "backup_pg_dump.sh"
    "backup_meta_copy_2.sh"
    "backup_check.py"
    "backup_restic.sh"
    "backup_mirror_prune.sh"
    # Not run by the CronJob: shipped here so a restore pod can mount it
    # (docs/runbooks/minas-tirith/pin-collector-garage.md, "Restore").
    "backup_restore_objects.py"
  ];
  pinCollectorBackupScriptPath = name: ../minas-tirith/manifests + "/${name}";
  pinCollectorBackupScriptsData = lib.concatMapStringsSep "\n" (
    name: "  ${name}: |\n${pinCollectorIndent (pinCollectorBackupScriptPath name)}"
  ) pinCollectorBackupScripts;
  pinCollectorBackupScriptsHash = builtins.substring 0 8 (
    builtins.hashString "sha256" (
      lib.concatStrings (
        lib.filter (lib.hasPrefix "# backup-scripts\n") (lib.splitString "\n---\n" pinCollectorTemplate)
      )
      + lib.concatMapStrings (
        name: "${name}\n${builtins.readFile (pinCollectorBackupScriptPath name)}"
      ) pinCollectorBackupScripts
    )
  );
  pinCollectorBaseManifest = pkgs.replaceVars ../minas-tirith/manifests/pin-collector.yaml.in {
    apiImage = pinCollectorApiImage;
    modelImage = pinCollectorModelImage;
    gitRevision = pinCollectorGitRevision;
    statefulReplicas = if pinCollectorRelease.staged then "1" else "0";
    # apiMaintenance holds the API at zero across restarts and re-applies (storage cutover).
    apiReplicas =
      if pinCollectorRelease.enabled && !(pinCollectorRelease.apiMaintenance or false) then "1" else "0";
    modelReplicas = if pinCollectorRelease.enabled then "1" else "0";
    migrationSuspended = if pinCollectorRelease.enabled then "false" else "true";
    migrationJobName = "pin-collector-migrate-${
      builtins.substring 0 12 pinCollectorApiDigest
    }-${pinCollectorMigrationJobHash}";
    backupScripts = pinCollectorBackupScriptsData;
    backupScriptsConfigName = "pin-collector-backup-scripts-${pinCollectorBackupScriptsHash}";
    # Nothing to back up until the release is enabled (PostgreSQL and Garage run then).
    backupSuspended = if pinCollectorRelease.enabled then "false" else "true";
    # Wave 3's push sender (`python -m app.maintenance.send_notifications`) exists only in
    # images from PinCollector 9e894cd on; an older image would fail every run. The
    # release that first pins such an image sets notificationSender = true.
    notificationsSuspended =
      if pinCollectorRelease.enabled && (pinCollectorRelease.notificationSender or false) then
        "false"
      else
        "true";
    # Wave 4's collector pages: the link key file and the "Get the app" target. Rendered
    # only once the release sets collectorPages = true, so merging this config changes
    # nothing in the API's Pod template (an older image would ignore both, but the new
    # volume item alone would restart it).
    # Each placeholder sits alone on an indented line; continuation lines carry the same
    # two-space ConfigMap indent.
    # The slot also carries Wave 2's stardust and Plus switches (the app's stardust_enabled
    # and plus_enabled, default off; plus_beta_open keeps the app's default). It is the API
    # ConfigMap's only slot, and that ConfigMap's template text feeds the migration Job's
    # name: a second placeholder, or a reworded comment above this one, would rename and
    # rerun the migration, and with the gates off would no longer render byte-identically.
    collectorPagesApiConfig = lib.concatStringsSep "\n  " (
      lib.optionals pinCollectorCollectorPages [
        "PIN_COLLECTOR_COLLECTOR_LINK_KEY_FILE: /run/secrets/pin-collector/collector-link-key"
        "# Until a TestFlight invite exists, \"Get the app\" goes to the support page."
        "PIN_COLLECTOR_GET_APP_URL: https://pin.saldivar.io/support"
      ]
      ++ lib.optional (
        pinCollectorStardust || pinCollectorPlus
      ) "# Set only when the release sets stardust or plus (manifest-renderers.nix)."
      ++ lib.optional pinCollectorStardust "PIN_COLLECTOR_STARDUST_ENABLED: \"true\""
      ++ lib.optional pinCollectorPlus "PIN_COLLECTOR_PLUS_ENABLED: \"true\""
    );
    collectorLinkKeyItem = pinCollectorCollectorLinkKeyItem;
    # A ConfigMap edit does not restart the Pods reading it, and unlike collectorPages the
    # stardust and plus switches change nothing else in the API's Pod template. This
    # annotation names the switches that are on, so flipping one rolls the API onto it.
    # It is appended to the expected-image-revision line with its own newline and indent,
    # so with both off the line, and the manifest, render byte-identically.
    apiFeatureGatesAnnotation = lib.optionalString (pinCollectorStardust || pinCollectorPlus) (
      "\n        pin-collector.saldivar.io/feature-gates: "
      + lib.concatStringsSep "," (
        lib.optional pinCollectorStardust "stardust" ++ lib.optional pinCollectorPlus "plus"
      )
    );
  };
  pinCollectorCollectorLinkKeyItem = lib.optionalString pinCollectorCollectorPages "- { key: collector-link-key, path: collector-link-key, mode: 0440 }";

  # The stardust conversion Job (pin-collector-stardust-conversion.yaml.in), appended to the
  # app manifest only while the release names a mode; without one the manifest is the base
  # render above, unchanged. It runs with the API's ConfigMap and secret items, so it
  # carries the API's collector-link-key item too.
  pinCollectorConversionArgs = {
    "dry-run" = "[python, -m, app.maintenance.convert_stardust]";
    apply = "[python, -m, app.maintenance.convert_stardust, --apply]";
    refresh = "[python, -m, app.maintenance.convert_stardust, --refresh]";
  };
  # Every placeholder but the Job's name and suspend flag, substituted in Nix so the
  # rendered Job can be hashed for its own name.
  pinCollectorConversionSpec =
    lib.replaceStrings
      [ "@apiImage@" "@gitRevision@" "@conversionArgs@" "@collectorLinkKeyItem@" ]
      [
        pinCollectorApiImage
        pinCollectorGitRevision
        pinCollectorConversionArgs.${pinCollectorStardustConversion}
        pinCollectorCollectorLinkKeyItem
      ]
      (builtins.readFile ../minas-tirith/manifests/pin-collector-stardust-conversion.yaml.in);
  pinCollectorConversionJobDocs = lib.filter (lib.hasInfix "name: @conversionJobName@") (
    lib.splitString "\n---\n" pinCollectorConversionSpec
  );
  # Same immutability rule as the migration Job: a completed Job never reruns and its Pod
  # template cannot change, so the name follows the mode, the API digest and the Job's
  # rendered spec. Re-rendering the same release gives the same name, and so no second
  # run; the stardust and plus switches are not in the spec, so flipping them renames
  # nothing. A new name, and so a new run, comes from a new mode, a new API image, an edit
  # to the Job's template, or a collectorPages flip (its link-key item is in the spec).
  # Another run is harmless: a dry run writes nothing, --apply skips accounts it already
  # converted, and --refresh is idempotent by subject key.
  pinCollectorConversionJobName =
    assert lib.assertMsg (
      builtins.length pinCollectorConversionJobDocs == 1
    ) "pin-collector-stardust-conversion.yaml.in must contain exactly one conversion Job";
    "pin-collector-stardust-${pinCollectorStardustConversion}-${
      builtins.substring 0 12 pinCollectorApiDigest
    }-${
      builtins.substring 0 8 (builtins.hashString "sha256" (builtins.head pinCollectorConversionJobDocs))
    }";
  pinCollectorConversionManifest =
    let
      rendered =
        lib.replaceStrings
          [ "@conversionJobName@" "@conversionSuspended@" ]
          [
            pinCollectorConversionJobName
            # The contract already requires an enabled release; held like the migration Job.
            (if pinCollectorRelease.enabled then "false" else "true")
          ]
          pinCollectorConversionSpec;
    in
    assert lib.assertMsg (
      builtins.length (builtins.split "@[A-Za-z_][A-Za-z0-9_]*@" rendered) == 1
    ) "pin-collector-stardust-conversion.yaml.in has a placeholder the renderer does not fill";
    pkgs.writeText "pin-collector-stardust-conversion.yaml" rendered;
  pinCollectorManifest =
    if pinCollectorStardustConversion == null then
      pinCollectorBaseManifest
    else
      pkgs.runCommand "pin-collector.yaml" { } ''
        { cat ${pinCollectorBaseManifest}; echo ---; cat ${pinCollectorConversionManifest}; } > $out
      '';
  pinCollectorGarageManifest =
    pkgs.replaceVars ../minas-tirith/manifests/pin-collector-garage.yaml.in
      {
        apiImage = pinCollectorApiImage;
        statefulReplicas = if pinCollectorRelease.staged then "1" else "0";
        migrationSuspended = if pinCollectorRelease.enabled then "false" else "true";
        garageBootstrapJobName = "garage-bootstrap-${
          builtins.substring 0 12 pinCollectorApiDigest
        }-${pinCollectorGarageBootstrapHash}";
        garageBootstrapScript = pinCollectorIndent pinCollectorGarageBootstrapScript;
        garageBootstrapConfigName = "garage-bootstrap-${pinCollectorGarageBootstrapHash}";
      };

  # Dungeon Scriber follows the same permanently-owned, inert-until-staged shape. The
  # renderer is a function of the release so its contract check can render the staged
  # and exposed shapes too, through this exact code.
  dungeonScriberRelease =
    (import ../minas-tirith/dungeon-scriber-release-contract.nix { inherit lib; }).assertValid
      (import ../minas-tirith/dungeon-scriber-release.nix);
  dungeonScriberManifest = import ./dungeon-scriber-manifest.nix {
    inherit lib pkgs;
  } dungeonScriberRelease;

  # coredns-custom — resolve minas' public hostnames to its LAN address.
  #
  # D13: a Pod resolving e.g. tautulli.saldivar.io gets the PUBLIC address, so its
  # request leaves the network and must return through NAT loopback. This maps those
  # names to 10.0.1.6 for cluster DNS, exactly as networking.hosts does for the host.
  #
  # The list is imported from minas-tirith/traefik-hostnames.nix — the SAME file
  # networking.hosts uses — so the two cannot drift.
  #
  # Delivered as a `.server` file, not `.override`, deliberately. k3s' Corefile carries
  # `import /etc/coredns/custom/*.override` INSIDE the .:53 block and
  # `import /etc/coredns/custom/*.server` at top level. The main block already uses the
  # `hosts` plugin for NodeHosts, and a second `hosts` in the same block is a config
  # error — so this defines its own server block for the zone instead.
  #
  # `fallthrough` matters: names NOT in the list (notably ha-pelargir.saldivar.io,
  # which lives on pelargir) fall through to `forward` and resolve normally.
  #
  # coredns-custom is the ONE CoreDNS customisation that is k3s-supported and
  # upgrade-safe. Both replicas mount it (packaged and coredns-ha), optional: true.
  minasHostnames = import ../minas-tirith/traefik-hostnames.nix;
  corednsCustom = pkgs.writeText "coredns-custom.yaml" ''
    # GENERATED by pelargir/manifests.nix from minas-tirith/traefik-hostnames.nix.
    apiVersion: v1
    kind: ConfigMap
    metadata:
      name: coredns-custom
      namespace: kube-system
    data:
      saldivar.server: |
        saldivar.io:53 {
            errors
            cache 30
            hosts {
    ${lib.concatMapStringsSep "\n" (h: "            10.0.1.6 ${h}") minasHostnames}
                fallthrough
            }
            forward . /etc/resolv.conf
        }
  '';

  minasTraefik = (import ./minas-traefik-manifest.nix { inherit lib pkgs; }).minasTraefik;
in
{
  inherit
    corednsCustom
    dungeonScriberManifest
    dungeonScriberRelease
    minasTraefik
    pinCollectorGarageManifest
    pinCollectorManifest
    pinCollectorRelease
    ;
}
