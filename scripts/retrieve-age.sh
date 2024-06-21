#!/bin/sh
  #echo $PATH
  # Check if the age.key already exists
  if [ ! -f /etc/nixos/age.key ]; then
  # Use the 1Password CLI to get the age.key
  eval $(/run/current-system/sw/bin/op account add)
  eval $(/run/current-system/sw/bin/op signin)
  /run/current-system/sw/bin/op get document 'age.key' > /etc/nixos/age.key
  chmod 600 /etc/nixos/age.key
  fi
