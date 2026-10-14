import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from zog.box_control.application_software import (
    ApplicationSoftwareReclaimer,
    protection_from_reference,
    resolve_binding,
    validate_binding,
)
from zog.box_control.application_storage import ApplicationStorage
from zog.box_control.errors import RecoveryRequired, RuntimeOperationError
from zog.box_control.images import ImageSelection
from zog.box_control.model import (
    ApplicationRuntimeState,
    ApplicationSpec,
    ProgramSpec,
)
from zog.box_control.operations import application_from_record
from zog.box_control.project import Project
from zog.box_control.runtime.reference import ApplicationRuntimeReference
from zog.box_control.runtime.systemd import service_definition
from zog.image_build.application_software import ApplicationSoftwareStore
from zog.image_build.application_views import ApplicationSoftwareViewStore


def descriptor(*, architecture="x86_64", version="3.12.7", libraries=()):
    return {
        "schema": 1,
        "mount_contract": "applications-v1",
        "architecture": architecture,
        "python": {
            "implementation": "cpython",
            "version": version,
            "soabi": "cpython-312-x86_64-linux-gnu",
        },
        "libraries": list(libraries),
    }


def publish_artifact(store, operation, files, *, libraries=("libssl.so.3",)):
    prepared = store.begin(
        operation,
        application="station-access",
        revision="git:" + operation[0] * 40,
        compatibility_requirements=descriptor(libraries=libraries),
        notices=(),
        provenance={"fixture": operation},
    )
    for name, value in files.items():
        path = prepared.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        if name.startswith("bin/"):
            path.chmod(0o755)
    return store.finalize(operation)


def fixture(tmp_path):
    project = Project(tmp_path / "demo")
    state = project.state_dir
    store = ApplicationSoftwareStore(state)
    backend = publish_artifact(
        store,
        "backend-a",
        {
            "bin/station-access": "#!/usr/bin/python3\n",
            "lib/python3.12/site-packages/station_access/__init__.py": "VERSION='fixture'\n",
        },
    )
    frontend = publish_artifact(
        store,
        "frontend-b",
        {"share/station-access/static/app.js": "console.log('fixture')\n"},
    )
    view = ApplicationSoftwareViewStore(state).publish(
        "station-view",
        application="station-access",
        artifacts=[backend.artifact, frontend.artifact],
    )
    generation = "1" * 64
    root = state / "image-build" / "generations" / generation / "root"
    (root / "var/lib/station").mkdir(parents=True)
    selection = ImageSelection(
        generation,
        root,
        {
            "kind": "image",
            "application_software_compatibility": descriptor(
                libraries=("libssl.so.3", "libc.so.6")
            ),
        },
        reused=True,
    )
    application = ApplicationSpec(
        name="station-access",
        programs=(
            ProgramSpec(
                name="web",
                command=("/applications/station-access/bin/station-access",),
                mounts=(("data", "/var/lib/station"),),
            ),
        ),
        software_view=view.view,
    )
    return project, selection, application, backend, frontend, view


def test_multi_artifact_view_is_bound_read_only_and_visible(tmp_path):
    project, selection, application, backend, frontend, view = fixture(tmp_path)
    bound = resolve_binding(project, application, selection)
    service = service_definition(
        project,
        bound,
        "A7K3Q2",
        bound.programs[0],
        generation_root=selection.root,
    )
    assert bound.software_binding["view"] == view.view
    assert bound.software_binding["reclamation_protection"]["artifacts"] == sorted(
        [backend.artifact, frontend.artifact]
    )
    assert [target for _source, target in service.read_only_bind_paths] == [
        "/applications/.store/" + item[7:]
        for item in sorted([backend.artifact, frontend.artifact])
    ] + ["/applications/station-access"]
    assert service.bind_paths[0][1] == "/var/lib/station"
    expected = dict(service.expected_properties())
    assert expected["BindReadOnlyPaths"] == [
        [str(source.resolve()), target]
        for source, target in (
            service.rootfs_bind_paths + service.read_only_bind_paths
        )
    ]
    assert expected["PrivateMounts"] is True
    environment = dict(service.environment)
    assert environment["PATH"].startswith("/applications/station-access/bin:")
    assert "/applications/station-access/lib/python3.12/site-packages" in environment["PYTHONPATH"]
    inventory = service.mount_inventory()
    assert inventory["software"]["application"]["view"] == view.view
    assert any(item["access"] == "read-write" for item in inventory["mounts"])
    assert sum(item["access"] == "read-only" for item in inventory["mounts"]) == 3


