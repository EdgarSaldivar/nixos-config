# Runs in the restic image (POSIX sh). RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE and
# RESTIC_CACHE_DIR come from the container's env; the password is only ever a file.
. /scripts/backup_guard.sh
export HOME=/tmp
if [ ! -f "$RESTIC_REPOSITORY/config" ]; then
  restic init
else
  # Plain `unlock` removes only STALE repository locks (restic refreshes a live one every
  # few minutes), such as one left by a backup pod that was killed. This pod holds the
  # pipeline flock, so no other backup is running; a live lock (a restore drill's) stays.
  restic unlock
fi

# Only a snapshot that restic finished without errors counts: it is tagged `complete`,
# and retention and restores consider only those. An incomplete one (exit 3: some file
# could not be read) is forgotten at once and the stage fails, so the Job retries.
rc=0
restic backup --json --host pincollector-pelargir /backup/work /backup/mirror > /tmp/restic-backup.json || rc=$?
# No pipeline (POSIX sh has no pipefail): grep then sed on files.
grep -v '"message_type":"status"' /tmp/restic-backup.json > /tmp/restic-backup.log || true
cat /tmp/restic-backup.log
snapshot=$(grep '"message_type":"summary"' /tmp/restic-backup.log | sed -n 's/.*"snapshot_id":"\([0-9a-f]*\)".*/\1/p' | tail -n 1)
case "$rc" in
  0)
    if [ -z "$snapshot" ]; then
      echo "restic backup succeeded but reported no snapshot id" >&2
      exit 1
    fi
    restic tag --add complete "$snapshot"
    ;;
  3)
    if [ -n "$snapshot" ]; then
      restic forget "$snapshot"
    fi
    echo "restic backup was incomplete (exit 3); snapshot ${snapshot:-<none>} forgotten" >&2
    exit 1
    ;;
  *)
    echo "restic backup failed (exit $rc)" >&2
    exit 1
    ;;
esac
# No hard maximum age: 14 daily, 8 weekly and 6 monthly complete snapshots, nominally ~6
# months. Untagged snapshots are not in the policy (see the runbook).
restic forget --host pincollector-pelargir --tag complete --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune
# Structure every night; a quarter of the data blobs on the 1st of each month.
if [ "$(date -u +%d)" = 01 ]; then
  restic check --read-data-subset=1/4
else
  restic check
fi
