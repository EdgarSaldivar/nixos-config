# Sourced first by every shell stage of the backup CronJob (POSIX sh; also runs under bash).
# Refuses unless
#   - /backup is the directory pelargir prepared: pincollector-backup.nix writes the
#     directory's device:inode into the sentinel on every boot, and a bind mount keeps
#     both, so a sentinel that survived on some other directory does not match; and
#   - this pod holds the pipeline lock: the `lock` sidecar (backup_lock.py) wrote exactly
#     "acquired" to the shared lock-state file.
set -eu
set +x
guard_refuse() {
  echo "refusing: $*" >&2
  exit 1
}
guard_sentinel=/backup/.pincollector-backup-target
if [ -L "$guard_sentinel" ] || [ ! -f "$guard_sentinel" ]; then
  guard_refuse "/backup is not the prepared PinCollector backup target"
fi
guard_expected=$(cat "$guard_sentinel")
guard_actual=$(stat -c %d:%i /backup)
if [ "$guard_expected" != "$guard_actual" ]; then
  guard_refuse "/backup is $guard_actual, the prepared target is ${guard_expected:-<empty>}"
fi
guard_state=$(cat /run/lock-state/state 2>/dev/null || true)
if [ "$guard_state" != acquired ]; then
  guard_refuse "the backup lock is not held by this pod (lock state: ${guard_state:-none})"
fi
