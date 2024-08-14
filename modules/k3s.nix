{ config, pkgs, ... }:

#Currently this is will be server but this will be an agent soon.
{
  environment.systemPackages = with pkgs;
    [
      k3s
      docker
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
    ];
  };
/*
  services.flux = {
    enable = true;
    gitUrl = "https://github.com/yourusername/your-k3s-config-repo.git";
    gitBranch = "main";
  };
*/
  virtualisation = {
    podman.enable = true;
    docker.enable = true;
  };

  networking.firewall.enable = false; # Ensure firewall doesn't interfere
}
