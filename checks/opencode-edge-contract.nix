{
  lib,
  pkgs,
  ...
}:

# opencode.saldivar.io publishes a shell-capable coding-agent server on
# dol-amroth. Each rule below is a way to leave it one step from the open
# internet while every page still loads.
let
  catalog = import ../hosts/nixos/minas-tirith/traefik-routes/catalog.nix {
    pinCollectorRelease = import ../hosts/nixos/minas-tirith/pin-collector-release.nix;
    dungeonScriberRelease = import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix;
  };
  route = catalog.routes.opencode or null;
  hostnames = import ../hosts/nixos/minas-tirith/traefik-hostnames.nix;
  authentikText = builtins.readFile ../hosts/nixos/minas-tirith/manifests/authentik.yaml;
  backendText = builtins.readFile ../hosts/nixos/minas-tirith/manifests/tailnet-services.yaml;
  deliveryText = builtins.readFile ../hosts/nixos/pelargir/manifest-catalog/minas-tirith.nix;

  # The opencode provider block, from its id to the next blueprint entry.
  providerBlock =
    let
      parts = lib.splitString "id: provider-opencode" authentikText;
    in
    if lib.length parts < 2 then "" else lib.head (lib.splitString "- model:" (lib.elemAt parts 1));
in
if route == null || route.hosts != [ "opencode.saldivar.io" ] then
  throw "opencode-edge: the opencode route must exist and answer only on opencode.saldivar.io"
else if route.namespace != "tailnet" || route.port != 4096 || route ? scheme then
  throw "opencode-edge: the route must target the tailnet/opencode Service on port 4096"

# 1. The passkey gate, and a closed (not open) rollback.
else if !(lib.elem "opencode" catalog.authentikRollout.protectedRoutes) then
  throw "opencode-edge: opencode must stay in authentikRollout.protectedRoutes; without it the agent server is public behind only its own password"
else if !(lib.elem "opencode" catalog.legacyBasicAuthFallbackRoutes) then
  throw "opencode-edge: opencode must keep BasicAuth as its rollback fallback, so detaching Authentik never leaves it open"

# 2. Authentik must know the host, serve it from the outpost, and leave
#    opencode's own Authorization header alone.
else if
  !(lib.hasInfix "external_host: https://opencode.saldivar.io" providerBlock)
  || !(lib.hasInfix "mode: forward_single" providerBlock)
then
  throw "opencode-edge: Authentik needs a forward_single provider for https://opencode.saldivar.io"
else if !(lib.hasInfix "intercept_header_auth: false" providerBlock) then
  throw "opencode-edge: the opencode provider must not intercept the Authorization header; opencode's web app authenticates its API with it"
else if !(lib.hasInfix "- !KeyOf provider-opencode" authentikText) then
  throw "opencode-edge: the embedded outpost must serve provider-opencode, or ForwardAuth fails for the host"
else if !(lib.hasInfix "target: !KeyOf app-opencode" authentikText) then
  throw "opencode-edge: the opencode application must stay bound to the authentik Admins group"

# 3. The backend: a selector-less Service with a manual slice, delivered.
else if
  !(lib.hasInfix "name: opencode\n  namespace: tailnet" backendText)
  || !(lib.hasInfix "kubernetes.io/service-name: opencode" backendText)
  || !(lib.hasInfix "port: 4096" backendText)
then
  throw "opencode-edge: tailnet-services.yaml must declare Service tailnet/opencode:4096 and its EndpointSlice"
else if lib.hasInfix "selector:" backendText then
  throw "opencode-edge: tailnet Services must stay selector-less; a selector makes Kubernetes overwrite the manual slice"
else if !(lib.hasInfix "name = \"minas-tailnet-services.yaml\"" deliveryText) then
  throw "opencode-edge: pelargir must deliver tailnet-services.yaml as minas-tailnet-services.yaml"

# 4. Pods and the host resolve the name to minas, like every other route.
else if !(lib.elem "opencode.saldivar.io" hostnames) then
  throw "opencode-edge: opencode.saldivar.io must be in traefik-hostnames.nix"
else
  pkgs.runCommand "opencode-edge-contract-ok" { } "touch $out"
