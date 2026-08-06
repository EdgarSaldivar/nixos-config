# Osgiliath secret values exist only below /run. The stable SSH host key is the
# sole machine identity; a missing restored key must never generate a new one.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  secretGate = pkgs.writeShellApplication {
    name = "osgiliath-secrets-gate";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.gnugrep
    ];
    text = ''
      check_present() {
        secret_path="$1"
        secret_name="$2"
        if [ ! -r "$secret_path" ]; then
          echo "osgiliath secret gate: $secret_name is unreadable" >&2
          exit 1
        fi
        secret_value="$(tr -d '\r\n' < "$secret_path")"
        if [ -z "$secret_value" ] || [ "$secret_value" = PLACEHOLDER ]; then
          echo "osgiliath secret gate: $secret_name is unset or still a placeholder" >&2
          exit 1
        fi
      }

      check_present ${config.sops.secrets.edgar_password_hash.path} edgar_password_hash
      check_present ${config.sops.secrets.tailscale_auth_key.path} tailscale_auth_key
      check_present ${config.sops.secrets.k3s_agent_token.path} k3s_agent_token
      check_present ${config.sops.secrets.wifi_psk_raw.path} wifi_psk_raw

      wifi_psk="$(tr -d '\r\n' < ${config.sops.secrets.wifi_psk_raw.path})"
      if ! printf '%s\n' "$wifi_psk" | grep -Eq '^[0-9A-Fa-f]{64}$'; then
        echo "osgiliath secret gate: wifi_psk_raw must be exactly 64 hexadecimal characters" >&2
        exit 1
      fi
    '';
  };
in
{
  # Preserve a last-known-good shadow password if a later sops activation
  # fails. On first install the neededForUsers secret still supplies the hash.
  users.mutableUsers = lib.mkForce true;

  sops = {
    defaultSopsFile = ../../../secrets/osgiliath.yaml;
    defaultSopsFormat = "yaml";
    age = {
      sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
      generateKey = false;
    };

    secrets = {
      edgar_password_hash.neededForUsers = true;
      tailscale_auth_key = { };
      k3s_agent_token = { };
      wifi_psk_raw = { };
    };

    templates = {
      "k3s-vpn-auth" = {
        mode = "0400";
        content = "name=tailscale,joinKey=${config.sops.placeholder.tailscale_auth_key}";
      };
      "wpa-supplicant-secrets" = {
        mode = "0400";
        content = "psk_penthouse=${config.sops.placeholder.wifi_psk_raw}";
      };
    };
  };

  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar_password_hash.path;

  # Both consumers require this oneshot. Merely decrypting a syntactically valid
  # sops file is insufficient because the committed ciphertext is intentionally
  # PLACEHOLDER-only until the migration gate is passed.
  systemd.services.osgiliath-secrets-gate = {
    description = "Reject placeholder Osgiliath runtime secrets";
    restartTriggers = [ config.sops.defaultSopsFile ];
    after = [
      "sops-install-secrets-for-users.service"
      "sops-install-secrets.service"
    ];
    wants = [
      "sops-install-secrets-for-users.service"
      "sops-install-secrets.service"
    ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      ExecStart = lib.getExe secretGate;
    };
  };
}
