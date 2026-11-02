#!/usr/bin/env bash
set -Eeuo pipefail

test -x /entrypoint.sh
test -x /opt/gow/startup.sh
test -x /opt/gow/launch-comp.sh
test -x /opt/gow/startup-app.sh
test -x /opt/eglantine/AppRun
test "$(readlink -f /opt/gow/startup-app.sh)" = /opt/gow/startup-app.sh
grep -q 'exec gosu "${UNAME}" /opt/gow/startup.sh' /entrypoint.sh
grep -q 'exec /opt/gow/startup-app.sh' /opt/gow/startup.sh
grep -q 'launcher /usr/local/bin/nardol-eglantine' /opt/gow/startup-app.sh
grep -q 'check_for_updates=false' /opt/gow/startup-app.sh
ldconfig -p | grep 'libvulkan.so.1' >/dev/null
unexpected="$(find /opt/eglantine -type f \( -iname '*.nsp' -o -iname '*.xci' -o -iname 'prod.keys' -o -iname 'title.keys' \) -print -quit)"
test -z "${unexpected}"

export LD_LIBRARY_PATH="/opt/eglantine/usr/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
! ldd /opt/eglantine/AppRun | grep 'not found'
# This release initializes SDL even for --version and exits 1 without a ROM.
# Require that exact frontend result, so loader/ABI failures cannot pass.
status=0
output="$(/opt/eglantine/usr/bin/eden-cli 2>&1)" || status=$?
test "${status}" = 1
printf '%s' "${output}" | grep 'Failed to load ROM: No ROM specified' >/dev/null
