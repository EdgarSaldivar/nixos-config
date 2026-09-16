# Reviewable concrete files and service options only. Global enablement remains
# false, and the importing fleet must map its existing sops-nix secret paths to
# each role's exact credential names before separately enabling anything.
{ ... }:
{
  environment.etc = {
    "terracompute-ops/collector.json" = {
      source = ./collector.json;
      user = "root";
      group = "root";
      mode = "0444";
    };
    "terracompute-ops/notifier.json" = {
      source = ./notifier.json;
      user = "root";
      group = "root";
      mode = "0444";
    };
    "terracompute-ops/operator-input.json" = {
      source = ./operator-input.json;
      user = "root";
      group = "root";
      mode = "0444";
    };
    "terracompute-ops/webhook.json" = {
      source = ./webhook.json;
      user = "root";
      group = "root";
      mode = "0444";
    };
    "terracompute-ops/bmc-username" = {
      source = ./bmc-username;
      user = "root";
      group = "root";
      mode = "0444";
    };
    "terracompute-ops/bmc-cert-sha256" = {
      source = ./bmc-cert-sha256;
      user = "root";
      group = "root";
      mode = "0444";
    };
  };

  services.terracomputeOps = {
    enable = false;
    collector = {
      enable = true;
      configFile = "/etc/terracompute-ops/collector.json";
    };
    notifier = {
      enable = true;
      configFile = "/etc/terracompute-ops/notifier.json";
    };
    operatorInput = {
      enable = true;
      configFile = "/etc/terracompute-ops/operator-input.json";
    };
    webhook = {
      enable = true;
      configFile = "/etc/terracompute-ops/webhook.json";
    };
  };
}
