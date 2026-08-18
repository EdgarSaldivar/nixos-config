#!/usr/bin/env python3
"""Report what the cluster is running that no manifest declares any more.

WHY THIS EXISTS

k3s auto-deploy has no lifecycle. Removing an object from a manifest -- or removing
a manifest file entirely -- does NOT delete the applied resource. `manifests.nix`
deliberately only *reports* a stale file rather than deleting it, because deleting
during activation could drop a live route. That trade is sound. The gap it leaves is
that nothing tracks what has accumulated.

This is not theoretical. On 2026-08-16 a traefik Middleware named `cloudflare-only`
was found live in the cluster, referenced by nothing, eleven days after it had
stopped being wanted -- and it was an ACL that would have 403'd Home Assistant had
anyone pointed a route at it. Nothing reported it, because nothing was looking.

HOW IT KNOWS

Every object applied by an AddOn carries ownership annotations:

    objectset.rio.cattle.io/owner-gvk:       k3s.cattle.io/v1, Kind=Addon
    objectset.rio.cattle.io/owner-name:      <addon>
    objectset.rio.cattle.io/owner-namespace: kube-system

and the AddOn itself records the KINDS it manages (not the objects -- which is
exactly why it cannot prune on its own):

    addon.k3s.cattle.io/gvks: /v1, Kind=Service;traefik.io/v1alpha1, Kind=Middleware;...

So for each AddOn: parse the file it was applied from, list live objects of the
kinds it manages, keep those it owns, and compare.

BOTH DIRECTIONS ARE REPORTED, because they mean different things:

  ORPHANED  live, owned by the AddOn, no longer in the file.
            The accumulation this exists to find.

  MISSING   in the file, but not live.
            Either the apply failed, or something deleted it out of band. Worth
            knowing; not necessarily wrong mid-rollout.
"""

import json
import os
import subprocess
import sys

import yaml

OWNER_NAME = "objectset.rio.cattle.io/owner-name"
GVKS_ANNOTATION = "addon.k3s.cattle.io/gvks"


