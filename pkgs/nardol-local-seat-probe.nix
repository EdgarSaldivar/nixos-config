{ curl, writeShellApplication }:
writeShellApplication {
  name = "nardol-local-seat-probe";
  runtimeInputs = [ curl ];
  text = ''
    set -euo pipefail
    endpoint="''${1:-http://nardol:8002/status}"
    if ! response="$(curl -fsS --connect-timeout 1 --max-time 3 "$endpoint" 2>/dev/null)"; then
      printf '%s\n' '{"state":"asleep","detail":"nardol status endpoint unreachable"}'
      exit 0
    fi
    printf '%s\n' "$response"
  '';
}
