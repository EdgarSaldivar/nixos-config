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
The flake is designed to generate an image that gets flashed onto the microsd of the pi. The image is generated via disko image generator command listed below.

    sudo nix build .#nixosConfigurations.pelargir.config.system.build.diskoImagesScript --impure --system aarch64-linux

Another thing to note is that it must be built on either x86 or aarch64-linux, or a remote builder on macos. Binfmt allows for cross compiling but it is linux kernel only. As such I know of no method do allow for direct on darwin building...
```

## Setup Bitfmt + Remote Builder
```sh
A short explanation on how to get it working on Macos w/ docker. Can easily be applied for non docker VMs though.

Enable bitfmt in docker:

    docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

Throw up nixos VM with the directory mapping (for the config):

    docker run --rm -it -v "$(pwd)":/mnt -w /mnt nixos/nix:latest bash

Edit your nixos conf for the remote builder:

    sudo nano /etc/nix/nix.conf
    builders = ssh://username@container-ip

Build the image:

    sudo nix build .#nixosConfigurations.pelargir.config.system.build.diskoImagesScript --impure --system aarch64-linux


```