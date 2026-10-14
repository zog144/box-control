import importlib.util
from pathlib import Path
import time

import pytest

from zog.box_control.errors import RuntimeOperationError, PersistenceError
from zog.box_control.project import Project
from zog.box_control.retention import Retention, WINDOW_SECONDS
from zog.box_control.requests import ApplicationRequest, ApplicationRequestOperation

spec = importlib.util.spec_from_file_location('retention_fixtures', Path(__file__).with_name('test_application_control.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [1000000.0]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    project = Project(tmp_path / 'project')
    fixtures.write_application(project)
    transport = fixtures.FakeTransport()
    control = fixtures.control_for(project, tmp_path, transport, ['boot'])
    return control, transport, clock, tmp_path


def complete(control):
    identity = control.issue_application_request_id()
    runtime = control.launch_application('desktop', request_id=identity)
    control.terminate_application_runtime(runtime.runtime_id)
    return identity, runtime


def test_unused_id_expires_and_cannot_be_forged_or_used_on_another_project(setup):
    control, transport, clock, tmp = setup
    identity = control.issue_application_request_id()
    assert not control._request_store().paths()
    other = fixtures.control_for(Project(tmp / 'other'), tmp, fixtures.FakeTransport(), ['boot'])
    for bad in [identity[:-1] + ('0' if identity[-1] != '0' else '1'), 'caller-chosen', '../escape']:
        with pytest.raises(RuntimeOperationError):
            control.request_application_launch('desktop', request_id=bad)
    with pytest.raises(RuntimeOperationError):
        other.request_application_launch('desktop', request_id=identity)
    clock[0] += WINDOW_SECONDS
    with pytest.raises(RuntimeOperationError, match='expired'):
        control.request_application_launch('desktop', request_id=identity)
    assert not transport.started


def test_completion_window_starts_at_completion_not_issuance(setup):
    control, transport, clock, _ = setup
    identity = control.issue_application_request_id()
    clock[0] += WINDOW_SECONDS - 10
    runtime = control.launch_application('desktop', request_id=identity)
    control.terminate_application_runtime(runtime.runtime_id)
    clock[0] += WINDOW_SECONDS - 1
    assert control.launch_application('desktop', request_id=identity).runtime_id == runtime.runtime_id
    assert len(transport.started) == 1
    clock[0] += 1
    with pytest.raises(RuntimeOperationError, match='expired'):
        control.launch_application('desktop', request_id=identity)
    assert control.application_request_result(identity) is None
    assert not any(r['request_id'] == identity for r in control.application_operations())
    assert len(transport.started) == 1


def test_accepted_queue_outlives_unused_deadline(setup):
    control, transport, clock, _ = setup
    request = control.request_application_launch('desktop')
    clock[0] += WINDOW_SECONDS * 3
    assert control.evaluate().ok
    assert control.application_request_result(request.request_id).status.value == 'satisfied'
    assert len(transport.started) == 1


def test_legacy_records_remain_retryable_then_are_rejected_after_pruning(setup):
    control, transport, clock, _ = setup
    identity = 'LEGACY-ACCEPTED'
    control._request_store().submit(ApplicationRequest(identity, ApplicationRequestOperation.LAUNCH,
                                                     application='desktop', created_at=clock[0]))
    assert control.evaluate().ok
    runtime = control.application_request_result(identity).runtime_id
    control.request_application_launch('desktop', request_id=identity)
    control.terminate_application_runtime(runtime)
    clock[0] += WINDOW_SECONDS
    with pytest.raises(RuntimeOperationError, match='expired'):
        control.request_application_launch('desktop', request_id=identity)
    assert len(transport.started) == 1


@pytest.mark.parametrize('cut', [0, 1, 2, 3])
def test_pruning_unlink_interruption_resumes_without_replay(setup, monkeypatch, cut):
    import zog.box_control.retention as module
    control, transport, clock, tmp = setup
    identity, _ = complete(control)
    clock[0] += WINDOW_SECONDS
    remove = module.remove_file
    count = [0]
    def interrupted(path):
        remove(path)
        if count[0] == cut:
            raise fixtures.SimulatedCrash()
        count[0] += 1
    monkeypatch.setattr(module, 'remove_file', interrupted)
    with pytest.raises(fixtures.SimulatedCrash):
        control.evaluate()
    monkeypatch.setattr(module, 'remove_file', remove)
    fresh = fixtures.control_for(Project(control.project.path), tmp, transport, ['boot'])
    assert fresh.evaluate().ok
    assert not Retention(fresh.project).journal.exists()
    with pytest.raises(RuntimeOperationError, match='expired'):
        fresh.request_application_launch('desktop', request_id=identity)
    assert fresh.application_request_result(identity) is None
    assert len(transport.started) == 1


def test_pruning_storage_failure_prevents_lifecycle_action(setup, monkeypatch):
    import zog.box_control.retention as module
    control, transport, clock, _ = setup
    old, _ = complete(control)
    clock[0] += WINDOW_SECONDS
    fresh = control.issue_application_request_id()
    remove = module.remove_file
    def fail(path):
        raise PersistenceError('unlink barrier failed')
    monkeypatch.setattr(module, 'remove_file', fail)
    with pytest.raises(PersistenceError):
        control.launch_application('desktop', request_id=fresh)
    assert len(transport.started) == 1
    assert Retention(control.project).journal.exists()
    monkeypatch.setattr(module, 'remove_file', remove)
    control.launch_application('desktop', request_id=fresh)
    assert len(transport.started) == 2
    with pytest.raises(RuntimeOperationError):
        control.launch_application('desktop', request_id=old)


def test_clock_rollback_cannot_resurrect_deleted_identity(setup):
    control, transport, clock, _ = setup
    identity, _ = complete(control)
    clock[0] += WINDOW_SECONDS
    assert control.evaluate().ok
    clock[0] -= WINDOW_SECONDS * 2
    with pytest.raises(RuntimeOperationError, match='expired'):
        control.request_application_launch('desktop', request_id=identity)
    assert len(transport.started) == 1


def test_unknown_outcome_and_cleanup_remain_protected_past_deadline(setup, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime
    control, transport, clock, _ = setup
    request = control.request_application_launch('desktop')
    launch = SystemdServiceRuntime.launch
    def crash(*a, **k):
        raise fixtures.SimulatedCrash()
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', crash)
    with pytest.raises(fixtures.SimulatedCrash):
        control.evaluate()
    clock[0] += WINDOW_SECONDS * 4
    # Exercise retention's selection independently: no lifecycle operation is
    # authorized here, and the pending operation must not become deletable.
    Retention(control.project).prune()
    assert any(r['request_id'] == request.request_id for r in control.application_operations())
    assert control._request_store().paths()
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', launch)
    assert control.evaluate().ok
    assert control.application_request_result(request.request_id).status.value == 'satisfied'


def test_pending_failure_cleanup_protects_records_regardless_of_age(setup):
    from dataclasses import replace
    control, _, clock, tmp = setup
    class Failed(fixtures.DelayedStopTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            name = kwargs['definition'].unit_name
            self.units[name] = replace(self.units[name], active_state='failed', result='exit-code')
    transport = Failed()
    control = fixtures.control_for(control.project, tmp, transport, ['boot'])
    request = control.request_application_launch('desktop')
    assert not control.evaluate().ok
    result = control.application_request_result(request.request_id)
    assert result.status.value == 'failed'
    clock[0] += WINDOW_SECONDS * 10
    Retention(control.project).prune()
    assert control.application_request_result(request.request_id) == result
    assert any(r['request_id'] == request.request_id for r in control.application_operations())
    assert control.application_runtime(result.runtime_id).cleanup_pending


def test_failed_validation_result_cannot_be_relaunched_via_direct_api(setup):
    control, transport, _, _ = setup
    request = control.request_application_launch('missing')
    assert not control.evaluate().ok
    assert control.application_request_result(request.request_id).status.value == 'failed'
    with pytest.raises(RuntimeOperationError):
        control.launch_application('missing', request_id=request.request_id)
    assert not transport.started


def test_pruning_keeps_operation_until_result_window_also_expires(setup):
    from dataclasses import replace
    control, transport, clock, _ = setup
    identity, reference = complete(control)
    result = control.application_request_result(identity)
    # Legacy results may have been published later than operation completion.
    clock[0] += 100
    control._request_store().save_result(replace(result, updated_at=clock[0]))
    clock[0] += WINDOW_SECONDS - 50
    assert control.launch_application('desktop', request_id=identity).runtime_id == reference.runtime_id
    assert len(transport.started) == 1


def test_failed_pruning_intent_write_deletes_nothing(setup, monkeypatch):
    import zog.box_control.retention as module
    control, transport, clock, _ = setup
    identity, _ = complete(control)
    clock[0] += WINDOW_SECONDS
    before = {p.name for p in control._reconciler().operations.store.directory.glob('*.json')}
    write = module.replace_json
    def fail(path, payload):
        if path == Retention(control.project).journal:
            raise PersistenceError('retention intent barrier failed')
        write(path, payload)
    monkeypatch.setattr(module, 'replace_json', fail)
    assert not control.evaluate().ok
    assert {p.name for p in control._reconciler().operations.store.directory.glob('*.json')} == before
    assert control.application_request_result(identity) is not None
    assert len(transport.started) == 1


def test_pruning_recovers_in_a_separate_process(setup):
    import os
    import subprocess
    import sys
    control, _, clock, tmp = setup
    identity, _ = complete(control)
    clock[0] += WINDOW_SECONDS
    driver = '''
import importlib.util, os, sys
from pathlib import Path
from zog.box_control.project import Project
from zog.box_control.errors import RuntimeOperationError
import zog.box_control.retention as retention
spec = importlib.util.spec_from_file_location('fixtures', sys.argv[1])
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
transport = f.FakeTransport()
c = f.control_for(Project(Path(sys.argv[2])), Path(sys.argv[3]), transport, ['boot'])
if sys.argv[4] == 'interrupt':
    remove = retention.remove_file
    def cut(path):
        remove(path)
        os._exit(17)
    retention.remove_file = cut
    c.evaluate()
else:
    assert c.evaluate().ok
    assert not retention.Retention(c.project).journal.exists()
    assert c.application_request_result(sys.argv[5]) is None
    try:
        c.request_application_launch('desktop', request_id=sys.argv[5])
    except RuntimeOperationError:
        pass
    else:
        raise AssertionError('expired identity was accepted')
    assert not transport.started
'''
    common = [sys.executable, '-c', driver, str(Path(__file__).with_name('test_application_control.py')),
              str(control.project.path), str(tmp)]
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    for phase, code in [('interrupt', 17), ('recover', 0)]:
        run = subprocess.run([*common, phase, identity], env=environment, capture_output=True, text=True, timeout=20)
        assert run.returncode == code, run.stderr
