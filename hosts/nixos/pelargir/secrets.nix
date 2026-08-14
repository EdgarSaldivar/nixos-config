# pelargir — sops-nix wiring. Secret values exist only in /run, never the store.
{ config, lib, pkgs, ... }:
let
  pinCollectorRelease = import ../minas-tirith/pin-collector-release.nix;
in
{
  sops = lib.recursiveUpdate {
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
      # Dedicated healthchecks.io endpoint for the external minas ingress probe.
      # It intentionally does not share minas' aggregate heartbeat.
      minas-ingress-healthchecks-url = { };

      # Application secrets for cluster workloads live in their OWN sops file, with
      # only the admin and pelargir as recipients. minas is an agent with no deploy
      # credential and is deliberately not a recipient — see .sops.yaml.
      palworld_admin_password = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      # tracearr's JWT and cookie signing secrets. They were plaintext env values in the
      # compose file; this repository is PUBLIC, so they cannot be inline in a manifest.
      # Migrated VERBATIM rather than rotated — rotating them invalidates every session,
      # and rotation is already tracked separately (they are short and weak).
      tracearr_jwt_secret = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      tracearr_cookie_secret = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      # PIA credentials for the gluetun-based VPN Pods (K3S-VPN-STACK-DESIGN.md).
      #
      # ⛔ These are consumed as a Secret VOLUME via gluetun's *_SECRETFILE variables,
      # NOT as secretKeyRef env. A resolved env value reaches containerd's on-disk
      # container metadata on the node, which defeats the point of encrypting them here.
      # gluetun reads them from files natively, so the file path costs nothing.
      #
      # ⚠️ Also on the rotate list: binhex wrote these to
      # /usr/local/etc/deluge-books/openvpn/credentials.conf on a hostPath, so they are
      # in the backup mirror and in ZFS snapshots. Migrating them here does not rotate
      # them — see K3S-HANDOFF.md.
      pia_openvpn_username = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      pia_openvpn_password = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      # MyAnonaMouse session cookie. Was a LITERAL value in deluge-books' docker argv,
      # and therefore also in config.v2.json on disk and in the compose file.
      #
      # ⚠️ MAM sessions are IP-BOUND. This is not decoration: the registrar re-registers
      # the current VPN exit IP, and without it MAM access breaks at the next endpoint
      # change — silently, and long after the cutover.
      mam_session_cookie = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      # nextcloud's Postgres credentials. Plaintext env on the docker container; this
      # repository is PUBLIC so they cannot be inline in a manifest. Consumed by BOTH the
      # database (which uses them to initialise/authenticate) and the app (which connects
      # with them), so the Secret is mounted/referenced by both workloads in `nextcloud`.
      nextcloud_postgres_user = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      nextcloud_postgres_password = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      # immich's Postgres credentials, consumed by both the database and the app.
      # ⚠️ `database` is not a secret in any meaningful sense, but it is carried here so
      # the Secret is the single object both workloads read — see the note on the Secret
      # template below about it disagreeing with the manifest's inline copy.
      immich_postgres_user = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      immich_postgres_password = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      immich_postgres_database = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      # traefik's three env values. ⛔ The Cloudflare DNS API token is the
      # highest-value credential on this fleet — it can edit DNS for the whole zone,
      # and traefik uses it for the DNS-01 challenge behind the *.saldivar.io
      # wildcard. It was plaintext in a docker env file until now.
      traefik_cloudflare_dns_api_token = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      traefik_cloudflare_email = {
        sopsFile = ../../../secrets/cluster-apps.yaml;
      };
      # PinCollector has a dedicated SOPS document so its credentials migrate as
      # one bounded unit without widening access to unrelated fleet secrets.
      pin_collector_postgres_password = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "postgres_password";
      };
      pin_collector_database_url = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "database_url";
      };
      pin_collector_owner_api_token = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "owner_api_token";
      };
      pin_collector_admin_api_token = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "admin_api_token";
      };
      pin_collector_bootstrap_admin_email = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "bootstrap_admin_email";
      };
      pin_collector_bootstrap_admin_password = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "bootstrap_admin_password";
      };
      pin_collector_minio_root_user = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "minio_root_user";
      };
      pin_collector_minio_root_password = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "minio_root_password";
      };
      pin_collector_minio_app_user = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "minio_app_user";
      };
      pin_collector_minio_app_password = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "minio_app_password";
      };
      pin_collector_hf_token = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "hf_token";
      };
      # Identity-system secrets are isolated in their own SOPS document. Pelargir is
      # the only host recipient because it renders/applies Kubernetes Secrets; minas
      # receives only the namespace-scoped Secret through the cluster datastore.
      authentik_secret_key = {
        sopsFile = ../../../secrets/authentik.yaml;
      };
      authentik_postgres_password = {
        sopsFile = ../../../secrets/authentik.yaml;
      };
      # A Django password hash, not plaintext. Authentik reads it only during the
      # worker's first successful bootstrap, but it remains encrypted for recovery.
      authentik_bootstrap_password_hash = {
        sopsFile = ../../../secrets/authentik.yaml;
      };
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
    } // lib.optionalAttrs pinCollectorRelease.registryPullSecretReady {
      # Compact Docker config JSON for a read-only GHCR package credential. It is
      # intentionally absent until the credential is provisioned; the release
      # assertion prevents workloads from being enabled without it.
      pin_collector_ghcr_dockerconfigjson = {
        sopsFile = ../../../secrets/pin-collector.yaml;
        key = "ghcr_dockerconfigjson";
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

    # Cluster application secrets, rendered to tmpfs and applied from there. The
    # namespace comes from minas-tirith/manifests/namespaces.yaml; this only supplies
    # the Secret its Pods reference.
    #
    # Required because nixos-config is a PUBLIC repository: palworld's ADMIN_PASSWORD
    # cannot be an inline env value in a manifest without publishing it.
    templates."cluster-apps-secrets.yaml" = {
      mode = "0400";
      content = ''
        apiVersion: v1
        kind: Secret
        metadata:
          name: palworld
          namespace: games
        type: Opaque
        stringData:
          admin-password: "${config.sops.placeholder.palworld_admin_password}"
        ---
        apiVersion: v1
        kind: Secret
        metadata:
          name: tracearr
          namespace: media
        type: Opaque
        stringData:
          jwt-secret: "${config.sops.placeholder.tracearr_jwt_secret}"
          cookie-secret: "${config.sops.placeholder.tracearr_cookie_secret}"
        ---
        # Consumed as a Secret VOLUME by the gluetun sidecar, via
        # OPENVPN_USER_SECRETFILE / OPENVPN_PASSWORD_SECRETFILE.
        #
        # ⛔ Mount the WHOLE directory, never with subPath — subPath mounts do not
        # receive updates, so a rotation would appear to succeed and change nothing.
        #
        # ⛔ Rotation is ONE transaction: sops edit -> systemctl restart
        # k3s-apply-secrets (this unit does NOT react to value-only changes) ->
        # confirm resourceVersion changed -> recreate Pods ONE AT A TIME, proving
        # tunnel up and exit IP non-local between each. gluetun reads these at
        # startup, so applying the Secret alone changes nothing in a running Pod.
        apiVersion: v1
        kind: Secret
        metadata:
          name: pia-openvpn
          namespace: media
        type: Opaque
        stringData:
          openvpn-username: "${config.sops.placeholder.pia_openvpn_username}"
          openvpn-password: "${config.sops.placeholder.pia_openvpn_password}"
        ---
        # Mounted by the MAM registrar sidecar ALONE — no other container gets it. The
        # registrar performs the HTTPS call itself so the cookie never reaches a child
        # process argv or a log, which is exactly how it leaked into docker's Cmd.
        # ⛔ The SAME PIA credentials, rendered a SECOND time into `books`. Secrets are
        # NAMESPACE-SCOPED: the books netns trio (gluetun + qbittorrent + flaresolverr)
        # cannot mount the `media` copy. Both documents render from the same sops values,
        # so a rotation updates both — but ⚠️ BOTH namespaces' Pods must then be recreated,
        # since gluetun reads credentials at startup.
        apiVersion: v1
        kind: Secret
        metadata:
          name: pia-openvpn
          namespace: books
        type: Opaque
        stringData:
          openvpn-username: "${config.sops.placeholder.pia_openvpn_username}"
          openvpn-password: "${config.sops.placeholder.pia_openvpn_password}"
        ---
        apiVersion: v1
        kind: Secret
        metadata:
          name: nextcloud-postgres
          namespace: nextcloud
        type: Opaque
        stringData:
          username: "${config.sops.placeholder.nextcloud_postgres_user}"
          password: "${config.sops.placeholder.nextcloud_postgres_password}"
        ---
        # immich's Postgres credentials. Same reasoning as nextcloud's above: plaintext
        # env on the docker container today, and this repository is PUBLIC.
        # ⚠️ `database` is carried here as well as `username`/`password`, because the
        # manifest's app container also names the database INLINE. Those two must agree —
        # see the split-source-of-truth note in manifests/immich.yaml. Keep `database`
        # equal to `immich` and `username` equal to `postgres` unless both are changed.
        # ⛔ The password is the EXISTING one, reused verbatim. On an already-initialised
        # PGDATA, POSTGRES_PASSWORD is an initialisation-time variable and does NOT change
        # the stored role password — inventing a new value here would produce a Secret
        # that authenticates against nothing, and the failure would look like a broken
        # database rather than a wrong credential.
        apiVersion: v1
        kind: Secret
        metadata:
          name: immich-postgres
          namespace: immich
        type: Opaque
        stringData:
          username: "${config.sops.placeholder.immich_postgres_user}"
          password: "${config.sops.placeholder.immich_postgres_password}"
          database: "${config.sops.placeholder.immich_postgres_database}"
        ---
        # traefik's environment. ⚠️ Unlike every other Secret here, losing or
        # mis-rendering this one takes down ALL 26 hostnames, because traefik will
        # not obtain or renew the wildcard certificate without the Cloudflare token.
        apiVersion: v1
        kind: Secret
        metadata:
          name: traefik-env
          namespace: traefik
        type: Opaque
        stringData:
          cloudflare-dns-api-token: "${config.sops.placeholder.traefik_cloudflare_dns_api_token}"
          cloudflare-email: "${config.sops.placeholder.traefik_cloudflare_email}"
        ---
        apiVersion: v1
        kind: Secret
        metadata:
          name: mam-session
          namespace: media
        type: Opaque
        stringData:
          session-cookie: "${config.sops.placeholder.mam_session_cookie}"
      '';
    };

    # PinCollector runtime values are mounted as files. The API entrypoint has an
    # explicit *_FILE allowlist, while Postgres and MinIO use their native file
    # settings. This avoids resolved secret values in containerd's env metadata.
    templates."pin-collector-secrets.yaml" = {
      mode = "0400";
      content = ''
        apiVersion: v1
        kind: Secret
        metadata:
          name: pin-collector-runtime
          namespace: pin-collector
        type: Opaque
        stringData:
          postgres-password: "${config.sops.placeholder.pin_collector_postgres_password}"
          database-url: "${config.sops.placeholder.pin_collector_database_url}"
          owner-api-token: "${config.sops.placeholder.pin_collector_owner_api_token}"
          admin-api-token: "${config.sops.placeholder.pin_collector_admin_api_token}"
          bootstrap-admin-email: "${config.sops.placeholder.pin_collector_bootstrap_admin_email}"
          bootstrap-admin-password: "${config.sops.placeholder.pin_collector_bootstrap_admin_password}"
          minio-root-user: "${config.sops.placeholder.pin_collector_minio_root_user}"
          minio-root-password: "${config.sops.placeholder.pin_collector_minio_root_password}"
          s3-access-key-id: "${config.sops.placeholder.pin_collector_minio_app_user}"
          s3-secret-access-key: "${config.sops.placeholder.pin_collector_minio_app_password}"
          hf-token: "${config.sops.placeholder.pin_collector_hf_token}"
      '';
    };

    # Authentik's Secret is deliberately its own rendered manifest. This keeps the
    # identity system's lifecycle reviewable and lets k3s-apply-secrets wait for its
    # new namespace without coupling that wait to the already-live application Secrets.
    templates."authentik-secrets.yaml" = {
      mode = "0400";
      content = ''
        apiVersion: v1
        kind: Secret
        metadata:
          name: authentik-secrets
          namespace: authentik
        type: Opaque
        stringData:
          secret-key: "${config.sops.placeholder.authentik_secret_key}"
          postgres-password: "${config.sops.placeholder.authentik_postgres_password}"
          bootstrap-password-hash: "${config.sops.placeholder.authentik_bootstrap_password_hash}"
      '';
    };
  } (lib.optionalAttrs pinCollectorRelease.registryPullSecretReady {
    templates."pin-collector-registry.yaml" = {
      mode = "0400";
      content = ''
        apiVersion: v1
        kind: Secret
        metadata:
          name: pin-collector-registry
          namespace: pin-collector
        type: kubernetes.io/dockerconfigjson
        stringData:
          .dockerconfigjson: |-
            ${config.sops.placeholder.pin_collector_ghcr_dockerconfigjson}
      '';
    };
  });

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
    # ⛔ WITHOUT THIS, ADDING A SECRET SILENTLY DOES NOTHING.
    #
    # This unit is `Type=oneshot` with `RemainAfterExit=true`, so once it has run it
    # stays `active` forever. A rebuild that changes the rendered manifest therefore
    # re-renders the file to tmpfs and NEVER APPLIES IT — the unit is already "active",
    # so systemd has no reason to touch it.
    #
    # Found on 2026-08-08 adding tracearr's Secret: the template rendered correctly and
    # `kubectl get secret tracearr` said NotFound, with nothing anywhere reporting a
    # problem. The workload would then have failed to start on a missing secretKeyRef,
    # pointing at the Pod rather than at this.
    #
    # The template content carries only PLACEHOLDERS, never secret values, so hashing it
    # into the unit definition puts nothing sensitive in the store.
    #
    # ⚠️ LIMIT, stated rather than papered over: this catches STRUCTURAL changes — a
    # secret added, removed or renamed. It does NOT catch a value-only ROTATION, because
    # re-encrypting a value in sops leaves the template text identical. After rotating a
    # value, run `systemctl restart k3s-apply-secrets` by hand.
    restartTriggers = [
      config.sops.templates."authentik-secrets.yaml".content
      config.sops.templates."cluster-apps-secrets.yaml".content
      config.sops.templates."pelargir-home-secrets.yaml".content
      config.sops.templates."pin-collector-secrets.yaml".content
    ] ++ lib.optional pinCollectorRelease.registryPullSecretReady
      config.sops.templates."pin-collector-registry.yaml".content;
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
      # Every rendered Secret manifest, listed explicitly rather than globbed: /run also
      # holds rendered files that are NOT manifests (k3s-vpn-auth), and applying those
      # would fail confusingly.
      existing_srcs="${config.sops.templates."pelargir-home-secrets.yaml".path} ${config.sops.templates."cluster-apps-secrets.yaml".path}"
      authentik_src="${config.sops.templates."authentik-secrets.yaml".path}"
      pin_collector_src="${config.sops.templates."pin-collector-secrets.yaml".path}"
      registry_src="${lib.optionalString pinCollectorRelease.registryPullSecretReady config.sops.templates."pin-collector-registry.yaml".path}"
      srcs="$existing_srcs $authentik_src $pin_collector_src $registry_src"

      for src in $srcs; do
        if [ ! -f "$src" ]; then
          echo "rendered secret manifest missing at $src" >&2
          exit 1
        fi
      done

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

      # Do not let a first-deploy race with k3s's namespace AddOn make the already-live
      # Secret refresh fail. Existing manifests apply first; the new Secret waits for
      # its namespace, which is delivered by minas-namespaces.yaml in the same rebuild.
      for src in $existing_srcs; do
        k3s kubectl apply -f "$src"
      done
      for i in $(seq 1 60); do
        if k3s kubectl get namespace authentik >/dev/null 2>&1; then break; fi
        sleep 2
      done
      if ! k3s kubectl get namespace authentik >/dev/null 2>&1; then
        echo "authentik namespace not ready after 2m — not applying its Secret" >&2
        exit 1
      fi
      k3s kubectl apply -f "$authentik_src"
      for i in $(seq 1 60); do
        if k3s kubectl get namespace pin-collector >/dev/null 2>&1; then break; fi
        sleep 2
      done
      if ! k3s kubectl get namespace pin-collector >/dev/null 2>&1; then
        echo "pin-collector namespace not ready after 2m — not applying its Secret" >&2
        exit 1
      fi
      k3s kubectl apply -f "$pin_collector_src"
      ${lib.optionalString pinCollectorRelease.registryPullSecretReady ''
        k3s kubectl apply -f "$registry_src"
      ''}
      echo "applied Secret manifests from tmpfs (nothing written to persistent disk)"
    '';
  };

  # Applied outside the sops block so the option is easy to find.
  users.users.edgar.hashedPasswordFile = config.sops.secrets.edgar_password_hash.path;
}
