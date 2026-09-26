# minas-tirith — Beszel hub: the fleet's live system-metrics dashboard.
#
# Browser path: https://status.saldivar.io → Traefik (Authentik ForwardAuth) → the
# selector-less `monitoring/beszel` Service → 10.0.1.6:8090 on cni0. See
# manifests/node-services.yaml and traefik-routes/catalog.nix.
#
# ⛔ The hub logs a request in as whoever `X-authentik-email` names, so nothing but
# Traefik may reach it. Traefik's ForwardAuth overwrites that header, but cni0 is
# shared by EVERY Pod on this node, so "arrives on cni0" is not "came from
# Traefik": any Pod could call 10.0.1.6:8090 with a forged header and be logged
# in as the administrator (cross-review, 2026-09-26). Hence the gate:
#
#   Traefik ══(mutual TLS)══▶ nginx 0.0.0.0:8090 ──▶ hub 127.0.0.1:8091
#
# The hub binds loopback only. nginx accepts a connection only if the client
# presents a certificate from this host's private gate CA, and Traefik accepts
# only nginx's certificate from that CA. The client key lives solely in the
# Traefik Pod's root-only file-provider mount. A Pod without it is refused at the
# TLS handshake, and one that ARP-spoofs its way into the path (several Pods keep
# NET_RAW/NET_ADMIN) sees ciphertext it can neither read nor replay. A plaintext
# shared-secret header was rejected for exactly that reason (cross-review).
#
# The NixOS firewall is NOT what keeps the tailnet out either: tailscaled
# installs `-A INPUT -j ts-input` with `-i tailscale0 -j ACCEPT` AHEAD of nixos-fw,
# so every port on this host is open to the tailnet whatever
# networking.firewall.interfaces.tailscale0 says (measured 2026-09-23: the hub
# answered 200 from pelargir over the tailnet). The raw-table rule below runs
# before both chains and drops 8090 unless it arrives on cni0, the Pod bridge.
#
# Agents (modules/nixos/fleet/metrics.nix) are reached hub → agent over SSH on
# the tailnet. The agent only accepts this hub's key, so the agents hold no
# secret; the hub's private key never leaves this host's state directory.
{
  lib,
  pkgs,
  config,
  ...
}:
let
  cfg = config.services.beszel.hub;
  dataDir = "${cfg.dataDir}/beszel_data";

  # gatePort is what the monitoring/beszel EndpointSlice targets (10.0.1.6:8090);
  # hubPort is loopback-only behind it.
  gatePort = 8090;
  hubPort = 8091;
  gateDir = "/var/lib/beszel-gate";
  # Outside gateDir (root-only, holds the CA key): nginx runs as the nginx user.
  nginxDir = "/var/lib/beszel-gate-nginx";
  # traefik-routes/delivery.nix `routeDir` on the host, /etc/traefiks in the Pod.
  routeDir = "/usr/local/etc/traefik";
  traefikCertDir = "${routeDir}/beszel-gate";
  podCertDir = "/etc/traefiks/beszel-gate";
  # The name both ends agree on; the Service DNS name is not in the certificate.
  serverName = "beszel-gate";

  transportFile = pkgs.writeText "beszel-gate.yml" (
    builtins.toJSON {
      http.serversTransports.beszel-gate = {
        inherit serverName;
        rootCAs = [ "${podCertDir}/ca.crt" ];
        certificates = [
          {
            certFile = "${podCertDir}/client.crt";
            keyFile = "${podCertDir}/client.key";
          }
        ];
      };
    }
  );

  # Idempotent: the CA (20 years) and both leaves (10 years) are created once and
  # reused while they still verify against the CA, match their keys and are more
  # than 30 days from expiry; otherwise the leaf is re-issued on this activation.
  #
  # Rotation (or a suspected key leak): delete /var/lib/beszel-gate, rebuild or
  # re-run activation, then `systemctl restart nginx` and restart the Traefik Pod
  # so both load the new material. Replacing the CA is the same procedure.
  gateInit = pkgs.writeShellScript "beszel-gate-init" ''
    set -eu
    PATH=${
      lib.makeBinPath [
        pkgs.coreutils
        pkgs.openssl
      ]
    }
    umask 077
    install -d -m 0700 -o root -g root ${gateDir}
    cd ${gateDir}
    if [ ! -s ca.key ] || [ ! -s ca.crt ]; then
      openssl req -x509 -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
        -keyout ca.key -out ca.crt -days 7300 -subj "/CN=beszel-gate CA" \
        -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign 2>/dev/null
      rm -f server.crt client.crt
    fi
    issue() { # name extendedKeyUsage [subjectAltName]
      if [ -s "$1.crt" ] && [ -s "$1.key" ] \
        && openssl verify -CAfile ca.crt "$1.crt" >/dev/null 2>&1 \
        && openssl x509 -checkend 2592000 -noout -in "$1.crt" >/dev/null \
        && [ "$(openssl x509 -in "$1.crt" -noout -pubkey)" = "$(openssl pkey -in "$1.key" -pubout)" ]; then
        return 0
      fi
      openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
        -keyout "$1.key" -out "$1.csr" -subj "/CN=$1" 2>/dev/null
      printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=%s\n%s\n' \
        "$2" "''${3:-}" > "$1.ext"
      openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial \
        -days 3650 -extfile "$1.ext" -out "$1.crt" 2>/dev/null
      rm -f "$1.csr" "$1.ext"
    }
    issue server serverAuth "subjectAltName=DNS:${serverName}"
    issue client clientAuth

    # nginx runs (and tests its configuration) as the nginx user.
    install -d -m 0750 -o root -g nginx ${nginxDir}
    install -m 0640 -o root -g nginx ca.crt server.crt server.key ${nginxDir}/

    # Traefik runs as root in its Pod and is the only consumer of this mount.
    install -d -m 0700 -o root -g root ${traefikCertDir}
    install -m 0600 -o root -g root ca.crt client.crt client.key ${traefikCertDir}/
    install -m 0644 -o root -g root ${transportFile} ${routeDir}/beszel-gate.yml
  '';

  # Declared, not clicked: the hub reconciles its systems table with config.yml on
  # every start and deletes systems that are not listed. See ./beszel-systems.nix.
  systems = import ./beszel-systems.nix;
  systemsFile = (pkgs.formats.yaml { }).generate "beszel-config.yml" {
    systems = map (s: s // { port = 45876; }) systems;
  };

  # Replaces the module's ExecStartPre so the three first-run steps happen in one
  # place and in order:
  #   1. the SSH key exists before anything reads it, and its PUBLIC half is
  #      published to /run so an operator can read it without touching state;
  #   2. `migrate up` sees USER_EMAIL/USER_PASSWORD, which it consumes exactly once
  #      to create the Authentik-matched user (the password is random and only a
  #      break-glass credential — login is by header);
  #   3. config.yml is refreshed from the store.
  hubInit = pkgs.writeShellApplication {
    name = "beszel-hub-init";
    runtimeInputs = [
      pkgs.coreutils
      pkgs.openssh
      cfg.package
    ];
    text = ''
      umask 077
      mkdir -p ${dataDir}
      if [ ! -e ${dataDir}/id_ed25519 ]; then
        ssh-keygen -q -t ed25519 -N "" -C beszel-hub@minas-tirith -f ${dataDir}/id_ed25519
      fi
      umask 022
      ssh-keygen -y -f ${dataDir}/id_ed25519 > /run/beszel-hub/hub.pub
      umask 077

      if [ ! -s ${cfg.dataDir}/break-glass-password ]; then
        head -c 32 /dev/urandom | base64 | tr -d '/+=' > ${cfg.dataDir}/break-glass-password
      fi
      USER_PASSWORD=$(cat ${cfg.dataDir}/break-glass-password)
      export USER_PASSWORD
      beszel-hub migrate up
      # No `history-sync`: the upstream module runs it, but beszel 0.18.7 has no
      # such command and only prints an error. Re-add it when the package does.

      install -m 0600 ${systemsFile} ${dataDir}/config.yml
    '';
  };
in
{
  services.beszel.hub = {
    enable = true;
    # Loopback only: the gate below is the one way in.
    host = "127.0.0.1";
    port = hubPort;
    environment = {
      APP_URL = "https://status.saldivar.io";
      TRUSTED_AUTH_HEADER = "X-authentik-email";
      # The Authentik administrator (AUTHENTIK_BOOTSTRAP_EMAIL in authentik.yaml).
      # Read once, on the first migration; it names the user the header logs in.
      USER_EMAIL = "teremaire@gmail.com";
    };
  };

  systemd.services.beszel-hub.serviceConfig.ExecStartPre = lib.mkForce [ (lib.getExe hubInit) ];

  # Before the route that references beszel-gate@file is published, so Traefik
  # never sees the route without its transport.
  system.activationScripts.beszel-gate = lib.stringAfter [
    "users"
    "groups"
  ] "${gateInit}";
  system.activationScripts.minas-traefik-routes.deps = [ "beszel-gate" ];

  services.nginx = {
    enable = true;
    virtualHosts.beszel-gate = {
      listen = [
        {
          addr = "0.0.0.0";
          port = gatePort;
          ssl = true;
        }
      ];
      onlySSL = true;
      sslCertificate = "${nginxDir}/server.crt";
      sslCertificateKey = "${nginxDir}/server.key";
      extraConfig = ''
        ssl_protocols TLSv1.3;
        ssl_client_certificate ${nginxDir}/ca.crt;
        ssl_verify_client on;
      '';
      locations."/" = {
        proxyPass = "http://127.0.0.1:${toString hubPort}";
        # The hub's realtime API is a long-lived event stream.
        proxyWebsockets = true;
        extraConfig = ''
          proxy_set_header Host $host;
          proxy_buffering off;
          proxy_read_timeout 1h;
        '';
      };
    };
  };

  # Deliberately NO networking.firewall.*.allowedTCPPorts entry for 8090, and a
  # raw-table drop for every other ingress path — see the header.
  # checks/fleet-metrics.nix fails the build if either changes.
  networking.firewall.extraCommands = ''
    ip46tables -t raw -D PREROUTING -p tcp --dport ${toString gatePort} ! -i cni0 -j DROP 2>/dev/null || true
    ip46tables -t raw -A PREROUTING -p tcp --dport ${toString gatePort} ! -i cni0 -j DROP
  '';
  networking.firewall.extraStopCommands = ''
    ip46tables -t raw -D PREROUTING -p tcp --dport ${toString gatePort} ! -i cni0 -j DROP 2>/dev/null || true
  '';
}
