# pelargir — Raspberry Pi 5 home-automation control plane.
{ inputs, ... }:
{
  # No `_module.args.nixos-raspberrypi` here, deliberately. `_module.args` is
  # part of `config`, so a module cannot use it to supply an argument that
  # imported modules need while their own import graph is still being resolved —
  # that recurses. The framework's `nixosSystem` wrapper passes the argument
  # through specialArgs instead, and flake.nix routes pelargir through that
  # wrapper via mkNixos's `builder`. (Raised in review 2026-08-03; the wrapper
  # is also what makes `raspberrypi-utils` and the vendor kernel resolve at all
  # — without it evaluation fails outright.)
  imports = with inputs.nixos-raspberrypi.nixosModules; [
    # Verified at nixos-raspberrypi 67616c2: base supplies Pi 5 firmware,
    # kernel, initrd hardware support, and loader infrastructure. No SD-image
    # or display module belongs on this headless disko-installed NVMe host.
    # `trusted-nix-caches` is deliberately NOT imported: the wrapper adds it
    # already (`trustCaches ? true`).
    raspberry-pi-5.base

    # Bluetooth is deliberately separate from base at 67616c2. Import it: the
    # module enables BlueZ plus krnbt, and Home Assistant consumes that host
    # bluetoothd over its read-only /run/dbus mount.
    raspberry-pi-5.bluetooth

    ./disko.nix
    ./boot.nix
    ./system.nix
    ./secrets.nix
    ./wireguard.nix
    ./k3s.nix
    ./manifests.nix
    ./backup.nix
    ./monitoring.nix
    ./tang.nix
    ../../../modules/nixos/fleet/disk-health.nix
    ../../../users/edgar/default.nix
  ];

  fleet.diskHealth = {
    enable = true;
    hostId = "pelargir";
  };
}
