#!/bin/bash
while ! kubectl get nodes &> /dev/null; do
  echo "Waiting for k3s to be ready..."
  sleep 5
done
echo "k3s is ready"