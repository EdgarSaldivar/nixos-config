#!/bin/bash

echo "Running move-age.sh script..."

# Defining the source and destination paths
src="/keys.txt"
dest="/home/edgar/.config/sops/age"
default_dest="/home/edgar/.config/sops/age/keys.txt"

# Create the destination directory if it doesn't exist
mkdir -p "$(dirname "$dest")"

# Set permissions for the source file
chmod 644 "$src"

# Move the file
mv "$src" "$dest" 

# Set permissions for the destination directory and file
chmod 700 "$(dirname "$dest")"
chmod 600 "$dest"

echo "File moved successfully."

# Reset the location back to the default
mv "$dest" "$default_dest"

# Set permissions for the default location
chmod 700 "$(dirname "$default_dest")"
chmod 600 "$default_dest"

echo "Location reset to default successfully."
