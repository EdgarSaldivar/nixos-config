# minas-tirith — sops-nix secret wiring.
#
# How this bootstraps without shipping any extra key material:
#   sops-nix converts the machine's SSH ed25519 HOST key into an age identity
#   and decrypts with it. The host's public half is already a recipient in
#   ../../../.sops.yaml, and the old host keys are restored during the install
#   (nixos-anywhere --extra-files), so decryption works from the very first boot.
#   Nothing has to be typed in, and no age key file needs provisioning.
#
# Secrets land in /run/secrets (tmpfs, root-owned, 0400) — never on disk.
#
# Edit secrets with:   sops secrets/minas-tirith.yaml
# Add a recipient:     edit .sops.yaml, then `sops updatekeys secrets/minas-tirith.yaml`
{ config, ... }:
{
  sops = {
    defaultSopsFile = ../../../secrets/minas-tirith.yaml;
    defaultSopsFormat = "yaml";

    # Derive the decryption identity from the host key. Explicitly NOT using a
    # standalone /var/lib/sops-nix/key.txt: that would be one more thing to
    # provision correctly on a machine we can only reach remotely.
    age = {
      sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
      # Don't silently invent a new key if the host key is missing — fail loudly
      # instead, so a broken restore is obvious rather than mysterious.
      generateKey = false;
    };
  };

  # ---------------------------------------------------------------------------
  # Console login password.
  #
  # This closes a genuine footgun: users/edgar/default.nix sets
  # mutableUsers = false, and sshd here is key-only with root login disabled.
  # Without a password, a BMC/SOL console shows a login prompt that CANNOT be
  # satisfied — if sshd or networking breaks, the machine is unreachable while
  # being perfectly healthy. That is exactly the failure shape that cost hours
  # on 2026-07-29.
  #
  # `neededForUsers` makes sops decrypt this early enough for user creation.
  # With security.sudo.wheelNeedsPassword = false, this one password also grants
  # full sudo, so it is the single credential that recovers the box.
  # ---------------------------------------------------------------------------
  sops.secrets.edgar-password = {
    neededForUsers = true;
  };

  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar-password.path;
}
