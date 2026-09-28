# Runs with bash in the PostgreSQL image (the live server's own image, so the client
# matches the server's major version).
set -euo pipefail
. /scripts/backup_guard.sh
export HOME=/tmp
# libpq ignores a password file that group or others can read.
umask 077
# PGPASSFILE lives in a memory-backed emptyDir; the password never reaches argv or env.
# Fields are host:port:database:user:password, with `\` and `:` escaped by a backslash.
pgpass=/work-secret/pgpass
password="$(cat /run/secrets/pin-collector/postgres-password)"
password="${password//\\/\\\\}"
password="${password//:/\\:}"
printf 'postgres:5432:pin_collector:pin_collector:%s\n' "$password" > "$pgpass"
unset password
export PGPASSFILE="$pgpass" PGCONNECT_TIMEOUT=30

mkdir -p /backup/work/db
dump=/backup/work/db/pin_collector.dump
rm -f "$dump.partial"
pg_dump -Fc -h postgres -U pin_collector -d pin_collector -f "$dump.partial"
# A dump pg_restore cannot read is not a backup.
pg_restore --list "$dump.partial" > /dev/null
mv -f "$dump.partial" "$dump"
rm -f "$pgpass"

# Every object key the dump can reference, in the three shapes the app stores. A name
# segment stops at a quote, whitespace, comma, backslash (COPY text escapes) or slash.
uuid='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
name="[^/\"',[:space:]\\\\]+"
keys="uploads/$uuid/$name|internal/golden-truth/$uuid/$name|internal/crop-evidence/[0-9a-fA-F]{64}/$name"
# grep exits 1 when nothing matches, which is a valid (empty) answer, not a failure.
pg_restore -f - "$dump" | { grep -oE "$keys" || [ $? -eq 1 ]; } | LC_ALL=C sort -u > /backup/work/refs.txt.tmp
mv -f /backup/work/refs.txt.tmp /backup/work/refs.txt
echo "pg-dump: $(stat -c %s "$dump") bytes, $(wc -l < /backup/work/refs.txt) referenced keys"
