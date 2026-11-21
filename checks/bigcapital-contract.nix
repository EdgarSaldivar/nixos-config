{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Bigcapital (accounting.saldivar.io). Each rule is a way for the deployment to look
# healthy while being wrong: open to anyone, unable to serve an attachment, or unable
# to apply its next upgrade.
let
  catalog = import ../hosts/nixos/minas-tirith/traefik-routes/catalog.nix {
    pinCollectorRelease = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
    dungeonScriberRelease = import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix;
  };
  inherit (catalog) routes;
  protected = catalog.authentikRollout.protectedRoutes;
  host = "accounting.saldivar.io";
  hostnames = import ../hosts/nixos/minas-tirith/traefik-hostnames.nix;
  manifest = builtins.readFile ../hosts/nixos/minas-tirith/manifests/bigcapital.yaml;
  authentikText = builtins.readFile ../hosts/nixos/minas-tirith/manifests/authentik.yaml;
  deliveryText = builtins.readFile ../hosts/nixos/pelargir/manifest-catalog/minas-tirith.nix;
  applier = nixosConfigurations.pelargir.config.systemd.services.k3s-apply-secrets.script;
  inherit (import ../hosts/nixos/pelargir/secrets/bigcapital-keys.nix { inherit lib; })
    mailConfigured
    backupHeartbeatConfigured
    ;
  pelargirServices = nixosConfigurations.pelargir.config.systemd.services;

  docs = lib.splitString "\n---\n" manifest;

  # ── hash-named documents ──────────────────────────────────────────────────────
  # A group's name suffix must be the first 8 hex of the sha256 of all its documents,
  # each without a trailing newline, concatenated, with the suffix itself replaced by
  # `@`. Removing the suffix before hashing is what makes the name a fixed point of its
  # own content. bigcapital.yaml's header states the same rule for humans.
  hashNamed =
    group:
    let
      members = map (lib.removeSuffix "\n") (lib.filter (lib.hasPrefix "# hash-named: ${group}\n") docs);
      joined = lib.concatStrings members;
      parts = lib.splitString "name: ${group}-" joined;
      claimed = if lib.length parts < 2 then "" else builtins.substring 0 8 (lib.elemAt parts 1);
      expected = builtins.substring 0 8 (
        builtins.hashString "sha256" (
          builtins.replaceStrings [ "${group}-${claimed}" ] [ "${group}-@" ] joined
        )
      );
    in
    {
      inherit members claimed expected;
      ok = members != [ ] && builtins.match "[0-9a-f]{8}" claimed != null && claimed == expected;
    };
  bootstrap = hashNamed "garage-bootstrap";
  migrate = hashNamed "migrate";
  backupScripts = hashNamed "backup-scripts";
  # The CronJob document, located by its kind and name.
  cronJob = lib.findFirst (
    d: lib.hasInfix "kind: CronJob\nmetadata:\n  name: backup\n  namespace: bigcapital" d
  ) "" docs;

  # The image every Bigcapital server-side process must run: server, migration and
  # bootstrap are one release, so a bump that misses one is a mixed-version deployment.
  serverImages = lib.unique (
    map (l: lib.removePrefix "image: " (lib.trim l)) (
      lib.filter (l: lib.hasInfix "image: bigcapitalhq/server:" l) (lib.splitString "\n" manifest)
    )
  );
  webappImages = lib.unique (
    map (l: lib.removePrefix "image: " (lib.trim l)) (
      lib.filter (l: lib.hasInfix "image: bigcapitalhq/webapp:" l) (lib.splitString "\n" manifest)
    )
  );
  tagOf =
    image:
    lib.head (
      lib.splitString "@" (lib.last (lib.splitString ":" (lib.head (lib.splitString "@" image))))
    );
  pinned = image: builtins.match ".*@sha256:[0-9a-f]{64}" image != null;

  providerBlock =
    let
      parts = lib.splitString "id: provider-bigcapital" authentikText;
    in
    if lib.length parts < 2 then "" else lib.head (lib.splitString "- model:" (lib.elemAt parts 1));

  route = name: routes.${name} or null;
in
# 1. The edge: three disjoint routes on one host.
if
  lib.any (n: route n == null || (route n).hosts != [ host ] || (route n).namespace != "bigcapital") [
    "bigcapital"
    "bigcapital-api"
    "bigcapital-files"
  ]
then
  throw "bigcapital-contract: routes bigcapital, bigcapital-api and bigcapital-files must exist, answer only on ${host}, and target namespace bigcapital"
else if
  (route "bigcapital").serviceName != "webapp"
  || (route "bigcapital-api").serviceName != "server"
  || (route "bigcapital-files").serviceName != "garage"
then
  throw "bigcapital-contract: the routes must target webapp, server and garage respectively"
else if
  (route "bigcapital-api").allowedPathPrefixes or [ ] != [ "/api" ]
  || (route "bigcapital-files").allowedPathPrefixes or [ ] != [ "/bigcapital-files/" ]
  ||
    lib.sort lib.lessThan ((route "bigcapital").excludedPathPrefixes or [ ]) != [
      "/api"
      "/bigcapital-files/"
    ]
then
  throw "bigcapital-contract: the webapp route must exclude exactly what the api and files routes match; every router has priority 1, so overlapping rules tie"
else if !(lib.elem "(\\.\\.|//|%)" ((route "bigcapital-files").rejectedPathPatterns or [ ])) then
  throw "bigcapital-contract: the unauthenticated files route must refuse `..`, `//` and `%` paths; Traefik forwards dot segments unresolved"

# 2. Authentik in front of the app, never in front of the bucket.
else if !(lib.elem "bigcapital" protected && lib.elem "bigcapital-api" protected) then
  throw "bigcapital-contract: bigcapital and bigcapital-api must stay in authentikRollout.protectedRoutes (the owner's decision for financial records)"
else if lib.elem "bigcapital-files" protected then
  throw "bigcapital-contract: bigcapital-files must NOT be Authentik-gated; the server's own S3 calls go through it and carry no Authentik session"
else if
  !(lib.hasInfix "external_host: https://${host}" providerBlock)
  || !(lib.hasInfix "mode: forward_single" providerBlock)
  || !(lib.hasInfix "intercept_header_auth: false" providerBlock)
then
  throw "bigcapital-contract: Authentik needs a forward_single provider for https://${host} that does not intercept the Authorization header (Bigcapital's SPA sends its own Bearer token)"
else if !(lib.hasInfix "- !KeyOf provider-bigcapital" authentikText) then
  throw "bigcapital-contract: the embedded outpost must serve provider-bigcapital, or ForwardAuth fails for the host"
else if
  !(lib.hasInfix "target: !KeyOf app-bigcapital\n          group: !Find [authentik_core.group, [name, authentik Admins]]" authentikText)
  || !(lib.hasInfix "target: !KeyOf app-bigcapital\n          group: !Find [authentik_core.group, [name, Accounting users]]" authentikText)
then
  throw "bigcapital-contract: the Bigcapital application must admit authentik Admins and Accounting users"

# 3. The app's own configuration.
else if !(lib.hasInfix "{ name: SIGNUP_DISABLED, value: \"true\" }" manifest) then
  throw "bigcapital-contract: SIGNUP_DISABLED must be \"true\"; Bigcapital enforces SIGNUP_ALLOWED_EMAILS only when signup is disabled, so \"false\" lets anyone create an organisation"
else if
  !(lib.hasInfix "{ name: S3_ENDPOINT, value: \"https://${host}\" }" manifest)
  || !(lib.hasInfix "{ name: S3_BUCKET, value: bigcapital-files }" manifest)
  || !(lib.hasInfix "{ name: S3_FORCE_PATH_STYLE, value: \"true\" }" manifest)
then
  throw "bigcapital-contract: the server must sign S3 for https://${host}/bigcapital-files/ (path-style): presigned download URLs go to the browser"
else if
  !(
    lib.hasInfix "s3_region = \"garage\"" manifest
    && lib.hasInfix "{ name: S3_REGION, value: garage }" manifest
  )
then
  throw "bigcapital-contract: Garage's s3_region and the server's S3_REGION must both be `garage`; the region is part of every signature"
else if !(lib.elem host hostnames) then
  throw "bigcapital-contract: ${host} must be in traefik-hostnames.nix; the server reaches its S3 endpoint through CoreDNS' LAN answer"
else if lib.length serverImages != 1 || !(pinned (lib.head serverImages)) then
  throw "bigcapital-contract: server, migration and Garage bootstrap must run one digest-pinned bigcapitalhq/server image; found ${builtins.toJSON serverImages}"
else if
  lib.length webappImages != 1
  || !(pinned (lib.head webappImages))
  || tagOf (lib.head webappImages) != tagOf (lib.head serverImages)
then
  throw "bigcapital-contract: the webapp must be the digest-pinned release matching the server (${tagOf (lib.head serverImages)})"

# 4. Hash-named Jobs: a stale name is an immutable-template apply failure later.
else if !bootstrap.ok then
  throw "bigcapital-contract: the garage-bootstrap ConfigMap and Job must be named garage-bootstrap-${bootstrap.expected} (found `${bootstrap.claimed}`); their content changed"
else if !(lib.hasInfix "configMap: { name: garage-bootstrap-${bootstrap.claimed} }" manifest) then
  throw "bigcapital-contract: the garage-bootstrap Job must mount the ConfigMap of the same name"
else if !migrate.ok then
  throw "bigcapital-contract: the migration Job must be named migrate-${migrate.expected} (found `${migrate.claimed}`); its content or image changed"

# 5. Delivery: pelargir ships the manifest and the Secret it reads.
else if
  !(lib.hasInfix "name = \"minas-bigcapital.yaml\";\n    path = ../../minas-tirith/manifests/bigcapital.yaml;" deliveryText)
then
  throw "bigcapital-contract: pelargir must deliver bigcapital.yaml as the frozen AddOn minas-bigcapital.yaml"
else if
  !(lib.hasInfix "create secret generic bigcapital-runtime" applier)
  || !(lib.hasInfix "--from-file=signup-allowed-emails=" applier)
  || !(lib.hasInfix "--from-file=garage-backup-secret=" applier)
then
  throw "bigcapital-contract: k3s-apply-secrets must build bigcapital-runtime from the sops files"

# 6. Mail: optional until the owner adds the Resend key, and then actually delivered.
else if
  !(lib.hasInfix "secretKeyRef: { name: bigcapital-runtime, key: mail-password, optional: true }" manifest)
then
  throw "bigcapital-contract: MAIL_PASSWORD must stay an optional secretKeyRef; a required reference to a key the Secret lacks stops the server from starting"
else if lib.hasInfix "--from-file=mail-password=" applier != mailConfigured then
  throw "bigcapital-contract: k3s-apply-secrets must include mail-password exactly when secrets/bigcapital.yaml has mail_password (has it: ${lib.boolToString mailConfigured})"
else if
  !(lib.hasInfix "{ name: MAIL_HOST, value: smtp.resend.com }" manifest)
  || !(lib.hasInfix "{ name: MAIL_PORT, value: \"465\" }" manifest)
  || !(lib.hasInfix "{ name: MAIL_SECURE, value: \"true\" }" manifest)
then
  throw "bigcapital-contract: mail must go to smtp.resend.com:465 with implicit TLS, the only port server-egress-smtp opens"
else if
  !(lib.hasInfix "name: server-egress-smtp" manifest)
  || !(lib.hasInfix "              - 10.0.0.0/8\n              - 172.16.0.0/12\n              - 192.168.0.0/16\n              - 100.64.0.0/10" manifest)
then
  throw "bigcapital-contract: the SMTP egress rule must exclude private, CGNAT and tailnet ranges; it opens 465 to the internet, not to the LAN"
# 7. Backup: off-site, read-only, and reaching the directory pelargir prepares.
else if !backupScripts.ok then
  throw "bigcapital-contract: the backup scripts ConfigMap must be named backup-scripts-${backupScripts.expected} (found `${backupScripts.claimed}`); its content changed"
else if cronJob == "" then
  throw "bigcapital-contract: CronJob bigcapital/backup must exist"
else if !(lib.hasInfix "configMap: { name: backup-scripts-${backupScripts.claimed} }" cronJob) then
  throw "bigcapital-contract: the backup CronJob must mount the current backup-scripts ConfigMap"
else if !(lib.hasInfix "nodeSelector:\n            kubernetes.io/hostname: pelargir" cronJob) then
  throw "bigcapital-contract: the backup must run on pelargir, the other house; a backup on minas does not survive minas"
else if lib.hasInfix "garage-app-" cronJob then
  throw "bigcapital-contract: the backup must use the read-only Garage key, never the server's read+write one"
else if
  !(pelargirServices ? bigcapital-backup-target)
  || !(lib.hasInfix "mkdir -p /var/lib/bigcapital-backup\n" pelargirServices.bigcapital-backup-target.script)
  || !(lib.hasInfix "local:\n    path: /var/lib/bigcapital-backup\n" manifest)
then
  throw "bigcapital-contract: the backup PersistentVolume must be the directory pelargir's bigcapital-backup-target prepares"
else if
  !(lib.hasInfix "values: [bigcapital-server, bigcapital-migrate, bigcapital-backup]" manifest)
then
  throw "bigcapital-contract: MariaDB's ingress policy must admit the backup pod"
else if !(lib.hasInfix "--from-file=backup-restic-password=" applier) then
  throw "bigcapital-contract: k3s-apply-secrets must deliver the restic repository password"
else if
  !(lib.hasInfix "secretName: bigcapital-runtime\n                optional: true" cronJob)
  || lib.hasInfix "--from-file=backup-healthchecks-url=" applier != backupHeartbeatConfigured
then
  throw "bigcapital-contract: the Healthchecks URL must be an optional volume, and delivered exactly when secrets/bigcapital-backup.yaml has healthchecks_url (has it: ${lib.boolToString backupHeartbeatConfigured})"

# 8. The backup scripts must pass ShellCheck: they run nowhere but pelargir at 03:45.
else
  pkgs.runCommand "bigcapital-contract-ok"
    {
      nativeBuildInputs = [
        pkgs.yq-go
        pkgs.shellcheck
      ];
      manifest = ../hosts/nixos/minas-tirith/manifests/bigcapital.yaml;
    }
    ''
      set -euo pipefail
      mkdir scripts
      sel='select(.kind == "ConfigMap" and (.metadata.name | test("^backup-scripts-")))'
      names=$(yq "$sel | .data | keys | .[]" "$manifest")
      # Refuse to pass vacuously: the pipeline has eight scripts.
      [ "$(printf '%s\n' "$names" | grep -c '\.sh$')" -eq 8 ] || { echo "expected 8 backup scripts, found: $names" >&2; exit 1; }
      for n in $names; do
        yq "$sel | .data[\"$n\"]" "$manifest" > "scripts/$n"
      done
      shellcheck scripts/*.sh
      touch $out
    ''
