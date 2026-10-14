import importlib.util
import json
from pathlib import Path

import pytest

from zog.box_control.errors import PersistenceError, RecoveryRequired, RuntimeOperationError
from zog.root_control.build import canonical_request, root_path

spec = importlib.util.spec_from_file_location('build_fixtures',Path(__file__).with_name('test_application_control.py'))
fixtures = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixtures)


class BuildTransport(fixtures.FakeTransport):
    def __init__(self):
        super().__init__(); self.build_starts=0; self.live=False; self.exit=0; self.failure=None; self.cleanup=True
        self.invocation='a'*32; self.result='success'; self.ended=True
    def build_call(self, operation, *, project_root, **kwargs):
        if self.failure==operation: raise OSError('lost reply')
        if operation in ('register','release','forget'): return {}
        if operation=='prepare':
            key=kwargs['job_id']; return dict(unit_name='unit-'+key,slice_name='slice-'+key,resolved_executable='/bin/cc')
        if operation=='start': self.build_starts+=1; return {}
        if operation=='observe':
            return dict(exists=True,transient=True,invocation_id=self.invocation,active_state='active' if self.live else 'inactive',
                        cgroup_empty=not self.live,ExecMainCode=1,ExecMainStatus=self.exit,
                        ExecMainExitTimestampMonotonic=1 if self.ended else 0,result=self.result)
        if operation=='cleanup': return self.cleanup
        if operation=='cancel': self.live=False; self.exit=15; self.result='signal'; return None
        raise AssertionError(operation)


@pytest.fixture
def setup(tmp_path):
    transport=BuildTransport()
    project=fixtures.Project(tmp_path/'project'); fixtures.write_application(project)
    control=fixtures.control_for(project,tmp_path,transport,['boot'])
    resource='a'*32
    control.register_build_root(resource_id=resource,prepared_root='/root/input',source_directory='/root/source',output_directory='/root/output',
                                input_manifest_id='manifest',execution_user_id=1001,execution_group_id=1001)
    request=dict(build_root_id=resource,source_workspace_id=resource+'-source',output_workspace_id=resource+'-output',command=['cc','--version'],
                 environment={'PATH':'/bin'},working_directory='/image-build/source',execution_user_id=1001,execution_group_id=1001,
                 startup_timeout_seconds=5,execution_timeout_seconds=10,termination_grace_seconds=3,
                 resource_limits={'thread-count-maximum':32,'memory-maximum-bytes':100000000},read_only_root=True,network_access=False)
    return control,transport,request


def submit(setup):
    c,t,r=setup
    return c.submit_build_job(request_id=c.issue_build_request_id(),**r)


def test_success_duplicate_and_workspace_continuity(setup):
    c,t,r=setup; request_id=c.issue_build_request_id()
    first=c.submit_build_job(request_id=request_id,**r)
    assert first['outcome']=='success' and first['exit_code']==0
    assert first['process_cleanup_complete'] and not first['resources_released']
    assert c.submit_build_job(request_id=request_id,**r)==first
    assert t.build_starts==1
    assert submit(setup)['outcome']=='success'
    assert t.build_starts==2
    with pytest.raises(RuntimeOperationError): c.submit_build_job(request_id=request_id,**dict(r,command=['false']))


def test_workspace_cannot_have_two_writers(setup):
    c,t,r=setup; t.live=True
    first=submit(setup)
    assert first['state']=='running'
    with pytest.raises(RuntimeOperationError): submit(setup)
    assert t.build_starts==1


@pytest.mark.parametrize('exit_code,result,outcome',[(2,'exit-code','nonzero-exit'),(0,'timeout','timeout'),(0,'resources','fault')])
def test_result_classification(setup,exit_code,result,outcome):
    c,t,r=setup; t.exit=exit_code; t.result=result
    assert submit(setup)['outcome']==outcome


def test_no_exit_evidence_cannot_be_success(setup):
    c,t,r=setup; t.ended=False
    assert submit(setup)['outcome']=='fault'


def test_missing_invocation_cannot_be_success(setup):
    c,t,r=setup; t.invocation=None
    assert submit(setup)['outcome']=='fault'


