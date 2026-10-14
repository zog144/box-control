import importlib.util
from pathlib import Path
import json
import pytest
from zog.box_control import BoxControl, Project
from zog.box_control.locking import ProjectLock
spec=importlib.util.spec_from_file_location('preflight_fixtures',Path(__file__).with_name('test_inspection.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)

@pytest.fixture
def control(tmp_path,monkeypatch):
    project=Project(tmp_path/'project');f.fixtures.write_application(project)
    c=f.fixtures.control_for(project,tmp_path,f.fixtures.FakeTransport(),['boot'])
    selection=c.image_provider.ensure(project)
    c.image_provider.preview=lambda project:selection
    monkeypatch.setattr(c.image_provider,'ensure',lambda *a:pytest.fail('must not build'))
    monkeypatch.setattr(c,'_reconciler',lambda:pytest.fail('must not reconcile'))
    return c

def test_ready_read_only(control):
    before=f.snapshot(control.project)
    result=control.preflight_application_launch('desktop')
    assert result['status']=='ready-to-attempt'
    assert result['generation']=='generation-1'
    assert not result['replacement_runtime_ids']
    assert f.snapshot(control.project)==before
    assert not control.systemd_transport.started
    json.dumps(result)

def test_absent(tmp_path):
    project=Project(tmp_path/'absent')
    c=BoxControl(project,systemd_transport=f.fixtures.FakeTransport())
    assert c.preflight_application_launch('unknown')['status']=='blocked'
    assert not project.path.exists()

@pytest.mark.parametrize('case,status',[('missing','blocked'),('unsupported','unable-to-verify'),('error','unable-to-verify'),('bad-manifest','blocked')])
def test_image(control,case,status):
    if case=='unsupported':control.image_provider.preview=None
    elif case=='missing':control.image_provider.preview=lambda p:None
    elif case=='error':
        def fail(p):raise OSError('unreadable')
        control.image_provider.preview=fail
    else:control.image_provider.preview(control.project).manifest['fingerprint']='wrong'
    assert control.preflight_application_launch('desktop')['status']==status

@pytest.mark.parametrize('version,status',[(249,'blocked'),(None,'unable-to-verify')])
def test_transport(control,version,status):
    def get():
        if version is None:raise TimeoutError()
        return version
    control.systemd_transport.version=get
    assert control.preflight_application_launch('desktop')['status']==status

def test_busy(control):
    def fail(*a):pytest.fail('must not query external inputs')
    control.image_provider.preview=fail;control.systemd_transport.version=fail
    with ProjectLock(control.project.lock_file):result=control.preflight_application_launch('desktop')
    assert result['status']=='unable-to-verify' and result['snapshot']=='busy'

def test_marker(control):
    control.project.state_dir.mkdir(parents=True,exist_ok=True)
    (control.project.state_dir/'mutation-incomplete.json').write_text('{}')
    before=f.snapshot(control.project)
    assert control.preflight_application_launch('desktop')['status']=='blocked'
    assert f.snapshot(control.project)==before

def test_singleton(tmp_path):
    project=Project(tmp_path/'project');f.fixtures.write_application(project)
    c=f.fixtures.control_for(project,tmp_path,f.fixtures.FakeTransport(),['boot'])
    runtime=c.launch_application('desktop');c.image_provider.preview=c.image_provider.ensure
    before=f.snapshot(project)
    assert c.preflight_application_launch('desktop')['replacement_runtime_ids']==[runtime.runtime_id]
    assert f.snapshot(project)==before

@pytest.mark.parametrize('state', ['failed', 'terminated'])
@pytest.mark.parametrize('cleanup_pending', [True, False])
def test_terminal_singleton_cleanup_candidates(tmp_path, state, cleanup_pending):
    from dataclasses import replace
    from zog.box_control.model import ApplicationRuntimeState
    from zog.box_control.runtime.reference import RuntimeReferenceStore
    from zog.box_control.reconcile import Reconciler

    project=Project(tmp_path/'project'); f.fixtures.write_application(project)
    c=f.fixtures.control_for(project,tmp_path,f.fixtures.FakeTransport(),['boot'])
    runtime=c.launch_application('desktop')
    store=RuntimeReferenceStore(project.runtime_reference_file)
    references=store.load()
    references[runtime.runtime_id]=replace(runtime,state=ApplicationRuntimeState(state),
                                          cleanup_pending=cleanup_pending)
    store.save(references)
    c.image_provider.preview=c.image_provider.ensure
    before=f.snapshot(project)
    expected=[runtime.runtime_id] if cleanup_pending else []
    assert c.preflight_application_launch('desktop')['replacement_runtime_ids']==expected
    assert [r.runtime_id for r in Reconciler._live_references(references,application='desktop')]==expected
    assert not store.current(references,application='desktop')
    assert f.snapshot(project)==before
