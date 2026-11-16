# edoras — secret values exist only below /run. The SSH host key is the sole
# machine identity: it was generated off-box and is placed into /etc/ssh at
# install, so sops-nix decrypts from first boot. A host that generated its own
# key would be locked out of its secrets.
#
# ⛔ NO DISK ENCRYPTION, deliberately (owner decision 2026-11-15). Nobody on
# site can answer a passphrase prompt, and a TPM-bound key falls back to one
# after a firmware or Secure Boot change. What is on this disk is public model
# weights plus the secrets below. If the box is stolen, rotate them:
#   - remove the edoras node from the tailnet
#   - edgar_password_hash (console password)
#   - any workload key added to secrets/edoras.yaml later
{ config, lib, ... }:
{
  # Preserve a last-known-good shadow password if a later sops activation
  # fails. On first install the neededForUsers secret still supplies the hash.
  users.mutableUsers = lib.mkForce true;

  sops = {
    defaultSopsFile = ../../../secrets/edoras.yaml;
    defaultSopsFormat = "yaml";
    age = {
      sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
      generateKey = false;
    };

    secrets = {
      # Console login for on-site rescue; plaintext at
      # ~/Development/secrets/edoras/console_password on the Mac.
      edgar_password_hash.neededForUsers = true;
      tailscale_auth_key = { };
    };
  };

  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar_password_hash.path;
}
