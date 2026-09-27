{
  lib,
  pkgs,
  nixosConfigurations,
  ...
}:

# Dungeon Scriber secrets reach the cluster only as SOPS references, and only once the
# encrypted document exists:
#
#   * while runtimeSecretReady is false, pelargir declares no Dungeon Scriber secret and
#     the applier creates no Secret, so evaluation never needs the missing document;
#   * once it is true, every key is a sops-nix secret from secrets/dungeon-scriber.yaml
#     and the applier builds the Secret with --from-file (no values in YAML, argv or
#     the store);
#   * if secrets/dungeon-scriber.yaml exists at all, it must be a SOPS document whose
#     every key is ciphertext. A plaintext file in this public repository is the one
#     mistake that cannot be undone by a later commit.
let
  release = import ../hosts/nixos/minas-tirith/dungeon-scriber-release.nix;
  pelargir = nixosConfigurations.pelargir.config;
  script = pelargir.systemd.services.k3s-apply-secrets.script;
  secrets = lib.filterAttrs (n: _: lib.hasPrefix "dungeon_scriber_" n) pelargir.sops.secrets;
  runtimeKeys = [
    "postgres_password"
    "database_url"
    "internal_worker_tokens"
  ];
  expectedKeys = runtimeKeys ++ lib.optional release.registryPullSecretReady "ghcr_dockerconfigjson";

  secretFile = ../secrets/dungeon-scriber.yaml;
  fileLines = lib.splitString "\n" (builtins.readFile secretFile);
  # A top-level `key: value` line whose value is not SOPS ciphertext.
  plaintextLines = lib.filter (
    line:
    let
      m = builtins.match "([a-z_]+): (.+)" line;
    in
    m != null && lib.head m != "sops" && !(lib.hasPrefix "ENC[" (lib.elemAt m 1))
  ) fileLines;

  problems =
    (
      if release.runtimeSecretReady then
        lib.optional (
          lib.sort (a: b: a < b) (map (s: s.key) (lib.attrValues secrets))
          != lib.sort (a: b: a < b) expectedKeys
        ) "pelargir's Dungeon Scriber sops keys are not exactly ${lib.concatStringsSep ", " expectedKeys}"
        ++ lib.optional (lib.any (s: toString s.sopsFile != toString secretFile) (
          lib.attrValues secrets
        )) "a Dungeon Scriber secret is read from a document other than secrets/dungeon-scriber.yaml"
        ++ lib.optional (
          !lib.hasInfix "create secret generic dungeon-scriber-runtime" script
        ) "the applier does not create dungeon-scriber-runtime"
        ++ lib.optional (
          !lib.hasInfix "--from-file=internal-worker-tokens=" script
        ) "the applier does not pass the worker token hashes by file"
        ++ lib.optional (
          !builtins.pathExists secretFile
        ) "runtimeSecretReady is set but secrets/dungeon-scriber.yaml does not exist"
      else
        lib.optional (secrets != { }) "Dungeon Scriber secrets are declared before runtimeSecretReady"
        ++ lib.optional (lib.hasInfix "dungeon-scriber-runtime" script) "the applier creates dungeon-scriber-runtime before runtimeSecretReady"
    )
    ++ lib.optionals (builtins.pathExists secretFile) (
      lib.optional (
        !lib.any (l: l == "sops:") fileLines
      ) "secrets/dungeon-scriber.yaml is not a SOPS document"
      ++ lib.optional (plaintextLines != [ ]) "secrets/dungeon-scriber.yaml holds a plaintext value"
    );
in
if problems != [ ] then
  throw "Dungeon Scriber secret contract: ${lib.concatStringsSep "; " problems}"
else
  pkgs.runCommand "dungeon-scriber-secret-contract-ok" { } "touch $out"
