. /scripts/backup_remote.sh
# copy, never sync: nothing leaves the mirror before a snapshot holds it.
rclone copy "$SOURCE" /backup/mirror --stats-one-line -v
