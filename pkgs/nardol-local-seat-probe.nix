{ curl, writeShellApplication }:
writeShellApplication {
  name = "nardol-local-seat-probe";
  runtimeInputs = [ curl ];
  text = ''
    set -euo pipefail
    endpoint="''${1:-http://nardol:8002/status}"
    status=0
    response="$(curl -fsS --connect-timeout 1 --max-time 3 "$endpoint" 2>/dev/null)" || status=$?
    if (( status == 22 )); then
      printf '%s\n' '{"state":"degraded","detail":"nardol status endpoint returned an HTTP error"}'
      exit 0
    fi
    if (( status != 0 )); then
      printf '%s\n' '{"state":"asleep","detail":"nardol status endpoint unreachable"}'
      exit 0
    fi
    printf '%s\n' "$response"
  '';
}