def kubectl(*args):
    """Run kubectl and return parsed JSON, or None if the call failed.

    Failure is returned, never raised: one unlistable kind must not abort the whole
    report. A kind that cannot be listed is reported as a gap rather than silently
    contributing zero objects, which would look exactly like "nothing orphaned".
    """
    try:
        out = subprocess.run(
            ["k3s", "kubectl", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if out.returncode != 0:
        return None, out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "unknown error"
    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"unparseable JSON: {exc}"


def ident(obj):
    """(kind, namespace, name) -- namespace normalised so cluster-scoped compares."""
    meta = obj.get("metadata") or {}
    return (obj.get("kind") or "?", meta.get("namespace") or "-", meta.get("name") or "?")


def declared_in(path):
    """Every object declared in a manifest file.

    Returns (set_of_idents, error). A parse failure is an ERROR, not an empty set:
    an unreadable file that silently yielded nothing would mark every live object it
    owns as orphaned, which is the most alarming possible way to be wrong.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            docs = list(yaml.safe_load_all(fh))
    except (OSError, yaml.YAMLError) as exc:
        return None, str(exc)

    out = set()
    for doc in docs:
        if not isinstance(doc, dict) or not doc.get("kind"):
            continue
        out.add(ident(doc))
    return out, None


def validate(path):
    """Server-side dry-run the manifest. Returns (errors, note).

    ⛔ This is the schema check ROADMAP item 6 asked for, and the API server is a
    BETTER tool for it than a vendored kubeconform bundle would be:

      * it knows the CRDs this cluster actually installs -- traefik's TLSOption,
        cert-manager's Certificate/ClusterIssuer, k3s's HelmChart -- at the exact
        versions installed, with no schema bundle to vendor and no refresh ritual
        when a CRD is upgraded;
      * it adjudicates immutable-field changes, which item 6 notes nothing offline
        can decide;
      * it is the same authority that will reject the manifest for real.

    The trade is that it needs a cluster, so this is a periodic and pre-deploy gate
    rather than a `nix flake check` gate. For a fleet that always has a cluster in
    reach, that is the better half of the trade.
    """
    try:
        out = subprocess.run(
            ["k3s", "kubectl", "apply", "--dry-run=server", "-f", path],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"could not run dry-run: {exc}"

    if out.returncode == 0:
        return [], None

    # kubectl warns on every object lacking last-applied-configuration, which is
    # every object k3s applied through an AddOn rather than through kubectl. That
    # is expected here and is not a schema problem.
    errs = [
        ln.strip()
        for ln in out.stderr.splitlines()
        if ln.strip() and not ln.startswith("Warning:")
    ]
    return errs, None


def resource_arg(gvk):
    """'traefik.io/v1alpha1, Kind=Middleware' -> 'middleware.traefik.io'."""
    gvk = gvk.strip()
    if not gvk or "Kind=" not in gvk:
        return None
    head, kind = gvk.split("Kind=", 1)
    kind = kind.strip().lower()
    group = head.split("/", 1)[0].strip().strip(",")
    return f"{kind}.{group}" if group else kind


def main():
    addons, err = kubectl("-n", "kube-system", "get", "addon", "-o", "json")
    if addons is None:
        print(f"FATAL: cannot list addons: {err}", file=sys.stderr)
        return 2

    lines = []
    total_orphans = 0
    total_missing = 0
    total_invalid = 0
    total_gaps = 0
    checked = 0

    for addon in addons.get("items", []):
        meta = addon.get("metadata") or {}
        name = meta.get("name", "?")
        source = (addon.get("spec") or {}).get("source")
        gvks_raw = (meta.get("annotations") or {}).get(GVKS_ANNOTATION, "")

        if not source:
            lines.append(f"[{name}] SKIP: addon records no source file")
            continue
        if not os.path.exists(source):
            # The file is gone but the AddOn -- and its objects -- remain. This is
            # the "stale manifest" case in its purest form.
            lines.append(f"[{name}] ⛔ SOURCE FILE MISSING: {source}")
            lines.append(f"[{name}]    its objects are still live and now declared nowhere")
            total_gaps += 1
            continue

        declared, derr = declared_in(source)
        if declared is None:
            lines.append(f"[{name}] ⛔ CANNOT PARSE {source}: {derr}")
            lines.append(f"[{name}]    skipped rather than reporting everything as orphaned")
            total_gaps += 1
            continue

        live = set()
        for gvk in gvks_raw.split(";"):
            arg = resource_arg(gvk)
            if not arg:
                continue
            got, gerr = kubectl("get", arg, "-A", "-o", "json")
            if got is None:
                got, gerr = kubectl("get", arg, "-o", "json")
            if got is None:
                lines.append(f"[{name}] ⚠️  cannot list {arg}: {gerr}")
                total_gaps += 1
                continue
            for obj in got.get("items", []):
                anns = (obj.get("metadata") or {}).get("annotations") or {}
                if anns.get(OWNER_NAME) == name:
                    live.add(ident(obj))

        checked += 1
        orphaned = sorted(live - declared)
        missing = sorted(declared - live)

        for kind, ns, nm in orphaned:
            lines.append(f"[{name}] ORPHANED {kind}/{ns}/{nm} -- live, owned, no longer declared")
        for kind, ns, nm in missing:
            lines.append(f"[{name}] MISSING  {kind}/{ns}/{nm} -- declared, not live")

        total_orphans += len(orphaned)
        total_missing += len(missing)

        errs, note = validate(source)
        if note:
            lines.append(f"[{name}] ⚠️  {note}")
            total_gaps += 1
        for e in errs:
            lines.append(f"[{name}] INVALID  {e}")
        total_invalid += len(errs)

    header = (
        f"k3s reconciliation: {checked} addons compared, "
        f"{total_orphans} orphaned, {total_missing} missing, "
        f"{total_invalid} invalid, {total_gaps} gaps"
    )
    report = "\n".join([header, ""] + (lines or ["nothing to report"])) + "\n"

    out_dir = os.environ.get("REPORT_DIR", "/var/lib/k3s-reconcile")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as fh:
            fh.write(report)
    except OSError as exc:
        print(f"WARNING: could not write report: {exc}", file=sys.stderr)

    print(report, end="")

    # ⛔ Orphans and MISSING do not fail the unit. Both are normal for hours after a
    # deliberate change, and a unit that goes red for something expected is a unit
    # whose red gets ignored.
    #
    # INVALID and GAPS do fail, for different reasons:
    #   INVALID -- the API server refuses this manifest, so an object silently is
    #              not there. That is never an expected steady state.
    #   GAPS    -- a kind that could not be listed or a file that could not be
    #              parsed means the report does not know what it does not know.
    return 1 if (total_gaps or total_invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
