# Recovering a PostgreSQL Pod that refuses to start after an unclean shutdown

**This will happen again.** The refusal is a deliberate safety gate, not a bug, and
it fires on every unclean stop of a hostPath PostgreSQL — including an ordinary
reboot that does not stop the kubelet gracefully first.

It happened on 2026-08-17: minas rebooted at 13:02, and **both** `immich-postgres14`
and `nextcloud-db` were left mid-flight. Their last checkpoints are four seconds
apart (13:29:37 and 13:29:33) — one event, two databases. Nothing alerted usefully;
it surfaced as `immich.saldivar.io` and `drive.saldivar.io` returning **502** about
ten hours later.

## Recognising it

```
kubectl -n <ns> get pods
# <db-pod>   0/1   Init:CrashLoopBackOff   120
```

The `verify-pgdata` init container is failing. It requires **both**:

```
test ! -e "$PGDATA/postmaster.pid"
test "$cluster_state" = "shut down"
```

An unclean stop breaks both: the pid file is left behind, and the control file still
says `in production`.

⛔ **Do not "fix" the gate.** Its own comment explains why it is this strict: it
cannot distinguish a stale pid from a second postmaster holding the same PGDATA in
another PID/IPC namespace, and *two writers can lose the database* while a refusal is
always recoverable.

## Confirming it is safe to recover

Prove nothing holds the data. All three must agree:

```sh
sudo lsof +D <PGDATA>                 # must be empty
pgrep -a postgres                     # note WHICH cluster each belongs to
sudo k3s crictl ps | grep -i postgres # other databases may legitimately be running
```

On 2026-08-17 `pgrep` showed live postgres processes — they were **authentik** and
**pin-collector**, not the failing databases. Check *which* cluster, not merely
whether any postgres exists.

The current state:

```sh
V=$(sudo cat <PGDATA>/PG_VERSION)
sudo nix shell nixpkgs#postgresql_$V -c pg_controldata <PGDATA> | grep -i 'cluster state'
```

## The recovery

⚠️ **Snapshot first.** `/storage/immich-data` and `/storage2/nextcloud` are
directories on their pools, not their own datasets, so snapshot the pool:

```sh
sudo zfs snapshot storage@immich-pgdata-recovery-$(date -u +%Y%m%dT%H%M%SZ)
```

⛔ **Scale the Deployment to 0 first.** Otherwise the kubelet restarts the Pod the
moment the gate starts passing, and races the manual postgres — which is precisely
the two-writer case the gate exists to prevent.

```sh
kubectl -n <ns> scale deploy <db> --replicas=0
```

Then replay the WAL **in the database's own image**, as a one-off Pod. Do not reach
for a host-installed postgres: the image guarantees the matching build, extensions
and user, and the container already runs as the right uid. (The hostPath PGDATA is
owned by uid 999, which on minas collides with the unrelated local `mandb` user —
running the replay on the host means fighting that for no benefit.)

```yaml
apiVersion: v1
kind: Pod
metadata: { name: pgdata-recover, namespace: <ns> }
spec:
  restartPolicy: Never
  nodeSelector: { kubernetes.io/hostname: minas-tirith }
  containers:
    - name: recover
      image: <THE SAME IMAGE DIGEST THE DEPLOYMENT USES>
      env: [ { name: PGDATA, value: /var/lib/postgresql/data } ]
      command: ["sh","-ec"]
      args:
        - |
          pg_controldata "$PGDATA" | grep -i 'cluster state'
          rm -f "$PGDATA/postmaster.pid"
          gosu postgres pg_ctl -D "$PGDATA" -o "-c listen_addresses=''" -w -t 300 start
          gosu postgres pg_ctl -D "$PGDATA" -m fast -w -t 300 stop
          pg_controldata "$PGDATA" | grep -i 'cluster state'
      volumeMounts: [ { name: pgdata, mountPath: /var/lib/postgresql/data } ]
  volumes:
    - name: pgdata
      hostPath: { path: <THE DEPLOYMENT'S hostPath>, type: Directory }
```

`listen_addresses=''` is load-bearing: it makes the recovery instance unreachable, so
nothing can connect and write during the replay.

Expect `redo starts at …` / `redo done at …`, then `database system is shut down`,
and a final `Database cluster state: shut down`.

Then restore the declared state and let the gate do its job:

```sh
kubectl -n <ns> delete pod pgdata-recover
kubectl -n <ns> scale deploy <db> --replicas=1
kubectl -n <ns> rollout status deploy/<db> --timeout=180s
```

## Verifying

The database Pod should come up **1/1 with 0 restarts** — that is the gate passing,
not being bypassed.

The application in front of it may take a few minutes more and will show restarts
from the outage; that is expected. Watch it recover rather than restarting it:

```sh
kubectl -n nextcloud logs deploy/nextcloud --tail=5   # /status.php 500 -> 200
```

Finish with the recorded baseline, not a spot check:

```sh
# 26/26 expected
python3 hosts/nixos/minas-tirith/scripts/ingress-acceptance.py \
  --baseline hosts/nixos/minas-tirith/baselines/minas-ingress-authentik-baseline-*.txt --public-dns
```

## Prevention

The real fix is not to reboot minas with databases running. There is no graceful
shutdown ordering today that stops the kubelet's Pods before the pools go away —
which is the same gap ROADMAP records for deploy-rs and unattended reboots.

Until then: **check both databases after any minas reboot.** They fail silently for
hours, because a 502 on one hostname is not something anything currently alerts on.
