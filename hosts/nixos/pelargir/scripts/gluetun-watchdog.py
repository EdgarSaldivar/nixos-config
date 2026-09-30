#!/usr/bin/env python3
"""Recycle a gluetun-gated Deployment whose VPN sidecar has wedged.

gluetun can wedge in a state it never leaves on its own. Measured 2026-09-26: PIA's
Amsterdam TCP gateways dropped for ~13 minutes, both TCP pods' health checks failed,
and gluetun logged "[vpn] stopping" -- then never "stopped" or "starting" again.
OpenVPN reconnected by itself inside the old process ("Preserving previous TUN/TAP
instance"), so traffic flowed, but gluetun's control loop was stuck: no health-driven
restarts, no public-IP fetch, no port forwarding, and not one log line for 3.5 days.
It had truncated /shared/ip and /shared/forwarded_port on the way down and never
rewrote them. deluge-books was down the whole time (mam-registrar waits for an IP
that never comes); deluge-vpn kept seeding with no inbound port.

Nothing inside the Pod can repair this BY DESIGN: it has no cluster DNS, gluetun
drops the service CIDR and the hardening container drops the Pod subnet, so it
cannot reach the API. And a container-level restart of gluetun is the wrong fix:
gluetun rebuilds its firewall on start, while the `hardening` rules that sit on top
of it are installed once, at Pod start. Only a new Pod restores both. This runs on
the control plane, where the API is.

⛔ A recycle SCALES THE DEPLOYMENT TO ZERO, WAITS, AND SCALES BACK. It never deletes
the Pod. Deleting lets the ReplicaSet start the replacement while the old Pod is
still terminating (up to 180 s for deluge-vpn), so for that window two gluetuns
hold the same PIA account on the same gateway and displace each other -- the exact
collision `ef06db0` split the server pools to prevent. Demonstrated 2026-09-29:
two deletes in a row each came up with "Connection reset" loops and no forwarded
port; scale 0 -> 1 came up clean on the first try.

The scale is restored to the value read immediately before it, so the Deployment
ends where it started and agrees with its manifest (AGENTS.md: durable state
belongs in git). The value is persisted BEFORE scaling to zero, so a run killed
mid-recycle is finished by the next one. If both fail, the next k3s server start
re-applies the manifest anyway.
"""

import datetime
import json
import os
import subprocess
import sys
import time

SIDECAR = "gluetun"
# gluetun's default when VPN_PORT_FORWARDING_STATUS_FILE is unset.
DEFAULT_PORT_FILE = "/tmp/gluetun/forwarded_port"

# gluetun restarts its own VPN after a failed health check, which takes a few
# minutes when it works. Fifteen minutes of continuous failure is well past that
# and still an order of magnitude inside the 3.5 days the 2026-09-26 wedge ran.
UNHEALTHY_FOR = 15 * 60
# One recycle per Deployment per hour. If a new Pod is still broken after that,
# the cause is outside the Pod (a region outage, a revoked credential) and
# recycling harder only churns Deluge. The unit goes red instead so someone looks.
COOLDOWN = 60 * 60
# Added to the Pod's terminationGracePeriodSeconds when waiting for it to go.
TERMINATION_MARGIN = 120
POLL_SECONDS = 5

sleep = time.sleep


def log(message):
    print(message, flush=True)


def now():
    return time.time()


