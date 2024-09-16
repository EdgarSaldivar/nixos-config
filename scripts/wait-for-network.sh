#!/bin/bash
while ! ip route | grep default &> /dev/null; do
  echo "Waiting for default route..."
  sleep 5
done
echo "Default route is available"