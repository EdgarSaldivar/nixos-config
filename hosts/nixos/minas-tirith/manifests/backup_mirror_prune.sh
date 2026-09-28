. /scripts/backup_remote.sh
# Runs only after restic has snapshotted the mirror, so a deletion reaches the mirror only
# once the deleted object is in history. Then records the mirror's keys; the next run's
# snapshot carries that record, and its check prunes inventory entries for keys that are
# neither mirrored nor referenced.
rclone sync "$SOURCE" /backup/mirror --stats-one-line -v
# No pipeline: POSIX sh has no pipefail, and a failed find must fail the stage.
(cd /backup/mirror && find . -type f) > /backup/work/mirror-keys.txt.find
sed 's|^\./||' /backup/work/mirror-keys.txt.find | LC_ALL=C sort > /backup/work/mirror-keys.txt.tmp
rm -f /backup/work/mirror-keys.txt.find
mv -f /backup/work/mirror-keys.txt.tmp /backup/work/mirror-keys.txt
