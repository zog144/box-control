import importlib.util
from pathlib import Path
from dataclasses import replace
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import pytest

from zog.box_control import BoxControl, Project
from zog.box_control.locking import ProjectLock
from zog.box_control.runtime.reference import RuntimeReferenceStore, ApplicationRuntimeReference, ProgramRuntimeReference
from zog.box_control.runtime.root_control import RootControlSystemdTransport
from zog.box_control.model import ApplicationRuntimeState
from zog.root_control import journal

spec=importlib.util.spec_from_file_location('station_fixtures',Path(__file__).with_name('test_application_control.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)

@pytest.fixture
def control(tmp_path):
    project=Project(tmp_path/'project');f.write_application(project)
    return f.control_for(project,tmp_path,f.FakeTransport(),['boot'])


def test_direct_retry_and_lock_contention(control):
    request=control.issue_application_request_id()
    first=control.execute_application_operation('launch','desktop',request_id=request)
    assert first['status']=='completed'
    again=control.execute_application_operation('launch','desktop',request_id=request)
    assert first['runtime']['runtime_id']==again['runtime']['runtime_id']
    runtime=first['runtime']['runtime_id']
    stop=control.issue_application_request_id()
    stopped=control.execute_application_operation('terminate',runtime,request_id=stop)
    assert stopped['status']=='completed'
    assert control.execute_application_operation('terminate',runtime,request_id=stop)==stopped
    conflict=control.execute_application_operation('launch','desktop',request_id=stop)
    assert conflict['status']=='failed'
    control.mutation_lock_timeout_seconds=0
    marker=control.project.state_dir/'mutation-incomplete.json'
    with ProjectLock(control.project.lock_file):
        busy=control.execute_application_operation('launch','desktop',request_id=request)
    assert busy['status']=='busy' and not marker.exists()
    from zog.box_control.errors import ProjectBusy
    assert ProjectBusy('busy').diagnostic().code=='project-busy'


def test_fresh_inspection_never_mutates_and_detects_tampering(control):
    reference=control.launch_application('desktop')
    before=control.project.runtime_reference_file.read_bytes()
    with ProjectLock(control.project.lock_file):
        observed=control.observe_application_runtime(reference.runtime_id)
    assert all(p['status']=='observed' for p in observed['programs'])
    assert control.project.runtime_reference_file.read_bytes()==before
    original=control.systemd_transport.observe
    control.systemd_transport.observe=lambda **kw: replace(original(**kw),invocation_id='different')
    assert control.observe_application_runtime(reference.runtime_id)['programs'][0]['status']=='integrity-fault'
    control.systemd_transport.observe=lambda **kw: (_ for _ in ()).throw(OSError('offline'))
    assert control.observe_application_runtime(reference.runtime_id)['programs'][0]['status']=='unavailable'
    control.boot_id_provider=lambda:'next-boot'
    assert control.observe_application_runtime(reference.runtime_id)['programs'][0]['status']=='previous-boot'
    assert control.project.runtime_reference_file.read_bytes()==before


def test_transport_deadline_is_thread_local():
    t=RootControlSystemdTransport()
    with t.operation_budget(.1):
        with ThreadPoolExecutor() as pool:
            assert pool.submit(lambda:t.operation_deadline).result() is None
    assert t.operation_deadline is None


@pytest.fixture
def logs(tmp_path):
    project=Project(tmp_path)
    member=ProgramRuntimeReference('main','zog-test.service',invocation_id='a'*32)
    ref=ApplicationRuntimeReference('ABCDEF','app','app','gen',ApplicationRuntimeState.RUNNING,
                                    boot_id='b'*32,programs=(member,))
    RuntimeReferenceStore(project.runtime_reference_file).save({'ABCDEF':ref})
    return dict(project_root=str(tmp_path),runtime_id='ABCDEF',program='main')


def row(cursor,message='hello'):
    return {'__CURSOR':cursor,'MESSAGE':message,'_BOOT_ID':'b'*32,'_SYSTEMD_INVOCATION_ID':'a'*32,
            '__REALTIME_TIMESTAMP':'1','PRIORITY':'6'}


def test_cursor_pagination_binding_and_gap(logs,monkeypatch):
    calls=[]
    def query(command,**kw):calls.append(command);return [row('one')]
    monkeypatch.setattr(journal,'run_bounded',query)
    first=journal.read_logs(**logs,limit=1)
    assert first['entries'][0]['message']=='hello'
    assert '_BOOT_ID='+'b'*32 in calls[0]
    assert '_SYSTEMD_INVOCATION_ID='+'a'*32 in calls[0]
    responses=iter([[row('one'),row('two'),row('three')]])
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:next(responses))
    second=journal.read_logs(**logs,cursor=first['next_cursor'],limit=1)
    assert second['has_more'] and second['next_cursor']!=first['next_cursor']
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:[row('replacement')])
    assert journal.read_logs(**logs,cursor=first['next_cursor'])['status']=='cursor-unavailable'
    with pytest.raises(ValueError):journal.read_logs(**logs,cursor='bad')
    store=RuntimeReferenceStore(Project(Path(logs['project_root'])).runtime_reference_file)
    refs=store.load();refs['ABCDEF']=replace(refs['ABCDEF'],programs=(replace(refs['ABCDEF'].programs[0],invocation_id='c'*32),));store.save(refs)
    with pytest.raises(ValueError):journal.read_logs(**logs,cursor=first['next_cursor'])


