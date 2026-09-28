# Sourced by every rclone stage of the backup CronJob (POSIX sh: the rclone image is Alpine).
# Brings in the target and lock checks (backup_guard.sh), then builds the one rclone remote
# from the mounted backup key, inside this shell only: nothing on argv, nothing in the Pod
# spec. The key can list and read the bucket, never write it.
. /scripts/backup_guard.sh
export HOME=/tmp RCLONE_CONFIG=/tmp/rclone.conf
: > "$RCLONE_CONFIG"
export RCLONE_CONFIG_GARAGE_TYPE=s3 RCLONE_CONFIG_GARAGE_PROVIDER=Other
export RCLONE_CONFIG_GARAGE_ENDPOINT=http://garage:3900 RCLONE_CONFIG_GARAGE_REGION=us-east-1
export RCLONE_CONFIG_GARAGE_FORCE_PATH_STYLE=true
RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID="$(cat /run/secrets/pin-collector/garage-backup-key-id)"
RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY="$(cat /run/secrets/pin-collector/garage-backup-secret)"
export RCLONE_CONFIG_GARAGE_ACCESS_KEY_ID RCLONE_CONFIG_GARAGE_SECRET_ACCESS_KEY
SOURCE=garage:pin-collector-uploads
# Written whole or not at all: a failed listing never leaves a truncated file behind.
list() {
  rclone lsjson -R --metadata --files-only "$SOURCE" > "$1.tmp"
  mv -f "$1.tmp" "$1"
}
