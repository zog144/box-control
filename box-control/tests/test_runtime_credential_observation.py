"""Public, read-only execution credential observation boundary."""
import json

import pytest

from zog.box_control import BoxControl, Project
from zog.box_control.errors import RuntimeOperationError


class Transport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def application_runtime_credentials(self, *, project_root, runtime_id, limit=50):
        self.calls.append((project_root, runtime_id, limit))
        return self.result


def _result(runtime_id="ABCDEF"):
    return {
        "schema": "zog-runtime-credential-observation-v1",
        "runtime_id": runtime_id,
        "application": "portal",
        "recorded_boot_id": "b" * 32,
        "snapshot": "non-atomic",
        "basis": "kernel-proc-status",
        "status": "observed",
        "programs": [
            {
                "program": "web", "unit_name": "zog-portal.service",
                "recorded_invocation_id": "i" * 32,
                "status": "observed",
                "control_group": "/portal.service",
                "processes": [{
                    "pid": 50, "start_time_ticks": 123,
                    "uid": [29001, 29001, 29001, 29001],
                    "gid": [29002, 29002, 29002, 29002],
                    "supplementary_groups": [29002], "comparison": "match",
                }],
            }
        ],
    }


def _files(project):
    return {str(p.relative_to(project.path)): p.read_bytes()
            for p in project.path.rglob("*") if p.is_file()}


def test_read_only_api_passes_only_runtime_identity_and_bounded_limit(tmp_path):
    project = Project(tmp_path / "project")
    marker = project.state_dir / "mutation-incomplete.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    transport = Transport(_result())
    control = BoxControl(project, systemd_transport=transport)
    before = _files(project)
    result = control.observe_application_runtime_credentials("ABCDEF", limit=18)
    assert result["status"] == "observed"
    assert transport.calls == [(project.path, "ABCDEF", 18)]
    assert _files(project) == before
    json.dumps(result)


@pytest.mark.parametrize("bad", [
    None,
    {"schema": "wrong", "runtime_id": "ABCDEF", "programs": []},
    {"schema": "zog-runtime-credential-observation-v1", "runtime_id": "other", "programs": []},
    {"schema": "zog-runtime-credential-observation-v1", "runtime_id": "ABCDEF"},
])
def test_malformed_root_control_response_is_rejected(tmp_path, bad):
    project = Project(tmp_path / "project")
    control = BoxControl(project, systemd_transport=Transport(bad))
    with pytest.raises(RuntimeOperationError, match="invalid runtime credential"):
        control.observe_application_runtime_credentials("ABCDEF")


@pytest.mark.parametrize("limit", [0, 65, True, 3.1])
def test_out_of_range_limits_rejected_without_transport(tmp_path, limit):
    project = Project(tmp_path / "project")
    transport = Transport(_result())
    control = BoxControl(project, systemd_transport=transport)
    with pytest.raises(RuntimeOperationError, match="credential process limit"):
        control.observe_application_runtime_credentials("ABCDEF", limit=limit)
    assert not transport.calls
