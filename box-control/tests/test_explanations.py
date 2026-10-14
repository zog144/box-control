import importlib.util
import json
from pathlib import Path
import pytest
from zog.box_control import BoxControl, Project
from zog.box_control.locking import ProjectLock
from zog.box_control.durability import _blocked_projects

spec=importlib.util.spec_from_file_location('explanation_fixtures',Path(__file__).with_name('test_inspection.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)

@pytest.fixture
def control(tmp_path):
    project=Project(tmp_path/'project');f.fixtures.write_application(project)
    return f.fixtures.control_for(project,tmp_path,f.fixtures.FakeTransport(),['boot'])


def record(control, **changes):
    if not list((control.project.state_dir/'operation').glob('*.json')):
        control.launch_application('desktop')
    path=next((control.project.state_dir/'operation').glob('*.json'))
    raw=json.loads(path.read_text());raw.update(changes);path.write_text(json.dumps(raw))
    return raw


def test_absent_project_is_not_bootstrapped(tmp_path):
    project=Project(tmp_path/'absent')
    result=BoxControl(project).recovery_explanation()
    assert result['mutation_status']=='no-recorded-block'
    assert not project.path.exists()


def test_completed_record_is_read_only(control,monkeypatch):
    raw=record(control);before=f.snapshot(control.project)
    monkeypatch.setattr(control,'_reconciler',lambda:pytest.fail('must not mutate'))
    result=control.recovery_explanation(operation_id=raw['operation_id'])
    op=result['operations'][0]
    assert op['progress']=='completed' and op['outcome']=='committed'
    assert op['runtime_ids']==raw['runtime_ids'] and op['generations']
    assert not op['operation_protects_generations'] and op['next_actions']==[]
    assert before==f.snapshot(control.project)


@pytest.mark.parametrize('phase,finished,published,progress',[
    ('prepared',False,False,'prepared'),('stopping',False,False,'terminating'),
    ('launching',False,False,'launching'),('failed',False,True,'awaiting-cleanup'),
    ('committed',True,False,'awaiting-publication'),('abandoned',True,True,'completed')])
def test_progress_keeps_outcome_separate(control,phase,finished,published,progress):
    record(control,phase=phase,finished=finished,published=published)
    op=control.recovery_explanation()['operations'][0]
    assert op['progress']==progress
    assert op['operation_protects_generations']==(not (finished and published))
    if phase=='launching':
        assert any(r['code']=='launch-outcome-unresolved' for r in op['reasons'])
        assert op['outcome']=='not-recorded'
    if phase in ('failed','committed','abandoned'):
        assert not any(a['code']=='consider-explicit-abandonment' for a in op['next_actions'])


def test_missing_input_and_storage_remain_distinct(control):
    raw=record(control,phase='prepared',finished=False,published=False,attempted_units=[],generation_root=str(control.project.path/'gone'))
    _blocked_projects.add(control.project.path)
    try:
        result=control.recovery_explanation(operation_id=raw['operation_id'])
        assert {'prepared-input-unavailable','storage-interlock'}<={r['code'] for r in result['project_reasons']}
        assert {a['code'] for a in result['next_actions']} >= {'restore-recorded-input','restore-storage'}
    finally:_blocked_projects.discard(control.project.path)


def test_busy_snapshot_does_not_suggest_recovery(control):
    control.request_application_launch('desktop')
    with ProjectLock(control.project.lock_file):
        result=control.recovery_explanation(operation_id='a'*32)
    assert result['snapshot']=='busy' and result['selection_status']=='unavailable'
    assert result['operations']==[]
    assert [a['code'] for a in result['next_actions']]==['retry-inspection']


def test_queue_and_corruption(control):
    request=control.request_application_launch('desktop');before=f.snapshot(control.project)
    result=control.recovery_explanation()
    assert result['requests'][0]['progress']=='accepted-unprepared'
    assert result['requests'][0]['request_id']==request.request_id
    assert before==f.snapshot(control.project)
    raw=record(control,error={'invalid':'shape'})
    result=control.recovery_explanation(operation_id=raw['operation_id'])
    assert result['selection_status']=='unavailable'
    assert any(r['code']=='operation-unreadable' for r in result['project_reasons'])


def test_selection_does_not_hide_project_blocker(control):
    raw=record(control)
    (control.project.state_dir/'mutation-incomplete.json').write_text('null')
    result=control.recovery_explanation(operation_id=raw['operation_id'],limit=1)
    assert result['operations'][0]['progress']=='completed'
    assert result['mutation_status']=='recovery-required'
    assert result['project_reasons'][0]['code']=='mutation-marker-invalid'
    with pytest.raises(ValueError):control.recovery_explanation(limit=True)
    with pytest.raises(ValueError):control.recovery_explanation(operation_id='../bad')


def test_runtime_cleanup_links_to_direct_operation_without_request(control):
    from dataclasses import replace
    from zog.box_control.runtime.reference import RuntimeReferenceStore
    from zog.box_control.model import ApplicationRuntimeState
    raw=record(control)
    store=RuntimeReferenceStore(control.project.runtime_reference_file)
    refs=store.load();identity=raw['runtime_ids'][0]
    refs[identity]=replace(refs[identity],state=ApplicationRuntimeState.FAILED,cleanup_pending=True,error='saved failure')
    store.save(refs)
    result=control.recovery_explanation(operation_id=raw['operation_id'])
    op=result['operations'][0]
    assert op['progress']=='completed'
    assert any(r['code']=='cleanup-pending' and not r['blocking'] for r in op['reasons'])
    assert result['runtimes'][0]['cleanup_pending']


def test_truncation_and_request_signing_key_not_exposed(control):
    control.issue_application_request_id()
    key=json.loads((control.project.state_dir/'request-identity.json').read_text())['key']
    control.request_application_launch('desktop')
    control.request_application_launch('desktop')
    result=control.recovery_explanation(limit=1)
    assert result['requests_truncated'] and len(result['requests'])==1
    assert key not in json.dumps(result)
