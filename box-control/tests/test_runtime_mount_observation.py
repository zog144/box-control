import json

import pytest

from zog.box_control import BoxControl, Project
from zog.box_control.errors import RuntimeOperationError


class Transport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def application_runtime_mounts(self, *, project_root, runtime_id):
        self.calls.append((project_root, runtime_id))
        return self.result


def result(runtime_id="ABCDEF"):
    return {
        "schema": "zog-runtime-mount-observation-v1",
        "runtime_id": runtime_id,
        "basis": "kernel-mount-namespace",
        "snapshot": "non-atomic",
        "observed_at": 1.0,
        "recorded_boot_id": "b" * 32,
        "status": "observed",
        "programs": [
            {
                "program": "web",
                "unit_name": "zog-web.service",
                "recorded_invocation_id": "a" * 32,
                "status": "observed",
                "mount_namespace": "mnt:[4026532000]",
                "mount_namespaces": [],
                "multiple_mount_namespaces": False,
                "mounts": [
                    {
                        "destination": "/applications/station-access",
                        "kind": "application-software-view",
                        "expected_access": "read-only",
                        "observed_presence": "present",
                        "observed_access": "read-only",
                        "backing_identity": "verified",
                    }
                ],
                "truncated": False,
            }
        ],
        "truncated": False,
    }


def snapshot(project):
    if not project.path.exists():
        return {}
    return {
        str(path.relative_to(project.path)): path.read_bytes()
        for path in project.path.rglob("*")
        if path.is_file()
    }


def test_public_mount_observation_is_runtime_identity_only_and_read_only(tmp_path):
    project = Project(tmp_path / "project")
    project.state_dir.mkdir(parents=True)
    marker = project.state_dir / "mutation-incomplete.json"
    marker.write_text("{}")
    transport = Transport(result())
    control = BoxControl(project, systemd_transport=transport)
    before = snapshot(project)

    observed = control.observe_application_runtime_mounts("ABCDEF")

    assert observed["schema"] == "zog-runtime-mount-observation-v1"
    assert observed["basis"] == "kernel-mount-namespace"
    assert transport.calls == [(project.path, "ABCDEF")]
    assert snapshot(project) == before
    json.dumps(observed)


@pytest.mark.parametrize(
    "bad",
    [
        {"schema": "other", "runtime_id": "ABCDEF"},
        {"schema": "zog-runtime-mount-observation-v1", "runtime_id": "OTHER"},
        None,
    ],
)
def test_public_mount_observation_rejects_invalid_root_control_contract(tmp_path, bad):
    project = Project(tmp_path / "project")
    control = BoxControl(project, systemd_transport=Transport(bad))
    with pytest.raises(RuntimeOperationError):
        control.observe_application_runtime_mounts("ABCDEF")
