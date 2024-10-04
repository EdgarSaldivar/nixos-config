# NixOS Multi-Host Configuration

This repository contains a **multi-flake, multi-host NixOS configuration**. It includes scripts and instructions for installing NixOS on remote machines using `nixos-anywhere`.

## Installation

To install the NixOS configuration on a remote machine, use the `nixos-anywhere` script. The following command demonstrates how to do this:

```sh
sudo nix run --impure github:numtide/nixos-anywhere -- --flake 'github:EdgarSaldivar/nixos-config#flake'  root@IP --build-on-remote --copy-host-keys --disk-encryption-keys /tmp/secret.txt ~/path/to/secret.txt
```
example:
```sh
sudo nix run --impure github:numtide/nixos-anywhere -- --flake 'github:EdgarSaldivar/nixos-config#pelargir'  root@192.168.1.121 --build-on-remote --copy-host-keys --disk-encryption-keys /tmp/secret.txt ~/Development/secrets/secret.txt
```

This is intended but not necessary to run with my k3s cluster https://github.com/EdgarSaldivar/k3s-collective . You will need to setup deploy keys with your own cluster repo if you wish to do the same. It is setup to use the same ssh_host_keys that nixos-anywhere is injecting.

## Options

```sh
    --copy-host-keys: Inserts host ID keys from your source /etc/ssh to the target. Using --disk-encryption-keys might also work but will require more arguements.
    --build-on-remote: Required if the source and target machines have different architectures (e.g., x86-linux vs. darwin).
```

## SSH into Initrd for LUKS decryption

SSH into the initrd as root, type the password to decrypt, and continue the boot process.
```sh
    ssh root@IP
```

## Flakes

| **Name**       | **Target System Architecture** | **Target System GPU** |
|----------------|--------------------------------|-----------------------|
| pelargir       | ARM                            | N/A                   |
| minas-tirith   | x86-64                         | Nvidia                |
| osgiliath      | ARM                            | IntegratedARM/CoralUSB|

## Some Notes on the Raspberry Pi Flake

```sh
In order to install the raspberry pi flake u need to be booted from an install medium seperate from the target, i.e. if you are installing to the microsd like the flake then you must be booted from USB or SSD. If its a microSD you will need a microSD nix image.
You may run into ssl errors which is a result of the images date being wrong.
Also in order for USB boot to work you may need to upgrade the custom internal bootloader in the pi's eeprom. This will also let you adjust boot priority among other nice things.
Also do note that the config works with 24.05 and Raspberry Pi 4 and nixos seems to push breaking changes for the way it handles the Pi family.
```