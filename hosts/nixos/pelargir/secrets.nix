# pelargir — assemble secret declarations, application, and user binding.
{ config, ... }:
{
  imports = [
    ./secrets/sops.nix
    ./secrets/k3s-apply-secrets.nix
  ];

  # Applied outside the sops block so the option is easy to find.
  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar_password_hash.path;
}
