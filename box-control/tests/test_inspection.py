import importlib.util
import json
from pathlib import Path
import fcntl

import pytest

from zog.box_control import BoxControl, Project
from zog.box_control.diagnostics import fault_from_exception
from zog.box_control.errors import PersistenceError

spec = importlib.util.spec_from_file_location('inspection_fixtures', Path(__file__).with_name('test_application_control.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@pytest.fixture
def setup(tmp_path):
    project = Project(tmp_path / 'project')
    fixtures.write_application(project)
    transport = fixtures.FakeTransport()
    control = fixtures.control_for(project, tmp_path, transport, ['boot'])
    return control, transport


def snapshot(project):
    return {str(p.relative_to(project.path)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in project.path.rglob('*') if p.is_file()}


def test_inspection_does_not_bootstrap_or_contact_transport(tmp_path):
    project = Project(tmp_path / 'absent')
    control = BoxControl(project)
    status = control.recovery_status()
    assert status.mutation_status == 'no-recorded-block'
    assert status.snapshot == 'unlocked'
    assert not project.path.exists()


def test_healthy_snapshot_is_serializable_and_read_only(setup, monkeypatch):
    control, transport = setup
    request = control.request_application_launch('desktop')
    assert control.evaluate().ok
    def fail(*args, **kwargs):
        raise AssertionError('inspection must not contact systemd')
    monkeypatch.setattr(control, '_reconciler', fail)
    before = snapshot(control.project)
    status = control.recovery_status()
    assert status.mutation_status == 'no-recorded-block'
    assert status.snapshot == 'locked'
    assert status.results[0]['request_id'] == request.request_id
    assert status.operations and status.runtimes
    assert not status.faults
    text = json.dumps(status.to_dict())
    key = json.loads((control.project.state_dir / 'request-identity.json').read_text())['key']
    assert key not in text
    assert snapshot(control.project) == before


@pytest.mark.parametrize('contents', ['{broken', 'null', '[]'])
def test_damaged_operation_does_not_hide_valid_records(setup, contents):
    control, _ = setup
    control.launch_application('desktop')
    path = control.project.state_dir / 'operation' / ('a' * 32 + '.json')
    path.write_text(contents)
    before = snapshot(control.project)
    status = control.recovery_status()
    assert status.operations and status.runtimes
    assert status.mutation_status == 'recovery-required'
    fault = next(f for f in status.faults if f.code == 'operation-unreadable')
    assert str(path) in fault.evidence
    assert snapshot(control.project) == before


@pytest.mark.parametrize('contents', ['null', '{broken', '{"schema":1,"status":"mutation-incomplete"}'])
def test_marker_blocks_without_being_removed(setup, contents):
    control, _ = setup
    control.issue_application_request_id()
    path = control.project.state_dir / 'mutation-incomplete.json'
    path.write_text(contents)
    status = control.recovery_status()
    assert status.mutation_status == 'recovery-required'
    assert path.read_text() == contents
    assert status.faults


def test_runtime_entries_are_decoded_independently(setup):
    control, _ = setup
    runtime = control.launch_application('desktop')
    path = control.project.runtime_reference_file
    raw = json.loads(path.read_text())
    raw['runtimes']['broken'] = None
    path.write_text(json.dumps(raw))
    status = control.recovery_status()
    assert status.runtimes[0]['runtime_id'] == runtime.runtime_id
    assert any(f.code == 'runtime-unreadable' for f in status.faults)


def test_busy_snapshot_does_not_wait_or_read_partial_records(setup):
    control, _ = setup
    control.request_application_launch('desktop')
    with control.project.lock_file.open('rb') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = control.recovery_status()
        assert status.snapshot == 'busy'
        assert status.mutation_status == 'unknown'
        assert not status.requests


def test_unfinished_operation_guidance_and_identities(setup):
    control, _ = setup
    request = control.request_application_launch('desktop')
    control.evaluate()
    path = next((control.project.state_dir / 'operation').glob('*.json'))
    record = json.loads(path.read_text())
    record.update(phase='launching', finished=False, published=False)
    path.write_text(json.dumps(record))
    status = control.recovery_status()
    fault = next(f for f in status.faults if f.code == 'operation-unfinished')
    assert fault.request_id == request.request_id
    assert fault.operation_id == record['operation_id']
    assert fault.runtime_ids == tuple(record['runtime_ids'])
    assert fault.guidance == 'evaluate-to-resolve-outcome'


def test_storage_failure_report_keeps_cause_and_inspection_available(setup, monkeypatch):
    control, _ = setup
    def fail():
        try:
            raise OSError('disk unavailable')
        except OSError as exc:
            raise PersistenceError('cannot save state') from exc
    monkeypatch.setattr(control, '_reconciler', fail)
    report = control.evaluate()
    assert not report.ok
    assert report.errors == ['cannot save state']
    assert report.faults[0].code == 'storage-failure'
    assert [c['type'] for c in report.faults[0].causes] == ['PersistenceError', 'OSError']
    assert control.recovery_status() is not None
    assert PersistenceError('bad').diagnostic().code == 'storage-failure'


def test_unreadable_directory_does_not_erase_other_evidence(setup, monkeypatch):
    control, _ = setup
    control.launch_application('desktop')
    original = Path.iterdir
    def fail(path):
        if path == control.project.application_request_dir:
            raise PermissionError('unreadable queue')
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', fail)
    status = control.recovery_status()
    assert status.operations
    assert any(f.code == 'directory-unreadable' for f in status.faults)


def test_retention_journal_is_inspected_without_pruning(setup):
    control, _ = setup
    control.launch_application('desktop')
    operation = control.application_operations()[0]
    path = control.project.state_dir / 'retention-incomplete.json'
    path.write_text(json.dumps({'schema': 1, 'deletions': [['operation', operation['operation_id']]]}))
    before = snapshot(control.project)
    status = control.recovery_status()
    assert any(f.code == 'retention-incomplete' for f in status.faults)
    assert snapshot(control.project) == before


def test_missing_prepared_input_reports_exact_operation(setup):
    control, _ = setup
    control.launch_application('desktop')
    path = next((control.project.state_dir / 'operation').glob('*.json'))
    record = json.loads(path.read_text())
    record.update(phase='prepared', finished=False, published=False, attempted_units=[],
                  generation_root=str(control.project.path / 'missing-root'))
    path.write_text(json.dumps(record))
    status = control.recovery_status()
    fault = next(f for f in status.faults if f.code == 'prepared-input-unavailable')
    assert fault.operation_id == record['operation_id']
    assert fault.guidance == 'restore-recorded-input'
    assert status.operations[0]['abandonment'] == 'request-if-outcome-remains-unknown'


def test_queued_configuration_failure_has_structured_fault(setup):
    control, _ = setup
    request = control.request_application_launch('missing')
    report = control.evaluate()
    assert not report.ok
    assert any(f.request_id == request.request_id for f in report.faults)
    assert report.errors


def test_null_identity_metadata_is_not_treated_as_absent(setup):
    control, _ = setup
    control.issue_application_request_id()
    path = control.project.state_dir / 'request-identity.json'
    path.write_text('null')
    status = control.recovery_status()
    assert any(f.code == 'request-identity-invalid' for f in status.faults)
    assert status.mutation_status == 'recovery-required'


def test_empty_unsupported_runtime_schema_is_a_fault(setup):
    control, _ = setup
    control.launch_application('desktop')
    path = control.project.runtime_reference_file
    path.write_text('{"schema":999,"runtimes":{}}')
    status = control.recovery_status()
    assert any(f.code == 'runtime-store-unreadable' for f in status.faults)
    assert status.mutation_status == 'recovery-required'