def test_lost_start_reply_never_resubmits(setup):
    c,t,r=setup; request_id=c.issue_build_request_id(); t.failure='start'
    with pytest.raises(RecoveryRequired): c.submit_build_job(request_id=request_id,**r)
    assert (c.project.state_dir/'mutation-incomplete.json').exists()
    t.failure=None
    job=c.submit_build_job(request_id=request_id,**r)
    assert job['state']=='starting'
    assert c.refresh_build_job(job['job_id'])['outcome']=='success'
    assert t.build_starts==0


def test_evidence_saved_before_cleanup_failure(setup):
    c,t,r=setup; request_id=c.issue_build_request_id(); t.failure='cleanup'
    with pytest.raises(RecoveryRequired): c.submit_build_job(request_id=request_id,**r)
    from zog.box_control.build_jobs import BuildJobs
    saved=c.inspect_build_job(BuildJobs.job_id(request_id))
    assert saved['outcome']=='success' and saved['exit_code']==0
    t.failure=None
    assert c.refresh_build_job(saved['job_id'])['process_cleanup_complete']
    assert t.build_starts==1


def test_release_requires_cleanup_and_explicit_output_consumption(setup):
    c,t,r=setup; t.cleanup=False
    job=submit(setup)
    with pytest.raises(RuntimeOperationError): c.release_build_job(job['job_id'])
    t.cleanup=True; c.refresh_build_job(job['job_id'])
    with pytest.raises(RuntimeOperationError): c.release_build_root(r['build_root_id'])
    assert c.release_build_job(job['job_id'])['resources_released']
    assert c.release_build_root(r['build_root_id'])['state']=='released'


def test_clock_expiry_rejects_completed_id(setup,monkeypatch):
    c,t,r=setup; job=submit(setup)
    monkeypatch.setattr('time.time',lambda:job['completed_at']+48*3600)
    with pytest.raises(RuntimeOperationError): c.submit_build_job(request_id=job['request_id'],**r)
    assert t.build_starts==1


def test_build_domain_rejects_application_id(setup):
    c,t,r=setup
    with pytest.raises(RuntimeOperationError): c.submit_build_job(request_id=c.issue_application_request_id(),**r)
    assert t.build_starts==0


def test_configuration_snapshot_does_not_follow_caller_mutation(setup):
    c,t,r=setup; t.live=True; job=submit(setup)
    r['environment']['PATH']='/changed'
    assert c.inspect_build_job(job['job_id'])['request']['environment']['PATH']=='/bin'


@pytest.mark.parametrize('field,value',[('execution_user_id',0),('network_access',True),('read_only_root',False),('execution_timeout_seconds',float('inf'))])
def test_invalid_policy_starts_nothing(setup,field,value):
    c,t,r=setup
    with pytest.raises((RuntimeOperationError,ValueError)): c.submit_build_job(request_id=c.issue_build_request_id(),**dict(r,**{field:value}))
    assert t.build_starts==0


def test_root_resolution_never_follows_absolute_link_on_host(tmp_path):
    root=tmp_path/'root'; root.mkdir(); (root/'bin').mkdir(); (root/'usr').mkdir(); (root/'usr'/'bin').mkdir()
    (root/'usr'/'bin'/'cc').write_text('compiler')
    (root/'bin'/'cc').symlink_to('/usr/bin/cc')
    path,virtual=root_path(root,tmp_path/'source',tmp_path/'output','/bin/cc')
    assert path==root/'usr'/'bin'/'cc' and virtual=='/usr/bin/cc'


