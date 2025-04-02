{ config, lib, ... }:
{
  fileSystems."/".fsType = lib.mkForce "bcachefs";
}