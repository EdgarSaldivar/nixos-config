#!/bin/bash

# Defining the source and destination paths
src="/age.txt"
dest="$HOME/.config/sops/age/keys.txt"

# Check if the source file exists
if [ -f "$src" ]; then
    # Create the destination directory if it doesn't exist
    mkdir -p "$(dirname "$dest")"

    # Move the file
    mv "$src" "$dest"

    echo "File moved successfully."
else
    echo "Source file does not exist."
fi
