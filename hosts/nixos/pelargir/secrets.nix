# pelargir — sops-nix wiring. Secret values exist only in /run, never the store.
{ config, ... }:
{
  sops = {
    defaultSopsFile = ../../../secrets/pelargir.yaml;
    defaultSopsFormat = "yaml";
    age = {
      sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
      generateKey = false;
    };

    secrets = {
      wireguard_server_private_key = { };
      wireguard_psk_site_a = { };
      tailscale_auth_key = { };
      k3s_token = { };
      k3s_agent_token = { };
      restic_password = { };
      mosquitto_password = { };
      cloudflare_api_token = { };
      zigbee_network_key = { };
      zigbee_pan_id = { };
      zigbee_ext_pan_id = { };
      zigbee_channel = { };
      # Z2M-typed variants (review fix 2026-08-03). Z2M validates
      # advanced.network_key as a LIST of 16 ints, pan_id as an int, and
      # ext_pan_id as a LIST of 8 bytes — the canonical hex strings above are
      # for zigpy/recovery use and would fail Z2M's schema. ext_pan_id is
      # stored LSB-first per zigbee-herdsman convention; if Z2M reports an
      # ext_pan_id mismatch against the adapter on first start, reverse the
      # byte order (ZIGBEE-RECOVERY.md documents both forms).
      zigbee_network_key_z2m = { };
      zigbee_pan_id_z2m = { };
      zigbee_ext_pan_id_z2m = { };

      # Console login password hash. neededForUsers makes sops decrypt it early
      # enough for user creation. Without this, a keyboard or serial console
      # shows a prompt nobody can satisfy -- see the note in system.nix.
      edgar_password_hash = {
        neededForUsers = true;
      };
    };

    # Keep this beside k3s: the k3s VPN provider consumes the rendered file
    # directly and owns the initial Tailscale login.
    templates."k3s-vpn-auth" = {
      mode = "0400";
      content = "name=tailscale,joinKey=${config.sops.placeholder.tailscale_auth_key}";
    };

    # These are Kubernetes Secret manifests, so the rendered files stay in
    # /run/secrets-rendered and are copied into k3s' live manifests directory
    # only at activation. Nothing interpolated here enters the Nix store.
    templates."pelargir-home-secrets.yaml" = {
      mode = "0400";
      content = ''
        apiVersion: v1
        kind: Secret
        metadata:
          name: mosquitto-auth
          namespace: home
        type: Opaque
        stringData:
          password: "${config.sops.placeholder.mosquitto_password}"
        ---
        apiVersion: v1
        kind: Secret
        metadata:
          name: zigbee2mqtt-config
          namespace: home
        type: Opaque
        stringData:
          configuration.yaml: |
            serial:
              adapter: zstack
              port: /dev/serial/by-id/usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_c436272daea4ed11854ce8a32981d5c7-if00-port0
            permit_join: false
            mqtt:
              server: mqtt://localhost:1883
              user: homeassistant
              password: "${config.sops.placeholder.mosquitto_password}"
            frontend:
              enabled: true
            advanced:
              # Unquoted on purpose: these render as YAML list/int scalars,
              # which is what Z2M's schema requires (see secrets declarations).
              network_key: ${config.sops.placeholder.zigbee_network_key_z2m}
              pan_id: ${config.sops.placeholder.zigbee_pan_id_z2m}
              ext_pan_id: ${config.sops.placeholder.zigbee_ext_pan_id_z2m}
              channel: ${config.sops.placeholder.zigbee_channel}
        ---
        apiVersion: v1
        kind: Secret
        metadata:
          name: ddns-updater-config
          namespace: home
        type: Opaque
        stringData:
          # zone_identifier is the Cloudflare Zone ID, NOT the zone name --
          # qmcgaw/ddns-updater rejects a domain string here. Fetched live
          # 2026-08-04 for saldivar.io. (Caught in pre-install review.)
          config.json: |
            {"settings":[{"provider":"cloudflare","zone_identifier":"82d850933afbd1e1d5e41cdd08a612a6","domain":"pelargir.saldivar.io","ttl":1,"proxied":false,"token":"${config.sops.placeholder.cloudflare_api_token}","ip_version":"ipv4"}]}
        ---
        apiVersion: v1
        kind: Secret
        metadata:
          name: cloudflare-api-token
          namespace: cert-manager
        type: Opaque
        stringData:
          api-token: "${config.sops.placeholder.cloudflare_api_token}"
      '';
    };
  };

  # Applied outside the sops block so the option is easy to find.
  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar_password_hash.path;
}
