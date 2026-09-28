. /scripts/backup_remote.sh
mkdir -p /backup/work /backup/mirror
# First listing, before the dump. This run's intermediate inputs start empty, so a stage
# can never read an earlier run's file; the cumulative objects.json is kept.
rm -f /backup/work/objects-1.json /backup/work/objects-2.json /backup/work/refs.txt
list /backup/work/objects-1.json
