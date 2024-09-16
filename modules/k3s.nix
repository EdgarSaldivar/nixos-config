{ config, pkgs, ... }:

let
  flux = pkgs.stdenv.mkDerivation {
    name = "flux";
    src = pkgs.fetchurl {
      url = "https://github.com/fluxcd/flux2/releases/download/v2.3.0/flux_2.3.0_linux_arm64.tar.gz";
      sha256 = "29d2363cfdf13546d900986d265f336ed18c6bbb12d0530c624eaa2ff27b547e";
    };
    phases = [ "unpackPhase" "installPhase" ];
    unpackPhase = ''
      tar -xzf $src
      mkdir -p $out/bin
      mv flux $out/bin/
    '';
    installPhase = ''
      chmod +x $out/bin/flux
    '';
  };
in
{
  environment.systemPackages = with pkgs; [
    k3s
    helm
    flux
    sudo
  ];

  services.k3s = {
    enable = true;
    role = "server";
    clusterInit = true;
    extraFlags = toString [
      "--node-name pelargir"
      "--disable traefik"
      "--disable metrics-server"
      "--etcd-expose-metrics"
      "--data-dir /var/lib/rancher/k3s"
      "--write-kubeconfig-mode 644" # ensure kubectl doesn't need sudo
    ];
  };

  systemd.services.wait-for-network = {
    description = "Wait for Network to be Ready";
    after = [ "network-online.target" "systemd-networkd-wait-online.service" ];
    wants = [ "network-online.target" "systemd-networkd-wait-online.service" ];
    serviceConfig = {
      ExecStart = "${pkgs.bash}/bin/bash /etc/nixos/scripts/wait-for-network.sh";
      Type = "oneshot";
      RemainAfterExit = true;
    };
    wantedBy = [ "multi-user.target" ];
  };

  systemd.services.flux = {
    description = "FluxCD service";
    after = [ "network.target" "k3s.service" "wait-for-network.service" ]; # Ensure k3s service is started before flux
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.bash}/bin/bash -c '${pkgs.coreutils}/bin/yes | ${pkgs.sudo}/bin/sudo ${flux}/bin/flux bootstrap git --url=ssh://git@github.com/EdgarSaldivar/k3s-collective.git --branch=main --path=clusters/k3s --private-key-file=/ssh_host_ed25519_key --kubeconfig=/etc/rancher/k3s/k3s.yaml'";
      Restart = "on-failure"; # Restart only on failure
      Environment = [
      "HOME=/home/edgar"
      "KUBECONFIG=/etc/rancher/k3s/k3s.yaml"
    ];
      RemainAfterExit = true; # Keep the service active after exit
    };
    serviceConfig.User = "root";
  };

  # Copy scripts to /etc/nixos/scripts
  environment.etc."nixos/scripts/create-sealed-secrets-key.sh".source = ../scripts/create-sealed-secrets-key.sh;
  environment.etc."nixos/scripts/apply-sealed-secrets.sh".source = ../scripts/apply-sealed-secrets.sh;

  # Run a script to create the Kubernetes Secret for the private key
  systemd.services.create-sealed-secrets-key = {
    description = "Create Kubernetes Secret for Sealed Secrets private key";
    after = [ "k3s.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.bash}/bin/bash /etc/nixos/scripts/create-sealed-secrets-key.sh";
      Restart = "on-failure";
      RemainAfterExit = true;
    };
    unitConfig = {
      ConditionPathExists = "!/etc/sealed-secrets/key-created.flag";
    };
  };

  # Run a script to apply SealedSecrets after k3s and Flux are ready
  systemd.services.apply-sealed-secrets = {
    description = "Apply SealedSecrets after k3s and Flux are ready";
    after = [ "k3s.service" "flux.service" "create-sealed-secrets-key.service" ];
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.bash}/bin/bash /etc/nixos/scripts/apply-sealed-secrets.sh";
      Restart = "on-failure";
      RemainAfterExit = true;
    };
    unitConfig = {
      ConditionPathExists = "!/etc/sealed-secrets/secrets-applied.flag";
    };
  };

  virtualisation = {
    docker.enable = true;
  };

  networking.firewall.enable = false; # Ensure firewall doesn't interfere
}

