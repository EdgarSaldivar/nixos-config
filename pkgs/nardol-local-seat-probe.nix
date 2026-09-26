{ curl, writeShellApplication }:
writeShellApplication {
  name = "nardol-local-seat-probe";
  runtimeInputs = [ curl ];
  text = ''
    set -euo pipefail
    endpoint="''${1:-http://nardol:8002/status}"
    status=0
    response="$(curl -fsS --connect-timeout 1 --max-time 3 "$endpoint" 2>/dev/null)" || status=$?
    case "$status" in
      0)
        printf '%s\n' "$response"
        ;;
      28)
        printf '%s\n' '{"state":"asleep","detail":"nardol status endpoint timed out"}'
        ;;
      6)
        printf '%s\n' '{"state":"degraded","detail":"nardol status hostname could not be resolved"}'
        ;;
      7)
        printf '%s\n' '{"state":"degraded","detail":"nardol status connection was refused"}'
        ;;
      22)
        printf '%s\n' '{"state":"degraded","detail":"nardol status endpoint returned an HTTP error"}'
        ;;
      *)
        printf '%s\n' '{"state":"degraded","detail":"nardol status probe failed"}'
        ;;
    esac
  '';
}