def test_empty_missing_identity_and_read_failure(logs,monkeypatch):
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:[])
    assert journal.read_logs(**logs)['status']=='empty-history'
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:(_ for _ in ()).throw(TimeoutError('late')))
    assert journal.read_logs(**logs)['status']=='unavailable'
    with pytest.raises(ValueError):journal.read_logs(**logs,limit=101)
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:[dict(row('x'),_SYSTEMD_INVOCATION_ID='d'*32)])
    assert journal.read_logs(**logs)['entries']==[]


def test_reader_bounds_bytes_and_time():
    with pytest.raises(ValueError,match='byte budget'):
        journal.run_bounded([sys.executable,'-c','print("x"*300000)'],deadline=time.monotonic()+2)
    with pytest.raises(TimeoutError):
        journal.run_bounded([sys.executable,'-c','import time;time.sleep(10)'],deadline=time.monotonic()+.05)


def test_diagnostic_has_frames_without_locals(control,monkeypatch):
    def broken(*a,**kw):
        secret='not-for-diagnostics'
        raise RuntimeError('boom')
    monkeypatch.setattr(control,'launch_application',broken)
    result=control.execute_application_operation('launch','desktop',request_id='request')
    assert result['fault']['causes'][0]['frames'][-1]['function']=='broken'
    assert 'not-for-diagnostics' not in json.dumps(result)


def test_log_rpc_is_read_only_during_mutation_block(logs,monkeypatch):
    from zog.root_control.daemon import RootControlDaemon
    project=Project(Path(logs['project_root']))
    marker=project.state_dir/'mutation-incomplete.json'
    marker.write_text('{"schema":1,"status":"mutation-incomplete"}')
    before={str(p):p.read_bytes() for p in project.path.rglob('*') if p.is_file()}
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:[row('one')])
    daemon=RootControlDaemon(project.path/'unused.sock',systemd_backend=object())
    with ProjectLock(project.lock_file):
        response=daemon.handle(dict(operation='application_logs',**logs))
        assert response['ok'] and response['result']['entries']
    assert all(Path(p).read_bytes()==data for p,data in before.items())
    with pytest.raises(TypeError):
        daemon.handle(dict(operation='application_logs',unit_name='other.service',**logs))
    with pytest.raises(ValueError):
        daemon.handle(dict(operation='application_logs',**dict(logs,program='other')))


def test_no_new_logs_preserves_cursor_and_binary_messages(logs,monkeypatch):
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:[row('one',[65,255])])
    page=journal.read_logs(**logs)
    assert page['entries'][0]['message']=='A\ufffd'
    following=journal.read_logs(**logs,cursor=page['next_cursor'])
    assert following['status']=='ok' and following['entries']==[]
    assert following['next_cursor']==page['next_cursor']
    monkeypatch.setattr(journal,'run_bounded',lambda *a,**kw:[row('two',None)])
    assert journal.read_logs(**logs)['entries'][0]['message_unavailable']


