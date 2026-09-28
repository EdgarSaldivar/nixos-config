. /scripts/backup_remote.sh
# After the dump: objects written while it ran are listed and copied too, so every key the
# dump can reference is in the mirror. Still copy, never sync.
list /backup/work/objects-2.json
rclone copy "$SOURCE" /backup/mirror --stats-one-line -v
