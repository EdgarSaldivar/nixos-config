#!/bin/sh
  # Check if the age.key already exists
  if [ ! -f /etc/nixos/age.key ]; then
  # Use the 1Password CLI to get the age.key
  eval $(op signin my.1password.com)
  op get document 'age.key' > /etc/nixos/age.key
  chmod 600 /etc/nixos/age.key
  fi
