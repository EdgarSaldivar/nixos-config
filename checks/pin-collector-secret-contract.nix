{
  lib,
  pkgs,
  nixosConfigurations,
  darwinConfigurations,
  ...
}:

let
  manifestSource = builtins.readFile ../hosts/nixos/minas-tirith/manifests/pin-collector.yaml.in;
  secretsSource = builtins.readFile ../hosts/nixos/pelargir/secrets.nix;
  requiredManifestFragments = [
    "emptyDir: { medium: Memory, sizeLimit: 16Mi }"
    "rm -rf /tmp/mc"
    "trap cleanup EXIT"
    ''expected-image-revision: "@gitRevision@"''
  ];
  requiredSecretFragments = [
    "set -euo pipefail"
    "--from-file=postgres-password="
    "--from-file=hf-token="
    "--from-file=.dockerconfigjson="
    "| k3s kubectl apply -f -"
  ];
  forbiddenSecretFragments = [
    "placeholder.pin_collector"
    "pin-collector-secrets.yaml"
    "pin-collector-registry.yaml"
  ];
  missingManifest = lib.filter (
    fragment: !lib.hasInfix fragment manifestSource
  ) requiredManifestFragments;
  missingSecrets = lib.filter (
    fragment: !lib.hasInfix fragment secretsSource
  ) requiredSecretFragments;
  forbiddenSecrets = lib.filter (
    fragment: lib.hasInfix fragment secretsSource
  ) forbiddenSecretFragments;
in
if missingManifest != [ ] || missingSecrets != [ ] || forbiddenSecrets != [ ] then
  throw "PinCollector ephemeral-secret contract failed"
else
  pkgs.runCommand "pin-collector-secret-contract-ok" { } "touch $out"
