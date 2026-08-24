# pelargir — apply rendered Kubernetes Secrets from tmpfs.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  pinCollectorRelease = import ../../minas-tirith/pin-collector-release.nix;
in
{
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
    # re-encrypting a value in sops leaves the template text identical.
    #
    # That gap is now covered elsewhere, so no hand restart is needed: every secret and
    # template this unit reads carries `restartUnits = [ "k3s-apply-secrets.service" ]`
    # (see sops.nix), which keys off the DECRYPTED value rather than
    # the unit definition. `flake.nix`'s `secret-applier-contract` enforces that wiring
    # for anything the script reads, and fails closed if the script ever stops
    # interpolating sops paths at all.
    #
    # This was not always true. A rotated value used to reach the host and stop there,
    # because a `oneshot` with `RemainAfterExit` is skipped on activation unless
    # something names it — the rebuild passed, sops reported the new value, and the
    # cluster kept serving the old one. That stranded a broken GHCR credential for two
    # hours on 2026-08-15.
    restartTriggers = [
      config.sops.templates."authentik-secrets.yaml".content
      config.sops.templates."cluster-apps-secrets.yaml".content
      config.sops.templates."pelargir-home-secrets.yaml".content
    ];
    # The ONLY PATH this script gets. A missing binary here fails at runtime while
    # the unit can still look like it did something, which this repo has been bitten
    # by before.
    path = with pkgs; [
      k3s
      coreutils
    ];
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
      set -euo pipefail
      # Every rendered Secret manifest, listed explicitly rather than globbed: /run also
      # holds rendered files that are NOT manifests (k3s-vpn-auth), and applying those
      # would fail confusingly.
      existing_srcs="${config.sops.templates."pelargir-home-secrets.yaml".path} ${
        config.sops.templates."cluster-apps-secrets.yaml".path
      }"
      authentik_src="${config.sops.templates."authentik-secrets.yaml".path}"
      pin_collector_runtime_srcs="
        ${config.sops.secrets.pin_collector_postgres_password.path}
        ${config.sops.secrets.pin_collector_database_url.path}
        ${config.sops.secrets.pin_collector_owner_api_token.path}
        ${config.sops.secrets.pin_collector_admin_api_token.path}
        ${config.sops.secrets.pin_collector_bootstrap_admin_email.path}
        ${config.sops.secrets.pin_collector_bootstrap_admin_password.path}
        ${config.sops.secrets.pin_collector_minio_root_user.path}
        ${config.sops.secrets.pin_collector_minio_root_password.path}
        ${config.sops.secrets.pin_collector_minio_app_user.path}
        ${config.sops.secrets.pin_collector_minio_app_password.path}
        ${config.sops.secrets.pin_collector_hf_token.path}
      "

      for src in $existing_srcs $authentik_src $pin_collector_runtime_srcs; do
        if [ ! -f "$src" ]; then
          echo "required tmpfs secret source missing at $src" >&2
          exit 1
        fi
      done
      ${lib.optionalString pinCollectorRelease.registryPullSecretReady ''
        registry_src="${config.sops.secrets.pin_collector_ghcr_dockerconfigjson.path}"
        if [ ! -f "$registry_src" ]; then
          echo "required tmpfs registry secret source missing at $registry_src" >&2
          exit 1
        fi
      ''}

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
      # Build the Kubernetes Secret from the individual sops-nix tmpfs files.
      # `--from-file` preserves every byte and puts only file paths in argv. The
      # generated YAML flows through an anonymous pipe straight into the API;
      # it is never interpolated into Nix, logged, or written to persistent disk.
      k3s kubectl create secret generic pin-collector-runtime \
        --namespace pin-collector \
        --type=Opaque \
        --from-file=postgres-password=${config.sops.secrets.pin_collector_postgres_password.path} \
        --from-file=database-url=${config.sops.secrets.pin_collector_database_url.path} \
        --from-file=owner-api-token=${config.sops.secrets.pin_collector_owner_api_token.path} \
        --from-file=admin-api-token=${config.sops.secrets.pin_collector_admin_api_token.path} \
        --from-file=bootstrap-admin-email=${config.sops.secrets.pin_collector_bootstrap_admin_email.path} \
        --from-file=bootstrap-admin-password=${config.sops.secrets.pin_collector_bootstrap_admin_password.path} \
        --from-file=minio-root-user=${config.sops.secrets.pin_collector_minio_root_user.path} \
        --from-file=minio-root-password=${config.sops.secrets.pin_collector_minio_root_password.path} \
        --from-file=s3-access-key-id=${config.sops.secrets.pin_collector_minio_app_user.path} \
        --from-file=s3-secret-access-key=${config.sops.secrets.pin_collector_minio_app_password.path} \
        --from-file=hf-token=${config.sops.secrets.pin_collector_hf_token.path} \
        --dry-run=client -o yaml \
        | k3s kubectl apply -f -
      ${lib.optionalString pinCollectorRelease.registryPullSecretReady ''
        k3s kubectl create secret generic pin-collector-registry \
          --namespace pin-collector \
          --type=kubernetes.io/dockerconfigjson \
          --from-file=.dockerconfigjson="$registry_src" \
          --dry-run=client -o yaml \
          | k3s kubectl apply -f -
      ''}
      echo "applied Secret manifests from tmpfs (nothing written to persistent disk)"
    '';
  };
}
