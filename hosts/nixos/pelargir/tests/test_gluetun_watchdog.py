import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


DEFAULT_SCRIPT = Path(__file__).parents[1] / "scripts" / "gluetun-watchdog.py"
SCRIPT = Path(os.environ.get("GLUETUN_WATCHDOG_SCRIPT", DEFAULT_SCRIPT))
SPEC = importlib.util.spec_from_file_location("gluetun_watchdog", SCRIPT)
watchdog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watchdog
SPEC.loader.exec_module(watchdog)

T0 = 1_800_000_000.0
MINUTE = 60


def stamp(seconds):
    return (
        datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class Cluster:
    """Just enough of the API for the calls the watchdog makes, with a call log."""

    def __init__(self, clock):
        self.clock = clock
        self.pods = []
        self.deployments = {}
        self.port_files = {}
        self.calls = []
        self.fail = set()
        # How many `get pods` polls a scaled-down Deployment's Pods survive.
        self.terminating_polls = 1

    def add(self, name, *, ready=True, port="41234", created=None, ns="media",
            replicas=1, grace=180, port_forwarding="on", terminating=False):
        rs = f"{name}-rs"
        self.deployments[(ns, name)] = {"replicas": replicas, "grace": grace, "rs": rs}
        env = [{"name": "VPN_PORT_FORWARDING", "value": port_forwarding},
               {"name": "VPN_PORT_FORWARDING_STATUS_FILE", "value": "/shared/forwarded_port"}]
        pod = {
            "metadata": {
                "name": f"{name}-pod",
                "namespace": ns,
                "creationTimestamp": stamp(created if created is not None else T0 - 3600),
                "ownerReferences": [{"kind": "ReplicaSet", "name": rs}],
            },
            "spec": {"initContainers": [{"name": "gluetun", "env": env}]},
            "status": {"initContainerStatuses": [{"name": "gluetun", "ready": ready}]},
        }
        if terminating:
            pod["metadata"]["deletionTimestamp"] = stamp(T0)
        self.pods.append(pod)
        if port is not None:
            self.port_files[(ns, f"{name}-pod")] = port

    def scales(self):
        return [c for c in self.calls if "scale" in c]

    def __call__(self, *args, parse=True, timeout=60):
        self.calls.append(args)
        args = list(args)
        ns = None
        if args[:1] == ["-n"]:
            ns, args = args[1], args[2:]
        verb = args[0]
        if verb in self.fail:
            return None, f"{verb} refused"
        if verb == "get" and args[1] == "pods":
            items = self.pods if ns is None else [p for p in self.pods if p["metadata"]["namespace"] == ns]
            if ns is not None:
                for (dns, dname), dep in self.deployments.items():
                    if dns == ns and dep["replicas"] == 0:
                        if self.terminating_polls > 0:
                            self.terminating_polls -= 1
                        else:
                            self.pods = [p for p in self.pods
                                         if p["metadata"]["ownerReferences"][0]["name"] != dep["rs"]]
                items = [p for p in self.pods if p["metadata"]["namespace"] == ns]
            return {"items": items}, None
        if verb == "get" and args[1] == "replicasets":
            return {"items": [
                {"metadata": {"namespace": dns, "name": dep["rs"],
                              "ownerReferences": [{"kind": "Deployment", "name": dname}]}}
                for (dns, dname), dep in self.deployments.items()
            ]}, None
        if verb == "get" and args[1] == "deployment":
            dep = self.deployments[(ns, args[2])]
            return {"spec": {"replicas": dep["replicas"],
                             "template": {"spec": {"terminationGracePeriodSeconds": dep["grace"]}}}}, None
        if verb == "scale":
            name = args[1].split("/", 1)[1]
            self.deployments[(ns, name)]["replicas"] = int(args[2].split("=", 1)[1])
            return "scaled", None
        if verb == "exec":
            content = self.port_files.get((ns, args[1]))
            if content is None:
                return None, "cat: can't open '/shared/forwarded_port': No such file or directory"
            return content, None
        raise AssertionError(f"unexpected kubectl call: {args}")


@pytest.fixture
def env(monkeypatch, tmp_path):
    clock = {"t": T0}
    cluster = Cluster(clock)

    def advance(seconds):
        clock["t"] += seconds

    monkeypatch.setattr(watchdog, "kubectl", cluster)
    monkeypatch.setattr(watchdog, "now", lambda: clock["t"])
    monkeypatch.setattr(watchdog, "sleep", advance)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("GLUETUN_WATCHDOG_DRY_RUN", raising=False)
    cluster.advance = advance
    cluster.state = lambda: json.loads((tmp_path / "state.json").read_text())
    return cluster


def test_healthy_pod_takes_no_action(env):
    env.add("deluge-vpn")
    assert watchdog.main() == 0
    assert env.scales() == []
    assert env.state()["unhealthy"] == {}


def test_first_unhealthy_sighting_only_starts_the_clock(env):
    env.add("deluge-vpn", ready=False)
    assert watchdog.main() == 0
    assert env.scales() == []
    assert env.state()["unhealthy"]["media/deluge-vpn"]["reason"] == "gluetun not ready"


def test_wedged_past_threshold_scales_to_zero_waits_then_restores(env):
    env.add("deluge-vpn", ready=False)
    watchdog.main()
    env.advance(16 * MINUTE)
    assert watchdog.main() == 0
    scale_calls = [c[-1] for c in env.scales()]
    assert scale_calls == ["--replicas=0", "--replicas=1"]
    # The replacement is only allowed once the old Pod is gone: two gluetuns on one
    # PIA gateway displace each other.
    scale_down = env.calls.index(env.scales()[0])
    scale_up = env.calls.index(env.scales()[1])
    polls = [c for c in env.calls[scale_down:scale_up] if c[2:4] == ("get", "pods")]
    assert len(polls) >= 2
    assert env.deployments[("media", "deluge-vpn")]["replicas"] == 1
    state = env.state()
    assert state["restoring"] == {} and "media/deluge-vpn" in state["recycled"]


def test_missing_forwarded_port_counts_as_unhealthy(env):
    env.add("deluge-vpn", ready=True, port=None)
    watchdog.main()
    assert env.state()["unhealthy"]["media/deluge-vpn"]["reason"].startswith("port forwarding is on")
    env.port_files[("media", "deluge-vpn-pod")] = "\n"
    env.advance(16 * MINUTE)
    watchdog.main()
    assert [c[-1] for c in env.scales()] == ["--replicas=0", "--replicas=1"]


def test_port_forwarding_off_is_not_checked(env):
    env.add("books-netns", port=None, port_forwarding="off")
    assert watchdog.main() == 0
    assert not any("exec" in c for c in env.calls)


def test_young_pod_is_not_recycled(env):
    env.add("deluge-vpn", ready=False, created=T0 + 10 * MINUTE)
    watchdog.main()
    env.advance(16 * MINUTE)
    assert watchdog.main() == 0
    assert env.scales() == []


def test_still_broken_inside_cooldown_fails_the_unit_without_recycling(env):
    env.add("deluge-vpn", ready=False)
    watchdog.main()
    env.advance(16 * MINUTE)
    watchdog.main()
    env.add("deluge-vpn", ready=False, created=env.clock["t"] - 20 * MINUTE)
    env.advance(16 * MINUTE)
    watchdog.main()
    env.advance(16 * MINUTE)
    assert watchdog.main() == 1
    assert len(env.scales()) == 2


def test_deployment_parked_at_zero_is_left_alone(env):
    env.add("deluge-books", ready=False, replicas=0)
    watchdog.main()
    env.advance(16 * MINUTE)
    assert watchdog.main() == 0
    assert env.scales() == []


def test_terminating_pod_is_ignored(env):
    env.add("deluge-vpn", ready=False, terminating=True)
    assert watchdog.main() == 0
    assert env.state()["unhealthy"] == {}


def test_interrupted_recycle_is_finished_by_the_next_run(env, tmp_path):
    env.add("deluge-vpn")
    env.deployments[("media", "deluge-vpn")]["replicas"] = 0
    (tmp_path / "state.json").write_text(json.dumps({"restoring": {"media/deluge-vpn": 1}}))
    assert watchdog.main() == 0
    assert env.deployments[("media", "deluge-vpn")]["replicas"] == 1
    assert env.state()["restoring"] == {}


def test_scale_down_timeout_still_restores_and_fails_the_unit(env):
    env.add("deluge-vpn", ready=False, grace=60)
    env.terminating_polls = 10_000
    watchdog.main()
    env.advance(16 * MINUTE)
    assert watchdog.main() == 1
    assert env.deployments[("media", "deluge-vpn")]["replicas"] == 1


def test_dry_run_changes_nothing(env, monkeypatch):
    monkeypatch.setenv("GLUETUN_WATCHDOG_DRY_RUN", "1")
    env.add("deluge-vpn", ready=False)
    watchdog.main()
    env.advance(16 * MINUTE)
    assert watchdog.main() == 0
    assert env.scales() == []


def test_cannot_list_pods_exits_2(env):
    env.fail.add("get")
    assert watchdog.main() == 2
