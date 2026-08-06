# Copy immutable manifests from the store and rendered secret manifests from
# /run. k3s continuously reconciles this directory; replacing files is enough.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  plainManifests = pkgs.linkFarm "pelargir-k3s-manifests" [
    {
      name = "namespace.yaml";
      path = ./manifests/namespace.yaml;
    }
    {
      name = "mosquitto.yaml";
      path = ./manifests/mosquitto.yaml;
    }
    {
      name = "zigbee2mqtt.yaml";
      path = ./manifests/zigbee2mqtt.yaml;
    }
    {
      name = "home-assistant.yaml";
      path = ./manifests/home-assistant.yaml;
    }
    {
      name = "ddns.yaml";
      path = ./manifests/ddns.yaml;
    }
    {
      name = "ingress.yaml";
      path = ./manifests/ingress.yaml;
    }
    {
      name = "osgiliath-namespace.yaml";
      path = ../osgiliath/manifests/namespace.yaml;
    }
    {
      name = "osgiliath-frigate.yaml";
      path = ../osgiliath/manifests/frigate.yaml;
    }
    {
      name = "osgiliath-home-assistant.yaml";
      path = ../osgiliath/manifests/home-assistant.yaml;
    }
    {
      name = "osgiliath-mosquitto.yaml";
      path = ../osgiliath/manifests/mosquitto.yaml;
    }
    {
      name = "osgiliath-edge.yaml";
      path = ../osgiliath/manifests/edge.yaml;
    }
  ];
in
{
  systemd.tmpfiles.rules = [
    "d /var/lib/rancher/k3s/server/manifests 0700 root root -"
  ];

  system.activationScripts.pelargir-k3s-manifests = lib.stringAfter [ "setupSecrets" ] ''
    install -d -m 0700 /var/lib/rancher/k3s/server/manifests
    for source in ${plainManifests}/*.yaml; do
      install -m 0600 "$source" "/var/lib/rancher/k3s/server/manifests/$(basename "$source")"
    done
    install -m 0600 \
      ${config.sops.templates."pelargir-home-secrets.yaml".path} \
      /var/lib/rancher/k3s/server/manifests/pelargir-home-secrets.yaml
  '';
}
