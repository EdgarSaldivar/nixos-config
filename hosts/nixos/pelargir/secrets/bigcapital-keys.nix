# Bigcapital's Secret `bigcapital/bigcapital-runtime`: each Kubernetes key, as
# manifests/bigcapital.yaml reads it, and the secrets/bigcapital.yaml entry it comes from.
#
# ONE list, imported by both sops.nix (what sops-nix decrypts) and k3s-apply-secrets.nix
# (what reaches the cluster), so the two cannot disagree.
{ lib }:
let
  # The top-level key names of secrets/bigcapital.yaml. SOPS encrypts only the values, so
  # the names read in clear; no value is looked at (the same method as PinCollector's
  # pin-collector-release-contract.nix).
  present = map (line: lib.head (lib.splitString ":" line)) (
    lib.filter (line: builtins.match "[a-z0-9_]+:.*" line != null) (
      lib.splitString "\n" (builtins.readFile ../../../../secrets/bigcapital.yaml)
    )
  );

  required = [
    {
      key = "app-jwt-secret";
      sops = "app_jwt_secret";
    }
    # ⛔ db_password and db_root_password are read by MariaDB only on first
    # initialisation; rotating them here alone locks the app out. See
    # docs/runbooks/minas-tirith/bigcapital.md before changing either.
    {
      key = "db-password";
      sops = "db_password";
    }
    {
      key = "db-root-password";
      sops = "db_root_password";
    }
    {
      key = "garage-rpc-secret";
      sops = "garage_rpc_secret";
    }
    {
      key = "garage-admin-token";
      sops = "garage_admin_token";
    }
    {
      key = "garage-app-key-id";
      sops = "garage_app_key_id";
    }
    {
      key = "garage-app-secret";
      sops = "garage_app_secret";
    }
    {
      key = "garage-backup-key-id";
      sops = "garage_backup_key_id";
    }
    {
      key = "garage-backup-secret";
      sops = "garage_backup_secret";
    }
    # Comma-separated, exact and case-sensitive. Only consulted at signup.
    {
      key = "signup-allowed-emails";
      sops = "signup_allowed_emails";
    }
  ];

  # Declared only once the owner has added it to secrets/bigcapital.yaml. sops-nix fails
  # pelargir's activation outright for a declared key the file lacks, so an unconditional
  # declaration would make this repository undeployable until the Resend key existed.
  # The server reads it through an `optional: true` secretKeyRef, so it starts either way.
  optional = lib.filter (k: lib.elem k.sops present) [
    # Resend API key (Sending access, saldivar.io only), used as the SMTP password.
    {
      key = "mail-password";
      sops = "mail_password";
    }
  ];
in
{
  keys = map (k: k // { secret = "bigcapital_${k.sops}"; }) (required ++ optional);
  mailConfigured = lib.elem "mail_password" present;
}