def kubectl(*args, parse=True, timeout=60):
    """Run `k3s kubectl`; return (result, error). Failure is returned, never raised."""
    try:
        out = subprocess.run(
            ["k3s", "kubectl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if out.returncode != 0:
        lines = out.stderr.strip().splitlines()
        return None, lines[-1] if lines else f"exit {out.returncode}"
    if not parse:
        return out.stdout, None
    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"unparseable JSON: {exc}"


def parse_time(stamp):
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def sidecar_spec(pod):
    for container in pod["spec"].get("initContainers", []):
        if container.get("name") == SIDECAR:
            return container
    return None


def sidecar_status(pod):
    for status in pod.get("status", {}).get("initContainerStatuses", []):
        if status.get("name") == SIDECAR:
            return status
    return None


def owning_deployment(pod, rs_owner):
    namespace = pod["metadata"]["namespace"]
    for ref in pod["metadata"].get("ownerReferences", []):
        if ref.get("kind") == "ReplicaSet":
            return rs_owner.get((namespace, ref.get("name")))
    return None


def replicaset_owners(replicasets):
    owners = {}
    for rs in replicasets["items"]:
        for ref in rs["metadata"].get("ownerReferences", []):
            if ref.get("kind") == "Deployment":
                owners[(rs["metadata"]["namespace"], rs["metadata"]["name"])] = ref["name"]
    return owners


def problem(pod):
    """Why this Pod's VPN is unusable, or None if it is fine.

    ⚠️ Pod phase is deliberately NOT consulted. The wedged deluge-books Pod sat in
    phase Pending for days, because its startup gate never passed, while gluetun
    itself was Running. Readiness of the gluetun container is the signal.
    """
    status = sidecar_status(pod)
    if status is None or not status.get("ready"):
        return "gluetun not ready"
    env = {e["name"]: e.get("value") for e in sidecar_spec(pod).get("env", []) if "name" in e}
    if (env.get("VPN_PORT_FORWARDING") or "").lower() != "on":
        return None
    # gluetun can pass its health check with port forwarding dead: the first
    # attempt after connecting failed on 2026-09-29 and gluetun did not retry.
    # Deluge then announces a port nobody can reach, which looks like slow torrents.
    path = env.get("VPN_PORT_FORWARDING_STATUS_FILE") or DEFAULT_PORT_FILE
    meta = pod["metadata"]
    out, err = kubectl(
        "-n", meta["namespace"], "exec", meta["name"], "-c", SIDECAR, "--", "cat", path,
        parse=False,
        timeout=30,
    )
    if err is not None or not (out or "").strip().isdigit():
        return "port forwarding is on but no forwarded port is published"
    return None


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    for key in ("unhealthy", "recycled", "restoring"):
        state.setdefault(key, {})
    return state


def save_state(path, state):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def restore(namespace, name, replicas):
    """Scale back to `replicas` unless something else already moved it off zero."""
    dep, err = kubectl("-n", namespace, "get", "deployment", name, "-o", "json")
    if dep is None:
        log(f"{namespace}/{name}: cannot read Deployment to restore {replicas} replicas: {err}")
        return False
    current = dep["spec"].get("replicas", 1)
    if current != 0:
        log(f"{namespace}/{name}: already at {current} replicas; nothing to restore")
        return True
    _, err = kubectl(
        "-n", namespace, "scale", f"deployment/{name}", f"--replicas={replicas}", parse=False
    )
    if err is not None:
        log(f"{namespace}/{name}: FAILED to restore {replicas} replicas: {err}")
        return False
    log(f"{namespace}/{name}: restored to {replicas} replicas")
    return True


def wait_gone(namespace, name, rs_owner, timeout):
    deadline = now() + timeout
    while True:
        pods, err = kubectl("-n", namespace, "get", "pods", "-o", "json")
        if pods is not None:
            left = [p for p in pods["items"] if owning_deployment(p, rs_owner) == name]
            if not left:
                return True
        if now() >= deadline:
            log(f"{namespace}/{name}: Pods still present after {timeout}s ({err or 'terminating'})")
            return False
        sleep(POLL_SECONDS)


def recycle(namespace, name, why, rs_owner, state, state_path, dry_run):
    key = f"{namespace}/{name}"
    dep, err = kubectl("-n", namespace, "get", "deployment", name, "-o", "json")
    if dep is None:
        log(f"{key}: cannot read Deployment: {err}")
        return False
    replicas = dep["spec"].get("replicas", 1)
    if replicas == 0:
        log(f"{key}: declared at 0 replicas; leaving it alone")
        return True
    grace = dep["spec"]["template"]["spec"].get("terminationGracePeriodSeconds", 30)
    log(f"{key}: {why} for over {UNHEALTHY_FOR // 60}m; recycling ({replicas} -> 0 -> {replicas})")
    if dry_run:
        log(f"{key}: dry run; not scaling")
        return True

    state["restoring"][key] = replicas
    save_state(state_path, state)
    clean = False
    try:
        _, err = kubectl("-n", namespace, "scale", f"deployment/{name}", "--replicas=0", parse=False)
        if err is not None:
            log(f"{key}: scale to 0 failed: {err}")
        else:
            clean = wait_gone(namespace, name, rs_owner, grace + TERMINATION_MARGIN)
    finally:
        restored = restore(namespace, name, replicas)
    if restored:
        del state["restoring"][key]
    return clean and restored


def main():
    state_dir = os.environ.get("STATE_DIR", "/var/lib/gluetun-watchdog")
    dry_run = os.environ.get("GLUETUN_WATCHDOG_DRY_RUN") == "1"
    state_path = os.path.join(state_dir, "state.json")
    state = load_state(state_path)
    failures = 0

    # Finish any recycle an earlier run started and could not complete.
    for key, replicas in list(state["restoring"].items()):
        namespace, name = key.split("/", 1)
        if restore(namespace, name, replicas):
            del state["restoring"][key]
        else:
            failures += 1
    save_state(state_path, state)

    pods, err = kubectl("get", "pods", "-A", "-o", "json")
    if pods is None:
        log(f"cannot list pods: {err}")
        return 2
    replicasets, err = kubectl("get", "replicasets", "-A", "-o", "json")
    if replicasets is None:
        log(f"cannot list replicasets: {err}")
        return 2
    rs_owner = replicaset_owners(replicasets)

    seen = set()
    healthy = 0
    for pod in pods["items"]:
        if sidecar_spec(pod) is None or pod["metadata"].get("deletionTimestamp"):
            continue
        namespace = pod["metadata"]["namespace"]
        deployment = owning_deployment(pod, rs_owner)
        if deployment is None:
            log(f"{namespace}/{pod['metadata']['name']}: gluetun Pod not owned by a Deployment; not managed")
            continue
        key = f"{namespace}/{deployment}"
        seen.add(key)

        why = problem(pod)
        if why is None:
            healthy += 1
            gone = state["unhealthy"].pop(key, None)
            if gone:
                log(f"{key}: recovered on its own ({gone['reason']})")
            continue

        t = now()
        entry = state["unhealthy"].setdefault(key, {"since": t})
        entry["reason"] = why
        bad_for = t - entry["since"]
        pod_age = t - parse_time(pod["metadata"]["creationTimestamp"])
        if bad_for < UNHEALTHY_FOR or pod_age < UNHEALTHY_FOR:
            log(f"{key}: {why} for {int(bad_for // 60)}m; recycling at {UNHEALTHY_FOR // 60}m")
            continue
        since_recycle = t - state["recycled"].get(key, 0)
        if since_recycle < COOLDOWN:
            log(
                f"{key}: STILL BROKEN {int(since_recycle // 60)}m after a recycle ({why}); "
                "the cause is likely outside the Pod"
            )
            failures += 1
            continue
        if not recycle(namespace, deployment, why, rs_owner, state, state_path, dry_run):
            failures += 1
        if not dry_run:
            state["recycled"][key] = now()
            state["unhealthy"].pop(key, None)

    for key in list(state["unhealthy"]):
        if key not in seen:
            del state["unhealthy"][key]
    save_state(state_path, state)
    log(f"checked {len(seen)} gluetun Deployment(s): {healthy} healthy")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
