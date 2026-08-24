# Live handoff — `fleet-restructure`

Audited 2026-08-24. This is the one live handoff permitted by the documentation
contract; completed migration history remains behind `pre-doc-cleanup-2026-08` and
in git history.

## Scope and baseline

The takeover baseline is the existing `fleet-restructure` branch, not `master`.
The branch already contained functional migrations and fixes that had been applied
to the fleet, so treating `master` as the desired configuration would have risked
reverting live expectations. The takeover work begins after the Palworld commit
`963cc8a`.

No host was deployed, switched, restarted, or mutated during this restructure.
No secret value was read or changed.

The Palworld source contract remains:

```yaml
BASE_CAMP_WORKER_MAX_NUM: "40"
BASE_CAMP_MAX_NUM_IN_GUILD: "10"
```

Both values are declared in
[`hosts/nixos/minas-tirith/manifests/palworld.yaml`](hosts/nixos/minas-tirith/manifests/palworld.yaml).

## Takeover changes

- Hardened the checks that guard external-checkout removal, workload selectors,
  and k3s reconciliation. The reconciler now has seven characterization tests for
  missing sources, valid objects, YAML errors, GVK errors, and kubectl failures.
- Repaired the filesystem and PostgreSQL recovery documentation. Filesystem restore
  is explicitly offline, stops k3s and its remaining writers, distinguishes the two
  pools, and does not claim that same-host media is an off-site copy.
- Split Pelargir manifest work into catalog, rendering, and delivery modules.
- Split Pelargir secret declarations from the runtime secret applier. The contract
  check reads the real implementation; a worker attempt to satisfy it with source
  comment anchors was rejected.
- Split Minas Traefik routes into catalog, rendering, and delivery modules while
  preserving the activation script and all 25 managed filenames exactly.
- Split Nardol Wolf into explicit image/config, container/GPU, audio/VBAN/firewall,
  and readiness/assertion fragments.
- Reduced `ROADMAP.md` from completed-history narrative to open work only, retained
  every verified unresolved obligation, and removed obsolete numbered citations.

The resulting composition roots are intentionally small:

- `hosts/nixos/pelargir/manifests.nix`: 188 lines
- `hosts/nixos/pelargir/secrets.nix`: 11 lines
- `hosts/nixos/minas-tirith/traefik-routes.nix`: 45 lines
- `hosts/nixos/nardol/wolf.nix`: 45 lines

## Verification evidence

The full local gate passes:

```sh
nix flake check
```

That runs 25 checks on `aarch64-darwin`. Nix reports the expected local limitation
that incompatible `x86_64-linux` checks are omitted; CI remains responsible for the
native Linux half.

The mutation harness passes all ten negative tests with no dead mutation and no
harness error:

```sh
bash scripts/mutation-test.sh .
```

Before prose-only source cleanup, every structural split produced the exact same
five derivation hashes as the recorded takeover baseline:

```text
nardol         3z9a1dvvrbj59gg5sypzgzrfzhqdw81i
minas-tirith   fr6inpab9bbg8hfhjn6hdva4f696gcy5
osgiliath      xisvrbbdd1gqdw0r5aqf8d10dxh79q2v
pelargir       jyj5k3sl5nfrfb6a8bqb3na7may3s5dh
dol-amroth     r93nh5xbhjryfnjrw3dqpqkniwgkpqbs
```

The final citation cleanup intentionally changes only prose packaged into Minas
shell scripts and one Pelargir Python docstring. Its expected pinned hashes are:

```text
nardol         3z9a1dvvrbj59gg5sypzgzrfzhqdw81i
minas-tirith   awjgks3r0888v5wa36jw0vcwqh9grg7z
osgiliath      xisvrbbdd1gqdw0r5aqf8d10dxh79q2v
pelargir       ywqxrmh7z89g3bsnlg73rgpxispk4739
dol-amroth     r93nh5xbhjryfnjrw3dqpqkniwgkpqbs
```

After removing full-line comments, both Minas scripts are byte-identical to their
pre-cleanup forms:

```text
backup-root-data.sh  c5a8aa97132c9095cd6cb9cf748de56a5cc3a7329bd0b40b22134f925192d982
healthcheck-ping.sh  a76363fd0a22452680334faeacbc7ef1369133e492b41171d090880590516144
```

The Pelargir executable-source diff is one function-docstring line. The targeted
shell, unit, Python, docs, and reconciler checks pass after that change.

## Review and remaining boundary

Claude was used as a skeptical second reviewer, not an authority. Its initial audit
helped locate recovery-runbook and check weaknesses, but every claim was verified
against evaluated source before acceptance. An exact review of the first hardened
check commit found no must-fix issue. The final consolidated Claude review must be
read and triaged before this branch is considered ready to merge or deploy.

The filesystem restore runbook restores file data. The database backup/restore
runbook documents dump formats and replay, but it is not yet a proven blank-disk,
fresh-cluster database rebuild. Do not describe disaster recovery as complete until
that end-to-end path is rehearsed. Remaining work is tracked in
[`ROADMAP.md`](ROADMAP.md).

Deployment is deliberately outside this handoff. If later authorized, follow the
repository three-beat deployment rule and remember that Pelargir delivers manifests
while Minas delivers Traefik routes; a cross-host commit may require Pelargir first.
