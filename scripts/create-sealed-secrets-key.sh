#!/bin/bash

# Path to the private key file
private_key_path="/sealed-secrets-key.pem"

# Encode the private key in base64
private_key_base64=$(base64 -w 0 "$private_key_path")

# Create the Kubernetes Secret manifest
cat <<EOF > /etc/sealed-secrets/sealed-secrets-key.yaml
apiVersion: v1
kind: Secret
metadata:
  name: sealed-secrets-key
  namespace: flux-system
  labels:
    sealedsecrets.bitnami.com/sealed-secrets-key: "true"
type: Opaque
data:
  tls.key: $private_key_base64
EOF

# Apply the Kubernetes Secret
kubectl apply -f /etc/sealed-secrets/sealed-secrets-key.yaml

# Create a flag file to indicate the script has run successfully
touch /etc/sealed-secrets/key-created.flag