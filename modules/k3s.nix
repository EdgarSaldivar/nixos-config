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

  systemd.services.flux = {
    description = "FluxCD service";
    after = [ "network.target" "k3s.service" ]; # Ensure k3s service is started before flux
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      ExecStart = "${pkgs.bash}/bin/bash -c '${pkgs.coreutils}/bin/yes | ${pkgs.sudo}/bin/sudo ${flux}/bin/flux bootstrap git --url=ssh://git@github.com/EdgarSaldivar/k3s-collective.git --branch=main --path=clusters/k3s --private-key-file=/ssh_host_ed25519_key --kubeconfig=/etc/rancher/k3s/k3s.yaml'";
      Restart = "on-failure"; # Restart only on failure
      Environment = "HOME=/root";
      RemainAfterExit = true; # Keep the service active after exit
    };
    serviceConfig.User = "root";
  };

  virtualisation = {
    docker.enable = true;
  };

  networking.firewall.enable = false; # Ensure firewall doesn't interfere
}