def test_adapter_persists_identity_and_resumes_after_caller_loss(setup,tmp_path):
    from zog.image_build.box_control_adapter import BoxControlExecutionAdapter,BuildExecutionPending
    from zog.image_build.runner import BuildExecutionRequest
    c,t,r=setup
    package=tmp_path/'package'; package.mkdir()
    root,source,output=[package/n for n in ('root','source','output')]
    for p in (root,source,output): p.mkdir()
    request=BuildExecutionRequest(root,source,output,('cc',),{'PATH':'/bin'},'/image-build/source',10)
    config=dict(execution_user_id=1001,execution_group_id=1001,startup_timeout_seconds=5,termination_grace_seconds=3,
                wait_timeout_seconds=0,resource_limits=r['resource_limits'],input_manifest_id='manifest')
    adapter=BoxControlExecutionAdapter(c,**config)
    checkpoint=package/'compile.controller.json'
    t.live=True
    with pytest.raises(BuildExecutionPending) as pending:
        adapter.execute_for_attempt(request,checkpoint)
    assert pending.value.request_id==json.loads(checkpoint.read_text())['request_id']
    assert pending.value.job_id==json.loads(checkpoint.read_text())['job_id']
    t.live=False
    c.refresh_build_job(pending.value.job_id)
    recovered=BoxControlExecutionAdapter(c,**config).execute_for_attempt(request,checkpoint)
    assert recovered.exit_code==0 and recovered.cleanup_complete
    assert t.build_starts==1
    adapter.execute_for_attempt(request,package/'test.controller.json')
    assert t.build_starts==2
    assert len(list(package.glob('controller-resources.json')))==1


def test_inspection_reports_pending_build_without_backend_access(setup):
    c,t,r=setup; t.live=True; job=submit(setup)
    t.failure='observe'
    report=c.recovery_status()
    assert report.build_jobs[0]['job_id']==job['job_id']
    assert report.build_resources


def test_released_build_records_prune_without_replaying_old_id(setup,monkeypatch):
    c,t,r=setup; job=submit(setup)
    c.release_build_job(job['job_id'])
    c.release_build_root(r['build_root_id'])
    monkeypatch.setattr('time.time',lambda:job['completed_at']+49*3600)
    with pytest.raises(RuntimeOperationError): c.submit_build_job(request_id=job['request_id'],**r)
    assert not list((c.project.state_dir/'build-job').glob('*.json'))
    assert not list((c.project.state_dir/'build-resource').glob('*.json'))
    assert t.build_starts==1


def test_storage_failure_before_start_prevents_execution(setup,monkeypatch):
    from zog.box_control.build_jobs import BuildJobs
    c,t,r=setup; request_id=c.issue_build_request_id()
    save=BuildJobs.save
    def fail(self,raw):
        if raw.get('state')=='starting': raise PersistenceError('disk unavailable')
        return save(self,raw)
    monkeypatch.setattr(BuildJobs,'save',fail)
    with pytest.raises(PersistenceError): c.submit_build_job(request_id=request_id,**r)
    assert t.build_starts==0
    monkeypatch.setattr(BuildJobs,'save',save)
    assert c.refresh_build_job(BuildJobs.job_id(request_id))['outcome']=='success'
    assert t.build_starts==1


def test_interrupted_build_pruning_resumes_before_new_work(setup,monkeypatch):
    c,t,r=setup; job=submit(setup)
    c.release_build_job(job['job_id']); c.release_build_root(r['build_root_id'])
    monkeypatch.setattr('time.time',lambda:job['completed_at']+49*3600)
    t.failure='forget'
    assert not c.evaluate().ok
    assert (c.project.state_dir/'build-pruning.json').exists()
    t.failure=None
    with pytest.raises(RuntimeOperationError): c.submit_build_job(request_id=job['request_id'],**r)
    assert not (c.project.state_dir/'build-pruning.json').exists()
    assert t.build_starts==1


def test_missing_attempt_after_reboot_stays_unknown_until_explicit_cancel(setup):
    c,t,r=setup; t.live=True; job=submit(setup)
    original=t.build_call
    def absent(operation,**kwargs):
        if operation=='observe': return {'exists':False}
        return original(operation,**kwargs)
    t.build_call=absent
    unknown=c.refresh_build_job(job['job_id'])
    assert unknown['outcome']=='unknown' and not unknown['process_cleanup_complete']
    assert c.cancel_build_job(job['job_id'])['outcome']=='unknown'
    assert c.inspect_build_job(job['job_id'])['process_cleanup_complete']
    assert t.build_starts==1


