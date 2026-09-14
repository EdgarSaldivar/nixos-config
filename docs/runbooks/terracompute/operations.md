# Terracompute observation operations

This runbook operates the configured observation-only supervisor. It does not
authorize a deployment, target mutation, model execution, or secret disclosure.

## Commissioning prerequisites

Before activating the imladris module:

1. Establish the tunnel from imladris to machine 17049. Configure
   `services.terracomputeOps.sshTarget` if its endpoint differs from
   `terracompute-observer@10.50.0.2`.
2. Verify the target SSH host key through an independent trusted channel. Build a
   minimal `known_hosts` file for the configured hostname/address; do not obtain
   trust by connecting with `accept-new` or disabled host-key checking.
3. Install a distinct SSH public key on the target account. Constrain it in
   `authorized_keys` to the reviewed read-only probe helper with a forced command,
   and disable forwarding, PTY allocation and user-controlled commands.
4. Create the Telegram bot and determine the outbound destination chat ID.
5. Add these four encrypted values to `secrets/imladris.yaml` using the existing
   sops workflow; never place their plaintext in Nix or shell arguments:
   `terracompute-ssh-identity`, `terracompute-known-hosts`,
   `terracompute-telegram-bot-token`, and
   `terracompute-telegram-chat-id`.
6. Set `services.terracomputeOps.enable = true` only after the preceding inputs
   are independently verified.

The Nix definitions remain inactive while the module is disabled. Add the four
encrypted keys and enable the module together; each definition wires rotation to
`terracompute-ops.service`.

Model integration is not a commissioning prerequisite for v1. Unknown-event
analysis request files remain inert. Do not add a model credential merely to
silence or consume them.

## Validate configuration before activation

From the repository checkout, run the repository gates described in
`AGENTS.md`. Confirm that the diff changes imladris only and does not import the
module from pelargir. Review the evaluated unit for all of these properties:

- machine ID is exactly `17049` and `observationOnly` is true;
- state is `/var/lib/imladris/terracompute-ops` and the unit requires the
  `/var/lib/imladris` mount;
- all four inputs use `LoadCredential`;
- SSH uses `StrictHostKeyChecking=yes` and the credential-backed pinned host-key
  file; and
- the timer and service have bounded execution times.

Follow the repository's imladris rebuild procedure. This runbook does not record
whether any particular deployment or service run has occurred.

## Inspect an observation failure

1. Read the unit status and journal for `terracompute-ops.service`. The program
   intentionally logs only error categories, not remote stderr or credential
   values.
2. Confirm `/var/lib/imladris` is the dedicated mounted state filesystem before
   inspecting its `terracompute-ops` child.
3. Check that each configured sops secret was installed as a runtime file. Inspect
   existence, ownership and size only; do not print values.
4. Verify the tunnel route and independently compare the expected host-key
   fingerprint with the credential source. Treat a mismatch as an identity
   failure; never bypass it.
5. Confirm the forced helper emits normalized JSON with machine ID `17049` and a
   nonempty boot ID. Repair the helper or tunnel at its owner rather than adding a
   generic command escape hatch to the controller.

## Inspect incidents and notification retries

Incident directories are append-only evidence. Verify a bundle from inside its
directory with `sha256sum -c manifest.sha256`. Do not edit a bundle to make the
check pass. The SQLite database is live supervisor state; take a consistent copy
before offline inspection and do not hand-edit retry timestamps or delivery
state.

A Telegram outage leaves rows pending and the next bounded timer run retries them
after exponential backoff. If delivery is failing, validate bot/chat ownership
and rotate the sops values as needed. Rotation restarts the oneshot; no plaintext
value should appear in the command line, environment, journal, or repository.

## Backup and recovery boundary

Back up the state database and complete incident directories together to
protected storage, and perform a restore exercise that verifies their manifests.
Do this before designing or enabling any mutation-capable successor. Evidence
that has no tested restore path is not an adequate safety record for automated
remediation.

Inbound Telegram commands, model execution and remote remediation are absent in
v1. Requests for any of them require a new architecture decision and explicit
authorization; they are not incident-response shortcuts.
