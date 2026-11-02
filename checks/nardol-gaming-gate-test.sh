#!/usr/bin/env bash
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
gate=${GATE_SCRIPT:-"$root/hosts/nixos/nardol/inference-gaming-gate.sh"}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

printf '#!%s\n' "$(command -v bash)" > "$tmp/systemctl"
cat >> "$tmp/systemctl" <<'EOF'
set -eu
[ "$1" = show ] && [ "$2" = --property=ActiveState ] && [ "$3" = --value ]
case "$4" in
  nardol-gaming.target)
    [ "${FAIL_TARGET:-0}" = 0 ] || exit 1
    printf '%s\n' "${TARGET_STATE-inactive}"
    ;;
  nardol-gpu-handover.service)
    [ "${FAIL_HANDOVER:-0}" = 0 ] || exit 1
    printf '%s\n' "${HANDOVER_STATE-inactive}"
    ;;
  *) exit 2 ;;
esac
EOF
chmod +x "$tmp/systemctl"

expect() {
  want=$1 label=$2
  shift 2
  if env -i PATH="$PATH" "$@" bash "$gate" "$tmp/systemctl" >"$tmp/out" 2>&1; then
    got=0
  else
    got=1
  fi
  if [ "$got" != "$want" ]; then
    printf 'FAIL: %s: exit %s, expected %s: %s\n' "$label" "$got" "$want" "$(cat "$tmp/out")" >&2
    exit 1
  fi
  printf 'ok: %s\n' "$label"
}

expect 0 'boot or serving: both units inactive'
expect 1 'gaming target active' TARGET_STATE=active
expect 1 'gaming target activating' TARGET_STATE=activating
expect 1 'gaming target deactivating' TARGET_STATE=deactivating
expect 1 'handover activating before inference stop' HANDOVER_STATE=activating
expect 1 'handover active while target start is queued' HANDOVER_STATE=active
expect 1 'failed handover cannot release the claim' HANDOVER_STATE=failed
expect 1 'target state unreadable' FAIL_TARGET=1
expect 1 'handover state unreadable' FAIL_HANDOVER=1
expect 1 'empty state is ambiguous' TARGET_STATE=
