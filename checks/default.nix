ctx: {
  wolf-reconciler = import ./wolf-reconciler.nix ctx;
  hostnames = import ./hostnames.nix ctx;
  docs-contract = import ./docs-contract.nix ctx;
  external-checkout-dependency = import ./external-checkout-dependency.nix ctx;
  minas-shell-fixtures = import ./minas-shell-fixtures.nix ctx;
  minas-unit-contract = import ./minas-unit-contract.nix ctx;
  manifest-objects = import ./manifest-objects.nix ctx;
  cloudflare-ranges = import ./cloudflare-ranges.nix ctx;
  workload-selectors = import ./workload-selectors.nix ctx;
  pin-collector-release-contract = import ./pin-collector-release-contract.nix ctx;
  pin-collector-secret-contract = import ./pin-collector-secret-contract.nix ctx;
  pin-collector-postgres-data-contract = import ./pin-collector-postgres-data-contract.nix ctx;
  secret-applier-contract = import ./secret-applier-contract.nix ctx;
  fleet-disk-health = import ./fleet-disk-health.nix ctx;
  nardol-disko-targets = import ./nardol-disko-targets.nix ctx;
  nardol-unlock-contract = import ./nardol-unlock-contract.nix ctx;
  nardol-gaming-contract = import ./nardol-gaming-contract.nix ctx;
  pelargir-tang-contract = import ./pelargir-tang-contract.nix ctx;
  minas-tirith-disko-targets = import ./minas-tirith-disko-targets.nix ctx;
  pelargir-disko-targets = import ./pelargir-disko-targets.nix ctx;
  osgiliath-disko-targets = import ./osgiliath-disko-targets.nix ctx;
}
