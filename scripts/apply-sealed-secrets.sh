#!/bin/bash

# Variables
REPO_URL="https://github.com/EdgarSaldivar/k3s-collective.git"
REPO_DIR="/tmp/k3s-collective"

# Clone the repository
echo "Cloning the repository..."
git clone "$REPO_URL" "$REPO_DIR"

# Wait for the sealed-secrets controller to be ready
echo "Waiting for sealed-secrets controller to be ready..."
kubectl wait --for=condition=available --timeout=600s deployment/sealed-secrets-controller -n flux-system

# Apply all SealedSecret resources in the /secrets directory
echo "Applying SealedSecret resources..."
for secret in "$REPO_DIR/secrets/*.yaml"; do
  kubectl apply -f "$secret"
done

echo "SealedSecret resources applied successfully."

# Create a flag file to indicate the script has run successfully
touch /etc/sealed-secrets/secrets-applied.flag

# Clean up
rm -rf "$REPO_DIR"