# NixOS Multi-Host Configuration

This repository contains a **multi-flake, multi-host NixOS configuration**. It includes scripts and instructions for installing NixOS on remote machines using `nixos-anywhere`.

## Installation

To install the NixOS configuration on a remote machine, use the `nixos-anywhere` script. The following command demonstrates how to do this:

```sh
sudo nix run --impure github:numtide/nixos-anywhere -- --flake '.#pelargir' root@192.168.6.209 --build-on-remote --copy-host-keys --extra-files ~/Development/secrets/age --disk-encryption-keys /tmp/secret.txt ~/Development/secrets/secret.txt



    --copy-host-keys: Inserts host ID keys from your source /etc/ssh to the target. Using --disk-encryption-keys might also work.
    --build-on-remote: Required if the source and target machines have different architectures (e.g., x86-linux vs. darwin).


SSH into Initrd

SSH into the initrd as root, type the password to decrypt, and continue the boot process.

Flakes

Name            Target System Architecture	Target System GPU
pelargir	    ARM	                        N/A
minas-tirith	x86-64	                    Nvidia