#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'smoke test failed: %s\n' "$*" >&2
  exit 1
}

for executable in \
  7z cabextract desktop-file-validate innoextract kitty ludusavi nardol-modctl \
  nano protontricks protontricks-launch rsync strace winetricks xdelta3 yad; do
  command -v "${executable}" >/dev/null 2>&1 || fail "missing ${executable}"
done

protontricks --version | grep -Fq '1.14.1' || fail 'unexpected Protontricks version'
7z | grep -Fq '7-Zip (z) 26.02' || fail 'unexpected 7-Zip version'
winetricks --version | grep -Fq '20260125' || fail 'unexpected Winetricks version'
ludusavi --version | grep -Fq '0.31.0' || fail 'unexpected Ludusavi version'
[[ "$(readlink -f /usr/bin/zenity)" == /usr/bin/true ]] || fail 'upstream Zenity shim was replaced'
[[ "${PROTONTRICKS_GUI:-}" == yad ]] || fail 'Protontricks is not configured to use YAD'
desktop-file-validate /usr/share/applications/nardol-mod-tools.desktop \
  || fail 'invalid desktop entry'
sha256sum --check --strict \
  /usr/local/share/nardol-steam-tools/dpkg-manifest.sha256 >/dev/null \
  || fail 'Debian package manifest changed'

fixture="$(mktemp -d)"
probe_pid=''
cleanup() {
  if [[ -n "${probe_pid}" ]]; then
    kill "${probe_pid}" >/dev/null 2>&1 || true
    wait "${probe_pid}" 2>/dev/null || true
  fi
  rm -rf "${fixture}"
}
trap cleanup EXIT

mkdir -p \
  "${fixture}/steam/steamapps/common/Fixture Game" \
  "${fixture}/steam/steamapps/compatdata/424242/pfx/drive_c" \
  "${fixture}/mods/downloads"
printf '%s\n' \
  '"AppState"' \
  '{' \
  '  "appid"      "424242"' \
  '  "name"       "Nardol Fixture"' \
  '  "installdir" "Fixture Game"' \
  '}' >"${fixture}/steam/steamapps/appmanifest_424242.acf"
touch "${fixture}/mods/downloads/installer.exe"

tool_env=(
  env
  "STEAM_DIR=${fixture}/steam"
  "NARDOL_MOD_ROOT=${fixture}/mods"
)

"${tool_env[@]}" nardol-modctl list | grep -Fq $'424242\tNardol Fixture' \
  || fail 'fixture manifest was not listed'
"${tool_env[@]}" nardol-modctl paths 424242 \
  | grep -Fq "${fixture}/steam/steamapps/common/Fixture Game" \
  || fail 'fixture game path was not resolved'
if "${tool_env[@]}" nardol-modctl paths invalid >/dev/null 2>&1; then
  fail 'invalid APPID was accepted'
fi

env SteamAppId=424242 sleep 30 &
probe_pid=$!
for _ in {1..20}; do
  if "${tool_env[@]}" nardol-modctl running 424242 >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
"${tool_env[@]}" nardol-modctl running 424242 >/dev/null \
  || fail 'SteamAppId process was not detected'
set +e
mutation_output="$("${tool_env[@]}" nardol-modctl run 424242 "${fixture}/mods/downloads/installer.exe" 2>&1)"
mutation_status=$?
set -e
[[ ${mutation_status} -eq 3 ]] || fail "running-game guard returned ${mutation_status}, expected 3"
grep -Fq 'refusing concurrent prefix mutation' <<<"${mutation_output}" \
  || fail 'running-game guard did not explain the refusal'

kill "${probe_pid}" >/dev/null 2>&1 || true
wait "${probe_pid}" 2>/dev/null || true
probe_pid=''

# Exercise the wrapper's argument construction without touching a real prefix.
# Each stub prints one argument per line so ordering and quoting stay visible.
mkdir -p "${fixture}/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "<%s>\\n" "$@"' >"${fixture}/bin/tool-stub"
chmod 0755 "${fixture}/bin/tool-stub"
ln -s tool-stub "${fixture}/bin/protontricks"
ln -s tool-stub "${fixture}/bin/protontricks-launch"
ln -s tool-stub "${fixture}/bin/ludusavi"
stub_env=(
  env
  "PATH=${fixture}/bin:${PATH}"
  "STEAM_DIR=${fixture}/steam"
  "NARDOL_MOD_ROOT=${fixture}/mods"
)

gui_output="$("${stub_env[@]}" nardol-modctl gui 424242)"
[[ "${gui_output}" == $'<--gui>\n<424242>' ]] || fail 'GUI APPID arguments changed'
if "${stub_env[@]}" nardol-modctl gui >/dev/null 2>&1; then
  fail 'unguarded GUI game picker was accepted'
fi
apply_output="$("${stub_env[@]}" nardol-modctl apply 424242 vcrun2022)"
[[ "${apply_output}" == $'<424242>\n<vcrun2022>' ]] || fail 'Winetricks arguments changed'
shell_output="$("${stub_env[@]}" nardol-modctl shell 424242)"
[[ "${shell_output}" == $'<-c>\n<exec /bin/bash>\n<424242>' ]] || fail 'prefix shell arguments changed'
run_output="$("${stub_env[@]}" nardol-modctl run 424242 "${fixture}/mods/downloads/installer.exe" '--fixture argument')"
[[ "${run_output}" == "$(printf '<--appid>\n<424242>\n<%s>\n<--fixture argument>' "${fixture}/mods/downloads/installer.exe")" ]] \
  || fail 'installer arguments changed'
backup_output="$("${stub_env[@]}" nardol-modctl backup 424242)"
[[ "${backup_output}" == "$(printf '<backup>\n<--force>\n<--no-cloud-sync>\n<--path>\n<%s>\n<--wine-prefix>\n<%s>\n<-->\n<Nardol Fixture>' \
  "${fixture}/mods/backups/ludusavi" \
  "${fixture}/steam/steamapps/compatdata/424242/pfx")" ]] \
  || fail 'Ludusavi arguments changed'

"${tool_env[@]}" nardol-modctl doctor 424242 >/dev/null \
  || fail 'fixture diagnostics failed'

printf 'nardol-steam-tools smoke test passed\n'
