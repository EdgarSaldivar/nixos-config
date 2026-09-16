# Terracompute operations supervisor

## Boundary

`terracompute-ops` v1 is an observation-only controller for Vast.ai machine
`17049`. Its only NixOS consumer is imladris. The target address is configurable;
the intended tunnel endpoint is `terracompute-observer@10.50.0.2`.
The transport and controller modules are imported with separate disabled latches.
The L2TP/IPsec transport is commissioned first with only two guarded `/32`
routes. The controller remains disabled until that tunnel, the forced-command
helper and its encrypted credentials have been independently verified.

The controller has two network capabilities:

1. open SSH to a pinned host key and an account constrained by a server-side
   forced, read-only probe helper; and
2. send outbound Telegram Bot API notifications.

It has no inbound listener, model client, generic remote-command argument, or
remote mutation action. `observationOnly = true` is a read-only Nix option and an
asserted contract. A later mutation-capable version requires a separate design,
review and recovery gate; it is not an extension point hidden in v1.

## Observation contract

The forced helper emits one normalized JSON object. A probe identifies its
`target`, Vast `machine_id`, `boot_id`, UTC `observed_at`, boolean `healthy`
state, and zero or more events. Each event supplies a `fault_family` and stable
facts such as its code, severity, component, device and message. The controller
rejects a probe whose machine ID is not `17049`.

Xid, PCIe AER and CDI mappings are tables in the Python source. They do not use
probabilistic analysis. Healthy observations create no incident and no analysis
request. A repeated known alert creates neither another incident bundle nor
another notification. Deduplication hashes:

```text
target + boot_id + fault_family + stable_signature
```

The reviewed helper is packaged at
`libexec/terracompute-ops/terracompute-probe`. It correlates NVIDIA-visible GPUs
with the physical PCI inventory. A GPU bound to `vfio-pci` is treated as an
intentional Vast VM assignment; a physical GPU that is absent from
`nvidia-smi` and unbound is an incident. This prevents an eight-GPU host from
being rebooted merely because one rented GPU was passed through to a VM.

The stable signature deliberately excludes raw evidence and counters. A reboot
therefore starts a new incident identity, while repeated samples in one boot do
not create notification storms.

## Durable data

All state is below `/var/lib/imladris/terracompute-ops`, on the dedicated
`imladris-state` filesystem. Nothing is written to `/srv/archive` or a mergerfs
branch.

Each new incident is assembled in a private temporary directory, synchronized,
and atomically renamed into `incidents/`. A bundle contains canonical incident
metadata, redacted and capped evidence, a SHA-256 manifest, and—only for an
unknown or contradictory incident—a bounded `model-analysis-request.json`.
Published bundle files are read-only and are never updated. The model request is
an inert review artifact: v1 has no mechanism that executes it.

SQLite stores the deduplication index and outbound notification outbox. An
outbox row remains pending after delivery failure and receives bounded
exponential backoff. Successful delivery is recorded durably so later timer runs
do not resend it. Transport exceptions are reduced to a fixed error category
because an exception may contain the secret-bearing Telegram URL.

Runtime incident data belongs on the controller and in its protected backups.
Incident bundles, probe captures and notification state must never be committed
to this public repository.

## Credential and trust boundary

The SSH identity, pinned `known_hosts` content, Telegram bot token and Telegram
chat ID are sops-nix secrets delivered through systemd `LoadCredential`. The
program receives only credential file paths; secret values are absent from the
Nix store, command arguments, environment and logs. Secret rotation restarts the
oneshot to refresh systemd's credential snapshots.

OpenSSH is invoked with an empty client config, batch mode, one identity,
`StrictHostKeyChecking=yes`, and the credential-backed `UserKnownHostsFile`.
There is no `accept-new` fallback. Missing network identity inputs or credentials
prevent the observation run.

## External prerequisites

These inputs are intentionally outside repository automation:

- an independently verified SSH host key for machine 17049;
- a tunnel from imladris to the configured target (expected `10.50.0.2`);
- a dedicated terracompute SSH identity whose authorized key is restricted to a
  forced-command, read-only probe helper;
- a Telegram bot token and destination chat ID;
- a separately reviewed model integration if bounded unknown-incident analysis
  is enabled later; and
- protected evidence backup plus a tested restore procedure before any future
  remote mutation capability is considered.

The security argument depends on the forced command being independently
maintained at the target. Client-side omission of a command narrows mistakes but
does not replace that server-side restriction.
