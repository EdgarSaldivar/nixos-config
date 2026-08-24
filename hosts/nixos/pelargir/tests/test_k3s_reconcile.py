import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


DEFAULT_SCRIPT = Path(__file__).parents[1] / "scripts" / "k3s-reconcile.py"
SCRIPT = Path(os.environ.get("K3S_RECONCILE_SCRIPT", DEFAULT_SCRIPT))
SPEC = importlib.util.spec_from_file_location("k3s_reconcile", SCRIPT)
reconcile = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reconcile
SPEC.loader.exec_module(reconcile)


@pytest.fixture
def isolated_main(monkeypatch, tmp_path):
    report_dir = tmp_path / "report"
    monkeypatch.setenv("REPORT_DIR", str(report_dir))
    monkeypatch.setattr(reconcile, "wait_for_api", lambda: True)
    return report_dir


def test_declared_in_reads_valid_multi_document_yaml(tmp_path):
    manifest = tmp_path / "explicit.yaml"
    manifest.write_text(
        """apiVersion: v1
kind: Service
metadata:
  name: api
  namespace: demo
---
# Empty YAML documents are ignored.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
""",
        encoding="utf-8",
    )

    declared, error = reconcile.declared_in(manifest)

    assert error is None
    assert declared == {
        ("ConfigMap", "-", "settings"),
        ("Service", "demo", "api"),
    }


def test_declared_in_returns_parse_failure(tmp_path):
    manifest = tmp_path / "invalid.yaml"
    manifest.write_text(
        """apiVersion: v1
kind: [Service
metadata:
  name: broken
""",
        encoding="utf-8",
    )

    declared, error = reconcile.declared_in(manifest)

    assert declared is None
    assert error


def test_resource_arg_handles_core_and_grouped_gvks():
    assert reconcile.resource_arg("/v1, Kind=Service") == "service"
    assert (
        reconcile.resource_arg("traefik.io/v1alpha1, Kind=Middleware")
        == "middleware.traefik.io"
    )


def test_kubectl_returns_last_error_line_on_command_failure(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="warning: retrying\nError from server: forbidden\n",
        )

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)

    result = reconcile.kubectl("get", "services", "-o", "json")

    assert result == (None, "Error from server: forbidden")
    assert calls == [
        (
            ["k3s", "kubectl", "get", "services", "-o", "json"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 60,
                "check": False,
            },
        )
    ]


def test_kubectl_rejects_invalid_json(monkeypatch):
    def fake_run(command, **kwargs):
        assert command == ["k3s", "kubectl", "get", "addons", "-o", "json"]
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 60,
            "check": False,
        }
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)

    value, error = reconcile.kubectl("get", "addons", "-o", "json")

    assert value is None
    assert error.startswith("unparseable JSON:")


def test_main_reports_addon_with_missing_source_under_current_behavior(
    monkeypatch, isolated_main, capsys
):
    source = "/var/lib/rancher/k3s/server/manifests/removed.yaml"
    addons = {
        "items": [
            {
                "metadata": {
                    "name": "removed",
                    "annotations": {
                        reconcile.GVKS_ANNOTATION: "/v1, Kind=Service"
                    },
                },
                "spec": {"source": source},
            }
        ]
    }
    real_exists = os.path.exists

    def fake_exists(path):
        if path == source:
            return False
        return real_exists(path)

    def fake_kubectl(*args):
        assert args == ("-n", "kube-system", "get", "addon", "-o", "json")
        return addons, None

    monkeypatch.setattr(reconcile.os.path, "exists", fake_exists)
    monkeypatch.setattr(reconcile, "kubectl", fake_kubectl)
    monkeypatch.setattr(
        reconcile,
        "declared_in",
        lambda path: pytest.fail(f"must not parse missing source {path}"),
    )
    monkeypatch.setattr(
        reconcile,
        "validate",
        lambda path: pytest.fail(f"must not validate missing source {path}"),
    )

    result = reconcile.main()
    output = capsys.readouterr()

    expected = (
        "k3s reconciliation: 0 addons compared, 0 orphaned, 0 missing, "
        "0 invalid, 1 gaps\n\n"
        f"[removed] ⛔ SOURCE FILE MISSING: {source}\n"
        "[removed]    its objects are still live and now declared nowhere\n"
    )
    assert result == 1
    assert output.out == expected
    assert output.err == ""
    assert (isolated_main / "report.txt").read_text(encoding="utf-8") == expected


def test_main_reports_normal_declared_live_match(
    monkeypatch, isolated_main, capsys
):
    source = "/var/lib/rancher/k3s/server/manifests/example.yaml"
    identity = ("Service", "demo", "api")
    addons = {
        "items": [
            {
                "metadata": {
                    "name": "example",
                    "annotations": {
                        reconcile.GVKS_ANNOTATION: "/v1, Kind=Service"
                    },
                },
                "spec": {"source": source},
            }
        ]
    }
    live = {
        "items": [
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "api",
                    "namespace": "demo",
                    "annotations": {reconcile.OWNER_NAME: "example"},
                },
            }
        ]
    }
    commands = []
    validated = []
    real_exists = os.path.exists

    def fake_exists(path):
        if path == source:
            return True
        return real_exists(path)

    def fake_kubectl(*args):
        commands.append(args)
        if args == ("-n", "kube-system", "get", "addon", "-o", "json"):
            return addons, None
        if args == ("get", "service", "-A", "-o", "json"):
            return live, None
        pytest.fail(f"unexpected kubectl call: {args}")

    def fake_declared(path):
        assert path == source
        return {identity}, None

    def fake_validate(path):
        validated.append(path)
        return [], None

    monkeypatch.setattr(reconcile.os.path, "exists", fake_exists)
    monkeypatch.setattr(reconcile, "kubectl", fake_kubectl)
    monkeypatch.setattr(reconcile, "declared_in", fake_declared)
    monkeypatch.setattr(reconcile, "validate", fake_validate)

    result = reconcile.main()
    output = capsys.readouterr()

    expected = (
        "k3s reconciliation: 1 addons compared, 0 orphaned, 0 missing, "
        "0 invalid, 0 gaps\n\nnothing to report\n"
    )
    assert result == 0
    assert output.out == expected
    assert output.err == ""
    assert commands == [
        ("-n", "kube-system", "get", "addon", "-o", "json"),
        ("get", "service", "-A", "-o", "json"),
    ]
    assert validated == [source]
    assert (isolated_main / "report.txt").read_text(encoding="utf-8") == expected
