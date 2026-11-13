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
else
  pkgs.runCommand "bigcapital-contract-ok" { } "touch $out"