def test_build_logs_survive_cleanup_and_reject_other_job_cursor(setup, monkeypatch):
    from zog.root_control import journal
    from zog.box_control.build_jobs import BuildJobs
    c,t,r=setup
    job=submit(setup); store=BuildJobs(c)
    job['boot_id']='b'*32; store.save(job)
    def query(command, **kwargs):
        assert '_SYSTEMD_INVOCATION_ID='+job['invocation_id'] in command
        return [dict(__CURSOR='first', _BOOT_ID='b'*32,
                     _SYSTEMD_INVOCATION_ID=job['invocation_id'], MESSAGE='compiler output')]
    monkeypatch.setattr(journal,'run_bounded',query)
    from zog.root_control.daemon import RootControlDaemon
    daemon=RootControlDaemon(c.project.path/'unused.sock',systemd_backend=object())
    t.build_job_logs=lambda **kw: daemon.handle(dict(operation='build_job_logs', **kw))['result']
    page=c.build_job_logs(job['job_id'])
    assert page['entries'][0]['message']=='compiler output'
    assert job['process_cleanup_complete']
    c.release_build_job(job['job_id'])
    assert c.build_job_logs(job['job_id'])['entries']==page['entries']
    other=submit(setup); other['boot_id']='b'*32; store.save(other)
    with pytest.raises(ValueError): c.build_job_logs(other['job_id'],cursor=page['next_cursor'])
    with pytest.raises(ValueError): c.build_job_logs(job['job_id'],limit=0)
    assert t.build_starts==2


def test_build_listing_is_read_only_and_paged(setup):
    c,t,r=setup
    a=submit(setup); b=submit(setup)
    marker=c.project.state_dir/'mutation-incomplete.json'; marker.write_text('{"blocked":true}')
    first=c.list_build_jobs(limit=1)
    second=c.list_build_jobs(after=first['next_after'],limit=1)
    assert first['has_more'] and not second['has_more']
    assert {first['jobs'][0]['job_id'],second['jobs'][0]['job_id']}=={a['job_id'],b['job_id']}
    assert marker.exists() and t.build_starts==2
    with pytest.raises(ValueError): c.list_build_jobs(limit=True)


def test_build_view_mapping_before_acceptance_and_after_lost_reply(setup,tmp_path):
    from zog.image_build.build_views import read_build_commands
    from zog.box_control.build_jobs import BuildJobs
    c,t,r=setup
    attempt=tmp_path/'attempt'; package=attempt/'packages'/'gcc'; package.mkdir(parents=True)
    view=dict(schema=1,attempt_id='attempt',stage_id='bootstrap',package='gcc',phase='build',
              command_index=0,command=['make'],checkpoint='build-0.controller.json')
    (package/'build-0.view.json').write_text(json.dumps(view))
    assert read_build_commands(attempt)['commands'][0]['job_id'] is None
    request_id=c.issue_build_request_id()
    (package/view['checkpoint']).write_text(json.dumps(dict(request_id=request_id)))
    command=read_build_commands(attempt)['commands'][0]
    assert command['job_id']==BuildJobs.job_id(request_id)
    assert command['stage_id']=='bootstrap' and t.build_starts==0



def test_cancellation_stop_timeout_keeps_evidence(setup):
    c,t,r=setup
    t.live=True
    job=submit(setup)
    original=t.build_call
    def stop_timeout(operation,**kwargs):
        result=original(operation,**kwargs)
        if operation=='cancel': t.result='timeout'
        return result
    t.build_call=stop_timeout
    result=c.cancel_build_job(job['job_id'])
    assert result['outcome']=='cancelled'
    assert result['service_result']=='timeout'
    assert result['process_cleanup_complete']
    assert c.inspect_build_job(job['job_id'])['service_result']=='timeout'


def test_completed_timeout_is_not_reclassified_by_cancel(setup):
    c,t,r=setup
    t.result='timeout'
    job=submit(setup)
    assert c.cancel_build_job(job['job_id'])['outcome']=='timeout'


