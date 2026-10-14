import json
import os
import stat
import subprocess
import sys

import pytest

from zog.box_control import durability
from zog.box_control.api import BoxControl
from zog.box_control.errors import PersistenceError, RecoveryRequired
from zog.box_control.project import Project


def project_at(path):
    project = Project(path)
    project.require_state_allowed = lambda: None
    return project


def test_replace_orders_file_and_directory_barriers(tmp_path, monkeypatch):
    events = []
    real_sync, real_replace = os.fsync, os.replace

    def sync(fd):
        events.append('file' if stat.S_ISREG(os.fstat(fd).st_mode) else 'directory')
        real_sync(fd)

    def rename(source, destination):
        events.append('replace')
        real_replace(source, destination)

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'replace', rename)
    target = tmp_path / 'nested' / 'state.json'
    durability.replace_json(target, {'new': True})
    assert events[-3:] == ['file', 'replace', 'directory']
    assert json.loads(target.read_text()) == {'new': True}
    assert not list(target.parent.glob('*.pending'))


@pytest.mark.parametrize('failure', ['file_sync', 'rename', 'directory_sync'])
def test_replace_fault_does_not_acknowledge_or_destroy_complete_json(tmp_path, monkeypatch, failure):
    target = tmp_path / 'state.json'
    durability.replace_json(target, {'old': True})
    real_sync, real_replace = os.fsync, os.replace
    renamed = False

    def sync(fd):
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        if (failure == 'file_sync' and regular) or (failure == 'directory_sync' and renamed):
            raise OSError('injected barrier failure')
        real_sync(fd)

    def rename(source, destination):
        nonlocal renamed
        if failure == 'rename':
            raise OSError('injected rename failure')
        real_replace(source, destination)
        renamed = True

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'replace', rename)
    with pytest.raises(PersistenceError):
        durability.replace_json(target, {'new': True})
    assert json.loads(target.read_text()) == ({'new': True} if renamed else {'old': True})
    if not renamed:
        assert len(list(tmp_path.glob('*.pending'))) == 1


def test_unlink_barrier_runs_even_when_name_already_absent(tmp_path, monkeypatch):
    target = tmp_path / 'request.json'
    durability.replace_json(target, {})
    calls = []
    monkeypatch.setattr(durability, 'synchronize_directory', lambda path: calls.append(path))
    durability.remove_file(target)
    durability.remove_file(target)
    assert not target.exists()
    assert calls == [tmp_path, tmp_path]


def test_new_directory_parent_barriers(tmp_path, monkeypatch):
    calls = []
    real_sync = durability.synchronize_directory

    def sync(path):
        calls.append(path)
        real_sync(path)

    monkeypatch.setattr(durability, 'synchronize_directory', sync)
    target = tmp_path / 'one' / 'two'
    durability.ensure_directory(target)
    assert calls[-4:] == [target.parent, tmp_path, target, target.parent]


def test_storage_fault_blocks_fresh_facade_but_allows_inspection(tmp_path):
    project = project_at(tmp_path / 'project')
    with pytest.raises(PersistenceError):
        with durability.mutation_guard(project):
            raise PersistenceError('injected storage fault')
    control = BoxControl(project)
    with pytest.raises(RecoveryRequired):
        control.request_application_launch('desktop')
    assert control.application_runtimes() == {}
    assert not control.evaluate().ok


def test_failed_final_unlink_barrier_latches_even_without_visible_marker(tmp_path, monkeypatch):
    project = project_at(tmp_path / 'project')
    real_remove = durability.remove_file

    def remove(path):
        real_remove(path)
        raise PersistenceError('final directory sync failed')

    monkeypatch.setattr(durability, 'remove_file', remove)
    with pytest.raises(PersistenceError):
        with durability.mutation_guard(project):
            pass
    assert not (project.state_dir / 'mutation-incomplete.json').exists()
    with pytest.raises(RecoveryRequired):
        with durability.mutation_guard(project):
            pytest.fail('must not run another mutation')


