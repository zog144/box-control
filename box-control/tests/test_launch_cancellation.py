import importlib.util
from pathlib import Path
import pytest
from zog.box_control.errors import RuntimeOperationError
from zog.box_control.project import Project
from zog.box_control.requests import ApplicationRequestStatus

spec = importlib.util.spec_from_file_location('cancel_fixtures', Path(__file__).with_name('test_application_control.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


def setup(tmp_path):
    project = Project(tmp_path / 'project')
    f.write_application(project)
    transport = f.FakeTransport()
    control = f.control_for(project, tmp_path, transport, ['boot'])
    return project, transport, control


def test_cancel_queued_launch_is_durable_and_never_evaluated(tmp_path):
    project, transport, control = setup(tmp_path)
    request = control.request_application_launch('desktop')
    response = control.cancel_application_launch('desktop', request_id=request.request_id)
    assert response['status'] == 'cancelled'
    reopened = f.control_for(project, tmp_path, transport, ['boot'])
    assert reopened.cancel_application_launch('desktop', request_id=request.request_id) == response
    assert reopened.application_request_result(request.request_id).status == ApplicationRequestStatus.CANCELLED
    assert reopened.evaluate().ok
    assert not transport.started


def test_cancel_accepted_launch_returns_exact_runtime_without_terminating(tmp_path):
    project, transport, control = setup(tmp_path)
    request = control.request_application_launch('desktop')
    assert control.evaluate().ok
    result = control.application_request_result(request.request_id)
    response = control.cancel_application_launch('desktop', request_id=request.request_id)
    assert response['status'] == 'accepted'
    assert response['runtime_id'] == result.runtime_id
    assert control.current_application_runtimes()[0].runtime_id == result.runtime_id
    with pytest.raises(RuntimeOperationError, match='another application'):
        control.cancel_application_launch('different', request_id=request.request_id)
    with pytest.raises(RuntimeOperationError, match='missing or expired'):
        control.cancel_application_launch('desktop', request_id='missing')


def test_cancel_never_invokes_recovery_for_uncertain_operation(tmp_path, monkeypatch):
    from zog.box_control.operations import OperationStore, OperationManager
    project, transport, control = setup(tmp_path)
    request = control.request_application_launch('desktop')
    assert control.evaluate().ok
    store = OperationStore(project)
    record = store.records()[0]
    record.update(phase='launching', finished=False, published=False)
    store.save(record)
    monkeypatch.setattr(OperationManager, 'recover', lambda self: pytest.fail('cancellation must not run recovery'))
    before = len(transport.started)
    result = control.cancel_application_launch('desktop', request_id=request.request_id)
    assert result['status'] == 'uncertain'
    assert len(transport.started) == before
    assert store.records()[0]['phase'] == 'launching'


def test_interrupted_cancellation_resumes_without_launching(tmp_path, monkeypatch):
    from zog.box_control.requests import ApplicationRequestStore
    project, transport, control = setup(tmp_path)
    request = control.request_application_launch('desktop')
    original = ApplicationRequestStore.remove
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(ApplicationRequestStore, 'remove', interrupted)
    with pytest.raises(KeyboardInterrupt):
        control.cancel_application_launch('desktop', request_id=request.request_id)
    assert control.application_request_result(request.request_id).status == ApplicationRequestStatus.CANCELLED
    monkeypatch.setattr(ApplicationRequestStore, 'remove', staticmethod(original))
    reopened = f.control_for(project, tmp_path, transport, ['boot'])
    assert reopened.evaluate().ok
    assert not transport.started
    assert reopened.cancel_application_launch('desktop', request_id=request.request_id)['status'] == 'cancelled'
