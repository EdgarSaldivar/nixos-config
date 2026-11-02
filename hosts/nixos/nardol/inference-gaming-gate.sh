#!/usr/bin/env bash
# ExecCondition for docker-ikllama. Exit 1 skips the start without marking the
# unit failed. The systemctl path is an argument so the same gate can be tested
# with a fake manager; the unit passes its pinned systemd binary.
set -eu
systemctl=$1

for unit in nardol-gaming.target nardol-gpu-handover.service; do
  # Any unreadable or ambiguous state must not give inference the GPU. In
  # particular, handover is "activating" before its stop has completed, while
  # the gaming target's start job may still report "inactive".
  state=$("$systemctl" show --property=ActiveState --value "$unit" 2>/dev/null) || exit 1
  if [ "$state" != inactive ]; then
    echo "inference start skipped: $unit is ${state:-unknown}" >&2
    exit 1
  fi
done