def test_process_exit_leaves_durable_block_for_new_process(tmp_path):
    project = tmp_path / 'project'
    code = '''
import os, sys
from pathlib import Path
from zog.box_control.project import Project
from zog.box_control.durability import mutation_guard
p = Project(Path(sys.argv[1]))
p.require_state_allowed = lambda: None
with mutation_guard(p):
    os._exit(17)
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run([sys.executable, '-c', code, str(project)], env=env)
    assert result.returncode == 17
    with pytest.raises(RecoveryRequired):
        with durability.mutation_guard(project_at(project)):
            pytest.fail('interrupted session must block')


def test_successful_request_submission_clears_guard_and_persists_intent(tmp_path):
    project = project_at(tmp_path / 'project')
    control = BoxControl(project)
    request = control.request_application_launch('desktop')
    assert not (project.state_dir / 'mutation-incomplete.json').exists()
    assert control._request_store().load(project.application_request_dir / f'{request.request_id}.json') == request


@pytest.mark.parametrize('where', ['action', 'result', 'remove'])
def test_request_storage_fault_keeps_queue_and_stops_processing(tmp_path, monkeypatch, where):
    from zog.box_control.model import ReconcileReport
    from zog.box_control.reconcile import Reconciler
    from zog.box_control.requests import ApplicationRequest, ApplicationRequestOperation, ApplicationRequestStore
    from zog.box_control.runtime.systemd import SystemdServiceRuntime

    project = project_at(tmp_path / 'project')
    store = ApplicationRequestStore(project.application_request_dir, project.application_request_result_dir)
    for identity in ('FIRST1', 'SECOND2'):
        store.submit(ApplicationRequest(request_id=identity,
            operation=ApplicationRequestOperation.LAUNCH, application='desktop', created_at=1.0))
    reconciler = Reconciler(project, image_provider=object(),
        systemd_runtime=SystemdServiceRuntime(project, object()), boot_id_provider=lambda: 'boot-1')
    actions = []

    def launch(*args, **kwargs):
        actions.append(kwargs['request_id'])
        if where == 'action':
            raise PersistenceError('injected action persistence fault')
        return None

    def fail(*args, **kwargs):
        raise PersistenceError('injected persistence fault')

    monkeypatch.setattr(reconciler, '_launch_application_loaded', launch)
    if where == 'result':
        monkeypatch.setattr(reconciler.requests, 'save_result', fail)
    if where == 'remove':
        monkeypatch.setattr(reconciler.requests, 'remove', fail)
    with pytest.raises(PersistenceError):
        reconciler._process_requests(applications={}, closure=None, references={},
            selection=None, boot_id='boot-1', report=ReconcileReport(ok=True))
    assert actions == ['FIRST1']
    assert len(store.paths()) == 2
    assert store.result('SECOND2') is None
    if where != 'remove':
        assert store.result('FIRST1') is None


def test_evaluation_state_fault_bypasses_report_handler_and_keeps_block(tmp_path, monkeypatch):
    project = project_at(tmp_path / 'project')
    control = BoxControl(project)

    operations = control._reconciler().operations

    class Reconciler:
        def __init__(self):
            self.operations = operations

        def reconcile(self):
            raise PersistenceError('state sync failed')

    monkeypatch.setattr(control, '_reconciler', lambda: Reconciler())
    report = control.evaluate()
    assert not report.ok
    assert 'state sync failed' in report.errors[0]
    assert (project.state_dir / 'mutation-incomplete.json').exists()
    assert 'recovery required' in control.evaluate().errors[0]


def test_state_directory_fault_is_inside_session_guard(tmp_path, monkeypatch):
    project = project_at(tmp_path / 'project')

    def fail_setup():
        raise PersistenceError('state directory sync failed')

    monkeypatch.setattr(project, 'ensure_state', fail_setup)
    control = BoxControl(project)
    with pytest.raises(PersistenceError, match='state directory'):
        control.request_application_launch('desktop')
    assert (project.state_dir / 'mutation-incomplete.json').exists()
    monkeypatch.setattr(project, 'ensure_state', lambda: None)
    with pytest.raises(RecoveryRequired):
        control.request_application_launch('desktop')


def test_partial_temporary_write_leaves_previous_state(tmp_path, monkeypatch):
    from contextlib import contextmanager

    target = tmp_path / 'state.json'
    durability.replace_json(target, {'old': True})
    real_fdopen = os.fdopen

    @contextmanager
    def partial_writer(fd, mode):
        with real_fdopen(fd, mode) as stream:
            class Writer:
                def write(self, data):
                    stream.write(data[:3])
                    raise OSError('disk full during write')
            yield Writer()

    monkeypatch.setattr(os, 'fdopen', partial_writer)
    with pytest.raises(PersistenceError):
        durability.replace_json(target, {'new': True})
    assert json.loads(target.read_text()) == {'old': True}
    assert len(list(tmp_path.glob('*.pending'))) == 1
