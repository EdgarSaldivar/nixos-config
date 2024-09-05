{ config, pkgs, fluxcd, ... }:

{

  environment.systemPackages = with pkgs;
    [
      k3s
      helm
      flux
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
      "--write-kubeconfig-mode 644" # ensure kubectl doesnt need sudo
    ];
  };

  services.fluxcd = {
    enable = true;
    gitUrl = "https://github.com/EdgarSaldivar/k3s-collective.git";
    gitBranch = "main";
    sshKeyFile = "/etc/ssh/ssh_host_ed25519_key";
  };

  virtualisation = {
    docker.enable = true;
  };

  networking.firewall.enable = false; # Ensure firewall doesn't interfere
}
