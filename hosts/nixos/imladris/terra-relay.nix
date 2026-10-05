# Import into imladris's NixOS configuration only during an approved deployment.
# Use a stable LAN address. The upstream travels over the existing management VPN.
{ ... }:
{
  services.nginx.enable = true;
  services.nginx.virtualHosts."terra-monitor" = {
    listen = [
      {
        addr = "10.0.0.131";
        port = 8080;
      }
    ];
    # Five frame requests per second should not produce continuous SD-card logs.
    extraConfig = "access_log off;";
    locations."/" = {
      proxyPass = "http://10.50.0.2:8088";
      extraConfig = ''
        proxy_connect_timeout 3s;
        proxy_read_timeout 10s;
        proxy_send_timeout 10s;
        proxy_buffering off;
        proxy_cache off;
      '';
    };
  };
  networking.firewall.interfaces.lan0.allowedTCPPorts = [ 8080 ];
  systemd.services.nginx.wants = [ "network-online.target" ];
  systemd.services.nginx.after = [ "network-online.target" ];
}