def test_binding_rejects_missing_corrupt_and_incompatible_objects(tmp_path):
    project, selection, application, backend, _frontend, _view = fixture(tmp_path)
    bound = resolve_binding(project, application, selection)
    artifact_manifest = Path(bound.software_binding["objects"][0]["manifest"])
    original = artifact_manifest.read_text()
    artifact_manifest.write_text(original + "\n")
    with pytest.raises((RecoveryRequired, RuntimeOperationError), match="manifest|invalid|changed"):
        validate_binding(project, application.name, bound.software_binding)

    # Recreate a clean fixture to exercise compatibility, not corruption.
    project2, selection2, application2, *_ = fixture(tmp_path / "second")
    incompatible = replace(
        selection2,
        manifest={
            **selection2.manifest,
            "application_software_compatibility": descriptor(
                version="3.13.0", libraries=("libssl.so.3",)
            ),
        },
    )
    with pytest.raises(RuntimeOperationError, match="incompatible"):
        resolve_binding(project2, application2, incompatible)


def test_rootfs_must_publish_software_compatibility(tmp_path):
    project, selection, application, *_ = fixture(tmp_path)
    selection = replace(selection, manifest={"kind": "image"})
    with pytest.raises(RuntimeOperationError, match="does not publish"):
        resolve_binding(project, application, selection)


def test_operation_snapshot_round_trip_preserves_exact_software_binding(tmp_path):
    project, selection, application, *_ = fixture(tmp_path)
    bound = resolve_binding(project, application, selection)
    restored = application_from_record(asdict(bound))
    assert restored.software_view == bound.software_view
    assert restored.software_binding == bound.software_binding


def test_software_change_does_not_replace_persistent_state_contract():
    program = ProgramSpec(
        name="web",
        command=("/applications/station-access/bin/station-access",),
        mounts=(("data", "/var/lib/station"),),
        user="1000",
    )
    preparation = ProgramSpec(
        name="migrate",
        command=("/applications/station-access/bin/station-access", "migrate"),
        mounts=(("data", "/var/lib/station"),),
        user="1000",
        execution_timeout_seconds=30,
    )
    first = ApplicationSpec(
        name="station-access",
        programs=(program,),
        persistent=True,
        writable_mounts=("data",),
        preparation_revision="schema-1",
        preparation=(preparation,),
        software_view="sha256:" + "1" * 64,
    )
    second = replace(first, software_view="sha256:" + "2" * 64)
    assert ApplicationStorage.signature(first) == ApplicationStorage.signature(second)


def test_pending_runtime_protects_view_and_artifacts_from_reclamation(tmp_path):
    project, selection, application, backend, frontend, view = fixture(tmp_path)
    bound = resolve_binding(project, application, selection)
    reference = ApplicationRuntimeReference(
        runtime_id="A7K3Q2",
        application=application.name,
        instance_id=application.name,
        generation=selection.generation,
        state=ApplicationRuntimeState.TERMINATED,
        cleanup_pending=True,
        software_view=view.view,
        software_binding_digest=bound.software_binding["binding_digest"],
        software_manifest_digest=bound.software_binding["view_manifest_digest"],
        software_artifacts=tuple(bound.software_binding["reclamation_protection"]["artifacts"]),
    )
    views, artifacts = protection_from_reference(reference)
    reclaimed_views, reclaimed_artifacts = ApplicationSoftwareReclaimer(project).reclaim_unreferenced(
        views, artifacts
    )
    assert reclaimed_views == ()
    assert reclaimed_artifacts == ()
    assert ApplicationSoftwareViewStore(project.state_dir).resolve(view.view).view == view.view
    assert ApplicationSoftwareStore(project.state_dir).resolve(backend.artifact).artifact == backend.artifact
    assert ApplicationSoftwareStore(project.state_dir).resolve(frontend.artifact).artifact == frontend.artifact


def test_unreferenced_view_and_artifacts_can_be_reclaimed(tmp_path):
    project, _selection, _application, backend, frontend, view = fixture(tmp_path)
    reclaimed_views, reclaimed_artifacts = ApplicationSoftwareReclaimer(project).reclaim_unreferenced(
        set(), set()
    )
    assert reclaimed_views == (view.view,)
    assert set(reclaimed_artifacts) == {backend.artifact, frontend.artifact}
