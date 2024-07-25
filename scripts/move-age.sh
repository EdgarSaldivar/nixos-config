#!/bin/bash

# Defining the source and destination paths
src="/age.txt"
dest="$HOME/.config/sops/age"

# Check if the source file exists
if [ -f "$src" ]; then
    # Create the destination directory if it doesn't exist
    mkdir -p "$(dirname "$dest")"

    # Move the file
    cp "$src" "$dest"

    echo "File moved successfully."
else
    echo "Source file does not exist."
fi