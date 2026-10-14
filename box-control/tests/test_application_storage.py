from dataclasses import replace
import importlib.util
from pathlib import Path
import pytest

from zog.box_control.errors import RecoveryRequired, RuntimeOperationError
from zog.box_control.project import Project
from zog.box_control.application_storage import ApplicationStorage
from zog.box_control.runtime.reference import RuntimeReferenceStore

spec = importlib.util.spec_from_file_location('storage_fixtures', Path(__file__).with_name('test_application_control.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class Transport(fixtures.FakeTransport):
    def sync_application_storage(self, **kwargs):
        from zog.root_control.systemd import BusctlSystemdBackend
        BusctlSystemdBackend().sync_application_storage(**kwargs)

    def delete_application_storage(self, **kwargs):
        from zog.root_control.systemd import BusctlSystemdBackend
        backend = BusctlSystemdBackend()
        backend.observe = lambda **kw: {'exists': kw['unit_name'] in self.units}
        backend.delete_application_storage(**kwargs)

    def start_service(self, **kwargs):
        for source, _ in kwargs['definition'].bind_paths:
            source.mkdir(parents=True, exist_ok=True)
        super().start_service(**kwargs)


def declaration(project, revision='v1', command='/usr/bin/setup'):
    path = project.application_dir / 'portal' / 'application.py'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'''application(name="portal", persistent=True,
        writable_mounts=("data", "python"), preparation_revision={revision!r},
        preparation=(program(name="setup", command=({command!r},),
            execution_timeout_seconds=60, mounts={{"data":"/data", "python":"/python"}}),),
        programs=(program(name="web", command=("/usr/bin/server",), mounts={{"data":"/data", "python":"/python"}}),
                  program(name="scheduler", command=("/usr/bin/scheduler",), mounts={{"data":"/data"}})))''')


def setup(tmp_path):
    project = Project(tmp_path / 'project')
    declaration(project)
    transport = Transport()
    control = fixtures.control_for(project, tmp_path, transport, ['boot-1'])
    return project, transport, control


def complete(control, transport, result='success'):
    raw = control.application_preparation('portal')
    ref = control.application_runtime(raw['steps'][-1]['runtime_id'])
    unit = ref.programs[0].unit_name
    transport.units[unit] = replace(transport.units[unit], active_state='inactive', sub_state='dead', result=result, main_pid=None)
    return control.refresh_application_preparation('portal')


def test_storage_reused_across_runtime_replacements_and_reopen(tmp_path):
    project, transport, control = setup(tmp_path)
    with pytest.raises(RuntimeOperationError, match='preparation'):
        control.launch_application('portal')
    raw = control.prepare_application('portal')
    assert raw['state'] == 'preparing'
    assert not control.current_application_runtimes('portal')
    with pytest.raises(RecoveryRequired):
        control.launch_application('portal')
    raw = complete(control, transport)
    assert raw['state'] == 'ready'
    source = project.mounts_dir / 'persistent' / raw['storage_id'] / 'data'
    (source / 'database').write_text('retained')
    first = control.launch_application('portal')
    second = control.launch_application('portal')
    assert first.runtime_id != second.runtime_id
    paths = [dict(p.expected_properties)['BindPaths'][0][0] for p in second.programs]
    assert paths == [str(source)] * 2
    control.terminate_application_runtime(second.runtime_id)
    control = fixtures.control_for(project, tmp_path, transport, ['boot-2'])
    third = control.launch_application('portal')
    assert third.runtime_id not in {first.runtime_id, second.runtime_id}
    assert (source / 'database').read_text() == 'retained'
    assert control.application_preparation('portal')['storage_id'] == raw['storage_id']


def test_new_generation_requires_explicit_upgrade_and_idle_application(tmp_path):
    project, transport, control = setup(tmp_path)
    control.prepare_application('portal'); complete(control, transport)
    runtime = control.launch_application('portal')
    original = control.image_provider.ensure
    control.image_provider.ensure = lambda p: type('Selection', (), dict(vars(original(p)), generation='generation-2'))()
    with pytest.raises(RuntimeOperationError):
        control.launch_application('portal')
    assert transport.units[runtime.programs[0].unit_name].active
    with pytest.raises(RuntimeOperationError, match='upgrade'):
        control.prepare_application('portal')
    with pytest.raises(RuntimeOperationError, match='stop'):
        control.prepare_application('portal', upgrade=True)
    control.terminate_application_runtime(runtime.runtime_id)
    before = control.application_preparation('portal')['storage_id']
    control.prepare_application('portal', upgrade=True)
    assert complete(control, transport)['generation'] == 'generation-2'
    assert control.application_preparation('portal')['storage_id'] == before


def test_interrupted_completion_is_not_replayed_and_requires_explicit_abandonment(tmp_path):
    project, transport, control = setup(tmp_path)
    raw = control.prepare_application('portal')
    transport.units.clear()
    reopened = fixtures.control_for(project, tmp_path, transport, ['boot-2'])
    assert reopened.refresh_application_preparation('portal')['state'] == 'uncertain'
    assert reopened.refresh_application_preparation('portal')['state'] == 'uncertain'
    assert len(transport.started) == 1
    with pytest.raises(RecoveryRequired):
        reopened.launch_application('portal')
    assert reopened.abandon_application_preparation('portal')['state'] == 'failed'
    with pytest.raises(RuntimeOperationError, match='retry'):
        reopened.prepare_application('portal', upgrade=True)
    reopened.prepare_application('portal', upgrade=True, retry=True)
    assert len(transport.started) == 2
    assert reopened.application_preparation('portal')['attempts'][0]['state'] == 'failed'


def test_failed_migration_evidence_persists_and_definition_needs_revision(tmp_path):
    project, transport, control = setup(tmp_path)
    control.prepare_application('portal')
    raw = complete(control, transport, 'exit-code')
    assert raw['state'] == 'failed'
    assert ApplicationStorage(project).load('portal')['steps'][0]['outcome'] == 'exit-code'
    declaration(project, command='/usr/bin/different')
    with pytest.raises(RuntimeOperationError, match='revision'):
        control.prepare_application('portal', upgrade=True, retry=True)
    declaration(project, revision='v2', command='/usr/bin/different')
    control.prepare_application('portal', upgrade=True, retry=True)
    assert complete(control, transport)['state'] == 'ready'


def test_explicit_storage_deletion_requires_idle_runtime_and_identity(tmp_path):
    project, transport, control = setup(tmp_path)
    control.prepare_application('portal'); raw = complete(control, transport)
    ref = control.launch_application('portal')
    with pytest.raises(RecoveryRequired):
        control.delete_application_storage('portal', storage_id=raw['storage_id'])
    control.refresh_application_preparation('portal')
    control.terminate_application_runtime(ref.runtime_id)
    with pytest.raises(RuntimeOperationError, match='identity'):
        control.delete_application_storage('portal', storage_id='incorrect')
    control.refresh_application_preparation('portal')
    assert control.delete_application_storage('portal', storage_id=raw['storage_id'])['state'] == 'deleted'
    assert not (project.mounts_dir / 'persistent' / raw['storage_id']).exists()
    assert control.prepare_application('portal')['storage_id'] != raw['storage_id']


def test_crash_after_start_intent_before_transport_never_replays(tmp_path, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime
    project, transport, control = setup(tmp_path)
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', interrupted)
    with pytest.raises(KeyboardInterrupt):
        control.prepare_application('portal')
    assert not transport.started
    assert (project.state_dir / 'mutation-incomplete.json').exists()
    reopened = fixtures.control_for(project, tmp_path, transport, ['boot-1'])
    assert reopened.refresh_application_preparation('portal')['state'] == 'uncertain'
    assert not transport.started
    assert not reopened.evaluate().ok


def test_durable_completion_survives_crash_before_cleanup(tmp_path, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime
    project, transport, control = setup(tmp_path)
    control.prepare_application('portal')
    original = SystemdServiceRuntime.terminate
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(SystemdServiceRuntime, 'terminate', interrupted)
    with pytest.raises(KeyboardInterrupt):
        complete(control, transport)
    assert control.application_preparation('portal')['steps'][0]['outcome'] == 'success'
    transport.units.clear()
    monkeypatch.setattr(SystemdServiceRuntime, 'terminate', original)
    reopened = fixtures.control_for(project, tmp_path, transport, ['boot-2'])
    assert reopened.refresh_application_preparation('portal')['state'] == 'ready'
    assert len(transport.started) == 1


def test_missing_prepared_data_is_not_silently_recreated(tmp_path):
    import shutil
    project, transport, control = setup(tmp_path)
    control.prepare_application('portal'); raw = complete(control, transport)
    shutil.rmtree(project.mounts_dir / 'persistent' / raw['storage_id'] / 'data')
    with pytest.raises(RuntimeOperationError, match='missing'):
        control.launch_application('portal')
    assert len(transport.started) == 1


def test_preparation_references_are_not_pruned_as_application_history(tmp_path):
    project, transport, control = setup(tmp_path)
    control.prepare_application('portal'); raw = complete(control, transport)
    store = RuntimeReferenceStore(project.runtime_reference_file)
    refs = store.load()
    store.prune_completed(refs, limit=0)
    assert raw['steps'][0]['runtime_id'] in refs


def test_prepared_tree_sync_failure_does_not_publish_readiness(tmp_path, monkeypatch):
    from zog.box_control.errors import PersistenceError
    project, transport, control = setup(tmp_path)
    control.prepare_application('portal')
    original = transport.sync_application_storage
    def failed(**kwargs):
        raise PersistenceError('injected storage barrier failure')
    monkeypatch.setattr(transport, 'sync_application_storage', failed)
    with pytest.raises(PersistenceError):
        complete(control, transport)
    assert control.application_preparation('portal')['state'] != 'ready'
    assert not control.evaluate().ok
    monkeypatch.setattr(transport, 'sync_application_storage', original)
    assert control.refresh_application_preparation('portal')['state'] == 'ready'
    assert len(transport.started) == 1


def test_multiple_steps_use_bound_definition_until_upgrade(tmp_path):
    project, transport, control = setup(tmp_path)
    path = project.application_dir / 'portal' / 'application.py'
    text = path.read_text().replace('execution_timeout_seconds=60, mounts={"data":"/data", "python":"/python"}),),',
        'execution_timeout_seconds=60, mounts={"data":"/data", "python":"/python"}), program(name="migrate", command=("/usr/bin/migrate",), execution_timeout_seconds=60, mounts={"data":"/data"})),')
    path.write_text(text)
    control.prepare_application('portal')
    declaration(project, revision='v2', command='/usr/bin/new-setup')
    raw = complete(control, transport)
    assert raw['state'] == 'preparing'
    assert raw['steps'][-1]['program'] == 'migrate'
    assert len(transport.started) == 2
    assert complete(control, transport)['revision'] == 'v1'
    with pytest.raises(RuntimeOperationError, match='upgrade'):
        control.launch_application('portal')


class FastExitTransport(Transport):
    """Model a real Type=exec preparation that ends before launch returns."""
    def start_service(self, **kwargs):
        super().start_service(**kwargs)
        unit = kwargs['definition'].unit_name
        self.units[unit] = replace(
            self.units[unit], active_state='inactive',
            sub_state='dead', result='success', main_pid=None,
        )


def fast_exit_setup(tmp_path):
    project = Project(tmp_path / 'project')
    declaration(project)
    transport = FastExitTransport()
    boot = ['boot-1']
    control = fixtures.control_for(project, tmp_path, transport, boot)
    raw = control.prepare_application('portal')
    assert raw['state'] == 'preparing'
    assert raw['steps'][0]['outcome'] is None
    runtime_id = raw['steps'][0]['runtime_id']
    saved = RuntimeReferenceStore(project.runtime_reference_file).load()[runtime_id]
    assert saved.state.value == 'terminated'
    assert saved.programs[0].result == 'success'
    assert saved.programs[0].invocation_id
    assert not saved.cleanup_pending
    assert not transport.observe(project_root=project.path, unit_name=saved.programs[0].unit_name).exists
    return project, transport, control, boot, raw, saved


def test_fast_exiting_preparation_uses_saved_terminal_result_after_unit_cleanup(tmp_path):
    project, transport, control, boot, raw, saved = fast_exit_setup(tmp_path)
    finished = control.refresh_application_preparation('portal')
    assert finished['state'] == 'ready'
    step = finished['steps'][0]
    assert step['outcome'] == 'success'
    assert step['invocation_id'] == saved.programs[0].invocation_id
    assert step['boot_id'] == saved.boot_id
    assert step['completion_source'] == 'durable-terminal-runtime'
    assert step['cleanup_complete'] and not finished['cleanup_pending']
    assert len(transport.started) == 1
    # Reopen/restart uses the saved identity and never replays the migration.
    reopened = fixtures.control_for(project, tmp_path, transport, boot)
    assert reopened.refresh_application_preparation('portal')['state'] == 'ready'
    assert len(transport.started) == 1


def test_fast_exiting_preparation_terminal_evidence_survives_controller_reboot(tmp_path):
    project, transport, control, boot, raw, saved = fast_exit_setup(tmp_path)
    boot[0] = 'boot-2'
    reopened = fixtures.control_for(project, tmp_path, transport, boot)
    finished = reopened.refresh_application_preparation('portal')
    assert finished['state'] == 'ready'
    assert finished['steps'][0]['invocation_id'] == saved.programs[0].invocation_id
    assert len(transport.started) == 1


@pytest.mark.parametrize('invalid', [
    'missing-invocation', 'invocation-mismatch', 'boot-mismatch',
    'missing-boot', 'unit-mismatch', 'program-mismatch',
    'wrong-application', 'wrong-instance', 'wrong-generation',
    'unfinished', 'missing-completion', 'failed-result', 'saved-error',
    'wrong-attempt',
])
def test_fast_exiting_preparation_requires_exact_durable_completion_evidence(tmp_path, invalid):
    project, transport, control, boot, raw, ref = fast_exit_setup(tmp_path)
    store = RuntimeReferenceStore(project.runtime_reference_file)
    step = raw['steps'][0]
    if invalid == 'missing-invocation':
        ref = replace(ref, programs=(replace(ref.programs[0], invocation_id=None),))
    elif invalid == 'invocation-mismatch':
        step['invocation_id'] = 'different-invocation'
    elif invalid == 'boot-mismatch':
        step['boot_id'] = 'other-boot'
    elif invalid == 'missing-boot':
        ref = replace(ref, boot_id=None)
    elif invalid == 'unit-mismatch':
        step['unit_name'] = 'other.service'
    elif invalid == 'program-mismatch':
        step['program'] = 'another-program'
    elif invalid == 'wrong-application':
        ref = replace(ref, application='another-application')
    elif invalid == 'wrong-instance':
        ref = replace(ref, instance_id='prepare-not-this-storage')
    elif invalid == 'wrong-generation':
        ref = replace(ref, generation='other-generation')
    elif invalid == 'unfinished':
        ref = replace(ref, cleanup_pending=True)
    elif invalid == 'missing-completion':
        ref = replace(ref, completed_at=None)
    elif invalid == 'failed-result':
        ref = replace(ref, programs=(replace(ref.programs[0], result='exit-code'),))
    elif invalid == 'saved-error':
        ref = replace(ref, error='not actually accepted')
    elif invalid == 'wrong-attempt':
        step['attempt_id'] = 'different-attempt'
    references = store.load()
    references[ref.runtime_id] = ref
    store.save(references)
    ApplicationStorage(project).save(raw)
    evidence = control.refresh_application_preparation('portal')
    assert evidence['state'] == 'uncertain'
    assert evidence['steps'][0]['outcome'] is None
    assert evidence['cleanup_pending']
    assert 'do not replay' in evidence['error']
    assert len(transport.started) == 1


def test_legacy_unbound_boot_preparation_requires_same_boot_for_absent_unit(tmp_path):
    project, transport, control, boot, raw, saved = fast_exit_setup(tmp_path)
    del raw['steps'][0]['boot_id']
    ApplicationStorage(project).save(raw)
    boot[0] = 'boot-2'
    reopened = fixtures.control_for(project, tmp_path, transport, boot)
    result = reopened.refresh_application_preparation('portal')
    assert result['state'] == 'uncertain'
    assert result['steps'][0]['outcome'] is None
    assert len(transport.started) == 1
