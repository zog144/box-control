"""Interrupted selected-program execution reuses its exact durable operation."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from zog.box_control.errors import RecoveryRequired, RuntimeOperationError
from zog.box_control.model import ApplicationSpec, ApplicationStartPolicy, ApplicationRuntimeState, ProgramSpec
from zog.box_control.operations import OperationManager
from zog.box_control.project import Project
from zog.box_control.runtime.reference import ApplicationRuntimeReference, ProgramRuntimeReference


class FakeSelectedRuntime:
    def __init__(self):
        self.transport = SimpleNamespace()
        self.replayed = []

    def preflight(self):
        return None

    def prepare(self, application, *, request_id, generation, instance_id,
                application_fingerprint, boot_id, **_kw):
        return ApplicationRuntimeReference(
            runtime_id="SINGLE1", application=application.name,
            instance_id=instance_id, generation=generation,
            state=ApplicationRuntimeState.STARTING, request_id=request_id,
            application_fingerprint=application_fingerprint, boot_id=boot_id,
            execution_binding=application.execution_binding,
            programs=(ProgramRuntimeReference(
                program=application.programs[0].name,
                unit_name="zog-project-SINGLE1-renew.service",
                command=application.programs[0].command,
            ),),
        )

    def definition(self, application, runtime_id, *, generation_root):
        assert runtime_id == "SINGLE1"
        assert [p.name for p in application.programs] == ["renew"]
        assert application.selected_program == "renew"
        service = SimpleNamespace(
            bind_paths=(), mount_inventory=lambda: {},
            expected_properties=lambda: (),
            unit_name="zog-project-SINGLE1-renew.service",
        )
        return SimpleNamespace(services=(service,))

    def launch(self, application, *, references, store, **kwargs):
        # The fake has no systemd side effects; it records which immutable
        # program snapshot would be executed during authoritative recovery.
        self.replayed.append((application.selected_program,
                              [p.name for p in application.programs],
                              kwargs["prepared"].request_id))
        result = replace(references["SINGLE1"], state=ApplicationRuntimeState.RUNNING,
                         programs=(replace(references["SINGLE1"].programs[0],
                                           invocation_id="invocation-1"),),
                         cleanup_pending=True)
        references["SINGLE1"] = result
        store.save(references)
        return result


def _setup(tmp_path):
    project = Project(tmp_path / "project")
    project.state_dir.mkdir(parents=True)
    runtime = FakeSelectedRuntime()
    manager = OperationManager(project, runtime, boot_id_provider=lambda: "boot-1")
    original_programs = (
        ProgramSpec("issue", ("/bin/true", "issue")),
        ProgramSpec("renew", ("/bin/true", "renew")),
        ProgramSpec("inspect", ("/bin/true", "inspect")),
    )
    grant = {"schema": "zog-execution-identity-v1", "project": str(project.path),
             "application": "certify-manage", "uid": 200001, "gid": 200001}
    app = ApplicationSpec(
        name="certify-manage", programs=(original_programs[1],),
        start_policy=ApplicationStartPolicy.EXTERNALLY_CONTROLLED,
        execution_identity="application", execution_binding=grant,
        persistent=True, writable_mounts=("certificate-data",),
        storage_id="a" * 32, selected_program="renew",
    )
    selection = SimpleNamespace(generation="image-1", root=tmp_path)
    return manager, runtime, app, selection


def test_interrupted_operation_replays_only_recorded_program(tmp_path):
    manager, runtime, application, selection = _setup(tmp_path)
    record = manager.prepare(
        application=application, selection=selection,
        references={}, instance_id="certify-manage", request_id="REQ1",
        replaces_runtime_id=None, boot_id="boot-1", fingerprint="fp",
        selected_program="renew",
    )
    raw = manager.store.load(manager.store.path(record["operation_id"]))
    assert raw["selected_program"] == "renew"
    assert [p["name"] for p in raw["application"]["programs"]] == ["renew"]
    assert [p.program for p in manager.references.load()["SINGLE1"].programs] == ["renew"]
    # A fresh controller instance recovers the interrupted operation using
    # saved selection, not whatever was later edited in application.py.
    restarted = OperationManager(manager.project, runtime, boot_id_provider=lambda: "boot-1")
    restarted.recover()
    assert runtime.replayed == [("renew", ["renew"], "REQ1")]
    assert restarted.store.records()[0]["phase"] == "committed"
    assert restarted.requests.result("REQ1").program_name == "renew"
    assert restarted.for_request("REQ1", "launch", application="certify-manage",
                                 selected_program="renew").runtime_id == "SINGLE1"
    with pytest.raises(RuntimeOperationError, match="different intent"):
        restarted.for_request("REQ1", "launch", application="certify-manage",
                              selected_program="issue")


def test_modified_selected_program_record_is_not_valid_recovery_authority(tmp_path):
    from zog.box_control.durability import replace_json
    manager, runtime, app, selection = _setup(tmp_path)
    record = manager.prepare(
        application=app, selection=selection, references={},
        instance_id="certify-manage", request_id="REQ1", replaces_runtime_id=None,
        boot_id="boot-1", fingerprint="fp", selected_program="renew",
    )
    record["selected_program"] = "issue"
    replace_json(manager.store.path(record["operation_id"]), record)
    with pytest.raises(RecoveryRequired, match="cannot read operation"):
        manager.recover()
    assert runtime.replayed == []
    assert manager.marker.exists()
