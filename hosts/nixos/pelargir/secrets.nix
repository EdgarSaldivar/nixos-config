# pelargir — sops-nix wiring. Secret values exist only in /run, never the store.
{ config, pkgs, ... }:
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

    # These are Kubernetes Secret manifests. The rendered file stays in
    # /run/secrets/rendered (tmpfs) and is APPLIED from there — see
    # k3s-apply-secrets below. It is deliberately NOT copied into k3s' manifests
    # directory: that directory is persistent disk, pelargir has no full-disk
    # encryption, and this file contains the mosquitto password, the Zigbee network
    # key and the Cloudflare API token in cleartext. Nothing interpolated here
    # enters the Nix store either.
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
            # Publish MQTT discovery so Home Assistant creates entities for
            # every Zigbee device automatically. Without this Z2M talks only to
            # MQTT and HA never learns the devices exist -- which is why nothing
            # reached Apple Home after the restore (found 2026-08-06). The
            # restored config predates the Zigbee setup entirely, so the MQTT
            # and HomeKit integrations have to be re-added in HA as well; this
            # only fixes the Z2M half of that chain.
            homeassistant:
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

  # ---------------------------------------------------------------------------
  #  Apply Secret manifests from /run — never from persistent disk
  # ---------------------------------------------------------------------------
  # Previously the rendered manifest was copied into
  # /var/lib/rancher/k3s/server/manifests/, which left four Secrets — mosquitto
  # password, Zigbee network key, Cloudflare API token — as CLEARTEXT on a disk with
  # no full-disk encryption. Enabling encryption at rest (P1B) protected the
  # datastore and did nothing for that file sitting one directory above it; anyone
  # holding the SD card or NVMe could read all three.
  #
  # So the rendered file stays on tmpfs and is applied from there.
  #
  # This is NOT a bootstrap dependency. The Secrets already live in the datastore, so
  # if this unit is slow or fails the cluster keeps working with the values it has —
  # this only pushes UPDATES. That is why it may retry quietly and why its failure is
  # not allowed to block activation.
  systemd.services.k3s-apply-secrets = {
    description = "Apply rendered Kubernetes Secret manifests from /run";
    after = [ "k3s.service" ];
    wants = [ "k3s.service" ];
    wantedBy = [ "multi-user.target" ];
    # The ONLY PATH this script gets. A missing binary here fails at runtime while
    # the unit can still look like it did something, which this repo has been bitten
    # by before.
    path = with pkgs; [ k3s coreutils ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
      # Bounded: if the API never comes back this must not retry forever and bury the
      # real failure in a restart loop.
      Restart = "on-failure";
      RestartSec = "20s";
      StartLimitBurst = 5;
      TimeoutStartSec = "5m";
    };
    environment.KUBECONFIG = "/etc/rancher/k3s/k3s.yaml";
    script = ''
      set -eu
      src=${config.sops.templates."pelargir-home-secrets.yaml".path}

      if [ ! -f "$src" ]; then
        echo "rendered secret manifest missing at $src" >&2
        exit 1
      fi

      # Wait for the API rather than assuming it. after=k3s.service only orders unit
      # START, and k3s reports started long before the apiserver serves requests.
      for i in $(seq 1 60); do
        if k3s kubectl get --raw /readyz >/dev/null 2>&1; then break; fi
        sleep 5
      done
      if ! k3s kubectl get --raw /readyz >/dev/null 2>&1; then
        echo "k3s API not ready after 5m — not applying" >&2
        exit 1
      fi

      k3s kubectl apply -f "$src"
      echo "applied Secret manifests from tmpfs (nothing written to persistent disk)"
    '';
  };

  # Applied outside the sops block so the option is easy to find.
  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar_password_hash.path;
}