def test_late_registration_reconciles_same_identity_and_keeps_guard_until_ready(setup):
    from zog.box_control.errors import RootControlReplyTimeout
    c,t,_ = setup
    rid='f'*32
    intent=dict(prepared_root='/root/late',source_directory='/root/late-source',output_directory='/root/late-output',
                input_manifest_id='late-manifest',execution_user_id=1001,execution_group_id=1001)
    original=t.build_call
    registrations=[]
    remote={'resource_id':rid,'intent':intent,'state':'importing','phase':'sync-source'}
    def call(operation,**kw):
        if operation=='register':
            registrations.append(kw)
            if len(registrations)==1:raise RootControlReplyTimeout('build_register',60)
            return remote
        return original(operation,**kw)
    t.build_call=call
    t.build_registration_status=lambda **kw:dict(remote)
    with pytest.raises(RecoveryRequired):c.register_build_root(resource_id=rid,**intent)
    marker=c.project.state_dir/'mutation-incomplete.json'
    assert marker.exists()
    assert c.inspect_build_root(rid)['phase']=='sync-source'
    assert marker.exists() # observation cannot clear the recovery interlock
    remote['intent']=dict(intent,input_manifest_id='wrong')
    with pytest.raises(RecoveryRequired,match='differs'):c.inspect_build_root(rid)
    remote.update(intent=intent,state='ready',phase='ready')
    assert c.register_build_root(resource_id=rid,**intent)['state']=='ready'
    assert not marker.exists() and t.build_starts==0
    assert registrations[0]==registrations[1]


@pytest.mark.parametrize('code,status,result,outcome',[(1,0,'success','success'),(1,7,'exit-code','nonzero-exit'),(2,15,'signal','signal')])
def test_unloaded_unit_recovers_durable_exit_without_relaunch(setup,code,status,result,outcome):
    c,t,r=setup;t.live=True
    c.boot_id_provider=lambda:'11111111-2222-3333-4444-555555555555'
    job=submit(setup);old=t.build_call
    evidence=dict(boot_id='11111111222233334444555555555555',invocation_id=t.invocation,ExecMainCode=code,ExecMainStatus=status,
                  ExecMainExitTimestampMonotonic=10,result=result)
    def call(operation,**kwargs):
        if operation=='observe':return dict(exists=False,terminal_evidence=evidence)
        return old(operation,**kwargs)
    t.build_call=call
    recovered=c.refresh_build_job(job['job_id'])
    assert recovered['outcome']==outcome and recovered['process_cleanup_complete']
    assert c.inspect_build_job(job['job_id'])['outcome']==outcome
    assert t.build_starts==1


@pytest.mark.parametrize('field,value',[('boot_id','other'),('invocation_id','b'*32)])
def test_unloaded_unit_rejects_mismatched_evidence(setup,field,value):
    c,t,r=setup;t.live=True;job=submit(setup);old=t.build_call
    evidence=dict(boot_id='boot',invocation_id=t.invocation,ExecMainCode=1,ExecMainStatus=0,
                  ExecMainExitTimestampMonotonic=10,result='success')
    evidence[field]=value
    t.build_call=lambda operation,**kw: dict(exists=False,terminal_evidence=evidence) if operation=='observe' else old(operation,**kw)
    with pytest.raises(RecoveryRequired,match='evidence'):c.refresh_build_job(job['job_id'])
    assert not c.inspect_build_job(job['job_id'])['process_cleanup_complete']


def test_cpu_weight_is_part_of_canonical_build_policy(setup):
    from zog.box_control.build_protocol import canonical_request
    _,_,request=setup
    weighted=dict(request,resource_limits=dict(request['resource_limits'],**{'cpu-weight':50}))
    assert canonical_request(weighted)['resource_limits']['cpu-weight']==50


@pytest.mark.parametrize('value',[0,-1,True,10001])
def test_invalid_cpu_weight_rejected(setup,value):
    from zog.box_control.build_protocol import canonical_request
    _,_,request=setup
    weighted=dict(request,resource_limits=dict(request['resource_limits'],**{'cpu-weight':value}))
    with pytest.raises(RuntimeOperationError):canonical_request(weighted)
