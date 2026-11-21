# Bigcapital's Secret `bigcapital/bigcapital-runtime`: each Kubernetes key, as
# manifests/bigcapital.yaml reads it, and the sops entry it comes from.
#
# ONE list, imported by both sops.nix (what sops-nix decrypts) and k3s-apply-secrets.nix
# (what reaches the cluster), so the two cannot disagree.
{ lib }:
let
  app = ../../../../secrets/bigcapital.yaml;
  backup = ../../../../secrets/bigcapital-backup.yaml;

  # The top-level key names of a SOPS document. SOPS encrypts only the values, so the
  # names read in clear; no value is looked at (the same method as PinCollector's
  # pin-collector-release-contract.nix).
  namesIn =
    file:
    map (line: lib.head (lib.splitString ":" line)) (
      lib.filter (line: builtins.match "[a-z0-9_]+:.*" line != null) (
        lib.splitString "\n" (builtins.readFile file)
      )
    );
  present = file: name: lib.elem name (namesIn file);

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
    # ⛔ The only key to the off-site restic repository on pelargir. Losing it loses every
    # snapshot; the copy in this encrypted document is the recovery path.
    {
      key = "backup-restic-password";
      sops = "restic_password";
      file = backup;
      secret = "bigcapital_backup_restic_password";
    }
  ];

  # Declared only once the owner has added them. sops-nix fails pelargir's activation
  # outright for a declared key its file lacks, so an unconditional declaration would make
  # this repository undeployable until the value existed. Both are read through
  # `optional: true` references, so their consumers run either way.
  optional = lib.filter (k: present k.file k.sops) [
    # Resend API key (Sending access, saldivar.io only), used as the SMTP password.
    {
      key = "mail-password";
      sops = "mail_password";
      file = app;
    }
    # Healthchecks ping URL: the backup's dead man's switch (pinged after every success).
    {
      key = "backup-healthchecks-url";
      sops = "healthchecks_url";
      file = backup;
      secret = "bigcapital_backup_healthchecks_url";
    }
  ];

  normalise =
    k:
    {
      file = app;
      secret = "bigcapital_${k.sops}";
    }
    // k;
in
{
  keys = map normalise (required ++ optional);
  mailConfigured = present app "mail_password";
  backupHeartbeatConfigured = present backup "healthchecks_url";
}