def test_lifecycle_events_follow_durability_and_correlate(control,caplog,monkeypatch):
    import logging
    from zog.box_control import operations
    from zog.box_control.errors import PersistenceError
    caplog.set_level(logging.INFO,logger='zog.box_control.lifecycle')
    request=control.issue_application_request_id()
    launched=control.launch_application('desktop',request_id=request)
    events=[record.zog_event for record in caplog.records if hasattr(record,'zog_event')]
    prepared=next(e for e in events if e['event']=='operation.recorded' and e['phase']=='prepared')
    start=next(e for e in events if e['event']=='service.start.requested')
    assert prepared['operation_id']==start['operation_id']
    assert start['request_id']==request and start['runtime_id']==launched.runtime_id
    assert start['program'] and 'command' not in start
    stop_request=control.issue_application_request_id()
    control.terminate_application_runtime(launched.runtime_id,request_id=stop_request)
    events=[record.zog_event for record in caplog.records if hasattr(record,'zog_event')]
    assert any(e['event']=='runtime.cleanup.returned' and e['request_id']==stop_request and not e['cleanup_pending'] for e in events)
    caplog.clear()
    def fail(*a,**kw):raise PersistenceError('disk failure')
    monkeypatch.setattr(operations,'replace_json',fail)
    with pytest.raises(PersistenceError):operations.OperationStore(control.project).save({'operation_id':'a'*32})
    assert not caplog.records


def test_broken_event_handler_does_not_break_lifecycle(control,monkeypatch):
    from zog.box_control import events
    def broken(*a,**kw):raise RuntimeError('bad logging handler')
    monkeypatch.setattr(events.logger,'info',broken)
    reference=control.launch_application('desktop')
    assert reference.runtime_id
    assert control.terminate_application_runtime(reference.runtime_id).cleanup_pending is False


def test_event_formatter_preserves_correlation():
    import logging
    from zog.box_control.events import EventFormatter
    record=logging.LogRecord('zog.box_control.lifecycle',logging.INFO,'',0,'operation.started',(),None)
    record.zog_event={'event':'operation.started','operation_id':'operation','request_id':'request'}
    value=json.loads(EventFormatter().format(record))
    assert value['operation_id']=='operation' and value['request_id']=='request'


@pytest.mark.parametrize("log_operation", ["application_logs", "build_job_logs", "application_processes", "build_job_processes"])
def test_journal_lane_is_independent_bounded_and_mutations_serial(monkeypatch,tmp_path,log_operation):
    import socket
    from threading import BoundedSemaphore, Event
    from concurrent.futures import ThreadPoolExecutor
    from zog.root_control.daemon import RootControlDaemon
    from zog.root_control.protocol import decode
    daemon=RootControlDaemon(tmp_path/'unused',systemd_backend=object())
    release=Event();entered=Event();second=Event()
    def handle(request):
        if request['operation']=='slow':entered.set();release.wait(3)
        elif request['operation']=='next':second.set()
        return {'ok':True,'result':request['operation']}
    monkeypatch.setattr(daemon,'handle',handle)
    clients=[]
    def dispatch(request,workers,slots):
        client,server=socket.socketpair();clients.append(client)
        daemon._dispatch(server,request,workers,slots)
        return client
    try:
        with ThreadPoolExecutor(max_workers=1) as mutations,ThreadPoolExecutor(max_workers=2) as logs:
            workers=(mutations,logs);slots=(BoundedSemaphore(8),BoundedSemaphore(2))
            dispatch({'operation':'slow'},workers,slots);assert entered.wait(1)
            dispatch({'operation':'next'},workers,slots)
            client=dispatch({'operation':log_operation},workers,slots)
            assert decode(client,deadline=time.monotonic()+1)['result']==log_operation
            assert not second.is_set()
            deadline=time.monotonic()+1
            while not slots[1].acquire(False):
                assert time.monotonic()<deadline
            assert slots[1].acquire(timeout=1)
            busy=dispatch({'operation':log_operation},workers,slots)
            assert not decode(busy,deadline=time.monotonic()+1)['ok']
            slots[1].release();slots[1].release()
            release.set()
        assert second.is_set()
    finally:
        release.set()
        for client in clients:client.close()
