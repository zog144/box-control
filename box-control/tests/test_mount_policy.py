import importlib.util
import json
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace
import pytest

from zog.box_control.errors import RuntimeOperationError, RecoveryRequired
from zog.box_control.mounts import validate, VERSION
from zog.box_control.runtime.reference import RuntimeReferenceStore
from zog.box_control.model import ApplicationSpec, ProgramSpec
from zog.box_control.project import Project
from zog.box_control.runtime.systemd import service_definition
from zog.root_control.systemd import BusctlSystemdBackend


def fixtures(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('target', ['/', '/usr/share', '/bin', '/lib64', '/opt/software', '/etc', '/proc/data', '/run/zog-workspace', '/tmp', '/var/../usr', '/data/', '//data'])
def test_prohibited_data_destinations(target, tmp_path):
    with pytest.raises(RuntimeOperationError):
        validate(tmp_path, [('source', target)])


def test_redirected_and_overlapping_destinations(tmp_path):
    (tmp_path / 'data').symlink_to('/usr')
    with pytest.raises(RuntimeOperationError):
        validate(tmp_path, [('source', '/data/lib')])
    with pytest.raises(RuntimeOperationError, match='overlapping'):
        validate(tmp_path, [('one','/srv/data'), ('two','/srv/data/nested')])
    with pytest.raises(RuntimeOperationError, match='writable application data'):
        validate(tmp_path, [('one','/state')], ['/state/venv/bin/python'])


def _systemd_definition(tmp_path):
    project = Project(tmp_path / "demo")
    generation = "generation-1"
    root = project.rootfs_dir / "generations" / generation / "root"
    root.mkdir(parents=True)
    for target in ("var/lib/browser", "tmp/.X11-unix"):
        (root / target).mkdir(parents=True)
    (root / ".box-control-rootfs.json").write_text(
        json.dumps({"fingerprint": generation}), encoding="utf-8"
    )
    application = ApplicationSpec(
        name="desktop",
        programs=(ProgramSpec(
            name="browser",
            command=("/usr/bin/browser",),
            mounts=(("data", "/var/lib/browser"),),
        ),),
    )
    definition = service_definition(
        project, application, "A7K3Q2", application.programs[0], generation_root=root
    )
    definition.bind_paths[0][0].mkdir(parents=True)
    return project, generation, definition


def test_actual_payload_and_missing_target_cannot_modify_generation(tmp_path):
    project, generation, definition = _systemd_definition(tmp_path)
    calls = []
    backend = BusctlSystemdBackend(jobs=SimpleNamespace(call=lambda *args, **kw: calls.append(args)))
    backend.start_service(project_root=project.path, generation=generation, definition=definition.to_transport_dict())
    properties = dict(calls[0][2][2])
    assert properties['RootDirectory'].value == str(definition.root_directory)
    assert properties['ProtectSystem'].value == 'strict'
    assert properties['PrivateTmp'].value is True
    assert properties['MountAPIVFS'].value is True
    assert properties['BindPaths'].signature == 'a(ssbt)'
    assert properties['BindPaths'].value == [[str(definition.bind_paths[0][0]), '/var/lib/browser', False, 0]]
    assert definition.mount_inventory()['software']['access'] == 'read-only'
    (definition.generation_root / 'var/lib/browser').rmdir()
    with pytest.raises(RuntimeOperationError, match='supplied by image-build'):
        backend.start_service(project_root=project.path, generation=generation, definition=definition.to_transport_dict())
    assert len(calls) == 1
    assert not (definition.generation_root / 'var/lib/browser').exists()


def test_persistent_inventory_survives_upgrade_and_inspection_is_read_only(tmp_path):
    f = fixtures('test_application_storage')
    project, transport, control = f.setup(tmp_path)
    control.prepare_application('portal'); f.complete(control, transport)
    first = control.launch_application('portal')
    inspected = control.application_runtime_mounts(first.runtime_id)
    assert inspected['schema'] == VERSION
    assert inspected['basis'] == 'expected-launched'
    inventory = inspected['programs'][0]['inventory']
    entry = inventory['mounts'][0]
    assert entry['lifetime'] == 'persistent'
    data = Path(entry['source']) / 'database'
    data.write_text('preserved')
    before = project.runtime_reference_file.read_bytes()
    assert control.application_runtime_mounts(first.runtime_id) == inspected
    assert project.runtime_reference_file.read_bytes() == before
    control.terminate_application_runtime(first.runtime_id)
    old_selection = control.image_provider.ensure
    from types import SimpleNamespace
    control.image_provider.ensure = lambda p: SimpleNamespace(**dict(vars(old_selection(p)), generation='generation-2'))
    control.prepare_application('portal', upgrade=True); f.complete(control, transport)
    second = control.launch_application('portal')
    other = control.application_runtime_mounts(second.runtime_id)['programs'][0]['inventory']['mounts'][0]
    assert (entry['source'], entry['owner_id']) == (other['source'], other['owner_id'])
    assert data.read_text() == 'preserved'
    assert first.runtime_id != second.runtime_id
    # Runtime references survive a fresh store load, not just in-memory objects.
    assert RuntimeReferenceStore(project.runtime_reference_file).load()[second.runtime_id].programs[0].mount_inventory is not None
    with pytest.raises(RecoveryRequired):
        control.delete_application_storage('portal', storage_id=entry['owner_id'])


def test_legacy_references_do_not_invent_inventory(tmp_path):
    f = fixtures('test_application_storage')
    project, transport, control = f.setup(tmp_path)
    control.prepare_application('portal'); f.complete(control, transport)
    runtime = control.launch_application('portal')
    store = RuntimeReferenceStore(project.runtime_reference_file)
    raw = store.encode(store.load())
    for item in raw['runtimes'].values():
        for program in item['programs']:
            program.pop('mount_inventory')
    store.save(store.decode(raw))
    inspected = control.application_runtime_mounts(runtime.runtime_id)
    assert inspected['programs'][0]['inventory'] is None
    assert inspected['programs'][0]['status'] == 'legacy-unrecorded'
    control.terminate_application_runtime(runtime.runtime_id)


def test_recovery_checks_frozen_mounts_before_stopping_old_runtime(tmp_path, monkeypatch):
    from zog.box_control.operations import OperationManager
    f = fixtures('test_application_storage')
    project, transport, control = f.setup(tmp_path)
    control.prepare_application('portal'); f.complete(control, transport)
    first = control.launch_application('portal')
    original = OperationManager.execute
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt('after durable preparation')
    monkeypatch.setattr(OperationManager, 'execute', interrupt)
    with pytest.raises(KeyboardInterrupt):
        control.launch_application('portal')
    monkeypatch.setattr(OperationManager, 'execute', original)
    root = control.image_provider.root
    (root / 'data').rmdir()
    (root / 'data').symlink_to('/usr')
    count = len(transport.started)
    with pytest.raises(RecoveryRequired, match='mount policy'):
        control.launch_application('portal')
    assert len(transport.started) == count
    assert transport.units[first.programs[0].unit_name].active
    assert (project.state_dir / 'mutation-incomplete.json').exists()


def test_missing_target_blocks_replacement_without_stopping_existing_runtime(tmp_path):
    f = fixtures('test_application_storage')
    project, transport, control = f.setup(tmp_path)
    control.prepare_application('portal'); f.complete(control, transport)
    first = control.launch_application('portal')
    (control.image_provider.root / 'data').rmdir()
    with pytest.raises(RuntimeOperationError, match='supplied by image-build'):
        control.launch_application('portal')
    assert transport.units[first.programs[0].unit_name].active


def test_workspace_targets_must_not_mutate_the_generation(tmp_path):
    from zog.box_control.mounts import validate_workspace_targets
    with pytest.raises(RuntimeOperationError, match='supplied without redirection'):
        validate_workspace_targets(tmp_path)
    assert not list(tmp_path.iterdir())

    # /run is supplied by the private runtime scaffold. Only the X11 target
    # remains an immutable-generation prerequisite.
    (tmp_path / 'tmp/.X11-unix').mkdir(parents=True)
    validate_workspace_targets(tmp_path)
    assert not (tmp_path / 'run').exists()

    (tmp_path / 'tmp/.X11-unix').rmdir()
    (tmp_path / 'tmp/.X11-unix').symlink_to('/usr')
    with pytest.raises(RuntimeOperationError, match='redirection'):
        validate_workspace_targets(tmp_path)
