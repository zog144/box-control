from dataclasses import asdict, replace
import pytest

from zog.box_control.application_storage import ApplicationStorage
from zog.box_control.errors import RecoveryRequired
from zog.box_control.model import ApplicationSpec, ProgramSpec
from zog.box_control.project import Project
from zog.box_control.operations import application_from_record
from zog.box_control.runtime.systemd import service_definition


def _application():
    prepare = ProgramSpec(name="prepare", command=("/usr/bin/true",),
                          mounts=(("data", "/var/lib/portal"),),
                          execution_timeout_seconds=60)
    web = ProgramSpec(name="web", command=("/usr/bin/true",),
                      mounts=(("data", "/var/lib/portal"),))
    return ApplicationSpec(name="portal", persistent=True,
                           writable_mounts=("data",), preparation_revision="1",
                           programs=(web,), preparation=(prepare,),
                           execution_identity="application")


def _grant(project):
    return dict(schema="zog-execution-identity-v1", grant_id="a" * 32,
                version=1, project=str(project.path), application="portal",
                uid=29001, gid=29002)


def test_numeric_grant_applies_to_preparation_runtime_and_systemd(tmp_path):
    project = Project(tmp_path / "project")
    original = _application()
    grant = _grant(project)
    bound = ApplicationStorage.bind_execution(
        replace(original, storage_id="b" * 32), grant)
    assert [(p.user, p.group) for p in bound.preparation + bound.programs] == [
        ("29001", "29002"), ("29001", "29002")]
    assert original.programs[0].user == "regular"
    assert bound.execution_binding == grant
    root = tmp_path / "image"
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/true").write_text("fixture")
    (root / "var/lib/portal").mkdir(parents=True)
    for spec in bound.preparation + bound.programs:
        service = service_definition(project, bound, "A7K3Q2", spec,
                                     generation_root=root)
        assert service.execution_binding == grant
        assert dict(service.expected_properties())["User"] == "29001"
        assert dict(service.expected_properties())["Group"] == "29002"
        assert service.to_transport_dict()["execution_binding"] == grant
    reconstructed = application_from_record(asdict(bound))
    assert reconstructed.execution_binding == bound.execution_binding
    assert reconstructed.programs[0].user == "29001"


def test_ready_storage_binds_original_numeric_identity_after_restart(tmp_path):
    project = Project(tmp_path / "project")
    storage = ApplicationStorage(project)
    app = _application()
    grant = _grant(project)
    bound = ApplicationStorage.bind_execution(
        replace(app, storage_id="b" * 32), grant)
    (project.mounts_dir / "persistent" / ("b" * 32) / "data").mkdir(parents=True)
    storage.save(dict(schema=1, application=app.name, storage_id="b" * 32,
        state="ready", revision="1", signature=storage.signature(app),
        generation="generation", mounts=["data"],
        definition=asdict(bound), cleanup_pending=False))
    recovered = storage.bind(app, "generation")
    assert recovered.execution_binding == grant
    assert recovered.programs[0].user == "29001"
    assert recovered.preparation[0].group == "29002"


def test_stale_missing_or_cross_application_grant_blocks(tmp_path):
    project = Project(tmp_path / "project")
    app = _application()
    grant = _grant(project)
    with pytest.raises(RecoveryRequired, match="binding"):
        ApplicationStorage.bind_execution(app, None)
    altered = dict(grant, application="other")
    with pytest.raises(RecoveryRequired, match="binding"):
        ApplicationStorage.bind_execution(app, altered)
    altered = dict(grant, uid=0)
    with pytest.raises(RecoveryRequired, match="binding"):
        ApplicationStorage.bind_execution(app, altered)
    with pytest.raises(RecoveryRequired, match="unbound"):
        root = tmp_path / "image"
        (root / "var/lib/portal").mkdir(parents=True)
        service_definition(project, replace(app, storage_id="b" * 32), "A7K3Q2",
                           app.programs[0], generation_root=root)



def test_runtime_reference_preserves_grant_and_rejects_historical_mismatch(tmp_path):
    from zog.box_control.runtime.reference import (
        ApplicationRuntimeReference, RuntimeReferenceStore
    )
    from zog.box_control.model import ApplicationRuntimeState
    project = Project(tmp_path / "project")
    grant = _grant(project)
    reference = ApplicationRuntimeReference(
        runtime_id="A7K3Q2", application="portal", instance_id="portal",
        generation="generation", state=ApplicationRuntimeState.STARTING,
        execution_binding=grant,
    )
    encoded = RuntimeReferenceStore.encode({reference.runtime_id: reference})
    assert encoded["runtimes"][reference.runtime_id]["execution_binding"] == grant
    recovered = RuntimeReferenceStore.decode(encoded)[reference.runtime_id]
    assert recovered.execution_binding == grant
    legacy = RuntimeReferenceStore.encode({reference.runtime_id: replace(reference, execution_binding=None)})
    del legacy["runtimes"][reference.runtime_id]["execution_binding"]
    assert RuntimeReferenceStore.decode(legacy)[reference.runtime_id].execution_binding is None
