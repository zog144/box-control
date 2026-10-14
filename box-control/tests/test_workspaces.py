from dataclasses import replace
import importlib.util
from pathlib import Path
from uuid import uuid4
import pytest
from zog.box_control.project import Project
from zog.box_control.workspaces import WorkspaceError
from zog.box_control.errors import RecoveryRequired

spec = importlib.util.spec_from_file_location('workspace_fixtures', Path(__file__).with_name('test_application_control.py'))
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)


class Transport(f.FakeTransport):
    def workspace_call(self, operation, *, project_root, binding, **kwargs):
        if operation == 'prepare':
            base = project_root / 'state' / 'workspace-resources' / binding['incarnation']
            for child in ('x11', 'access'):
                (base / child).mkdir(parents=True)
            return dict(display=':' + str(binding['number']), namespace_path='/proc/1/ns/net',
                        namespace_device=1, namespace_inode=2, x11_directory=str(base / 'x11'),
                        access_directory=str(base / 'access'), vnc_endpoint={'kind':'unix', 'path':str(base / 'access' / 'vnc.sock')})
        if binding['boot_id'] != self.boot[0]:
            raise WorkspaceError('stale-boot', 'desktop binding belongs to old boot')
        if binding['role'] == 'client':
            ref = self.control.application_runtime(binding['desktop_runtime_id'])
            if not ref or not all(self.units.get(p.unit_name) and self.units[p.unit_name].active for p in ref.programs):
                raise WorkspaceError('desktop-not-ready', 'desktop not ready')
        return {'ready':True}


def setup(tmp_path, *, multiple=True):
    project = Project(tmp_path / 'project')
    for name, role in [('desktop', 'desktop'), ('terminal', 'client')]:
        path = project.application_dir / name / 'application.py'
        path.parent.mkdir(parents=True)
        path.write_text(f'application(name={name!r}, workspace_role={role!r}, multiple_instances={True if name == "desktop" else multiple!r}, programs=(program(name="main", command=("/usr/bin/test",)),))')
    boot = ['boot-1']; transport = Transport(); transport.boot = boot
    control = f.control_for(project, tmp_path, transport, boot); transport.control = control
    workspace = str(uuid4()); control.register_workspace(workspace, 1)
    return project, control, transport, workspace, boot


def desktop(control, workspace):
    return control.launch_application('desktop', workspace_id=workspace)


def test_shared_binding_and_durable_request_intent(tmp_path):
    project, control, transport, workspace, _ = setup(tmp_path)
    server = desktop(control, workspace)
    request = control.issue_application_request_id()
    first = control.launch_application('terminal', workspace_id=workspace, request_id=request)
    assert first.workspace_binding['desktop_runtime_id'] == server.runtime_id
    assert dict(first.programs[0].expected_properties)['NetworkNamespacePath'] == '/proc/1/ns/net'
    repeated = control.launch_application('terminal', request_id=request, parameters={'workspace-number':1})
    assert repeated.runtime_id == first.runtime_id
    assert len(transport.started) == 2
    other = str(uuid4()); control.register_workspace(other, 2)
    with pytest.raises(WorkspaceError, match='different'):
        control.launch_application('terminal', request_id=request, workspace_id=other)
    with pytest.raises(WorkspaceError, match='different'):
        control.launch_application('terminal', request_id=request)
    assert len(transport.started) == 2
    assert control.workspace_desktop_access(workspace)['desktop_runtime_id'] == server.runtime_id
    control.terminate_application_runtime(first.runtime_id)
    assert transport.units[server.programs[0].unit_name].active


def test_queued_membership_cancel_and_teardown(tmp_path):
    project, control, transport, workspace, _ = setup(tmp_path)
    queued = control.request_application_launch('terminal', workspace_id=workspace)
    assert control.workspace_membership(workspace)['items'][0]['kind'] == 'request'
    with pytest.raises(WorkspaceError, match='protect'):
        control.delete_workspace(workspace)
    with pytest.raises(WorkspaceError, match='protect'):
        control.change_workspace_network(workspace, 'host-shared')
    assert control.cancel_application_launch('terminal', request_id=queued.request_id, parameters={'workspace-number':1})['status'] == 'cancelled'
    assert control.workspace_membership(workspace)['total'] == 0
    assert control.delete_workspace(workspace)['state'] == 'deleted'
    with pytest.raises(WorkspaceError, match='never reused'):
        control.register_workspace(str(uuid4()), 1)
    assert not transport.started


def test_singleton_cannot_replace_other_workspace(tmp_path):
    project, control, transport, first, _ = setup(tmp_path, multiple=False)
    second = str(uuid4()); control.register_workspace(second, 2)
    desktop(control, first); desktop(control, second)
    runtime = control.launch_application('terminal', workspace_id=first)
    with pytest.raises(WorkspaceError, match='another workspace'):
        control.launch_application('terminal', workspace_id=second)
    assert transport.units[runtime.programs[0].unit_name].active


def test_desktop_stop_refuses_attached_clients_and_network_change(tmp_path):
    project, control, transport, workspace, _ = setup(tmp_path)
    server = desktop(control, workspace)
    client = control.launch_application('terminal', workspace_id=workspace)
    with pytest.raises(WorkspaceError, match='attached'):
        control.terminate_application_runtime(server.runtime_id)
    control.terminate_application_runtime(client.runtime_id)
    control.terminate_application_runtime(server.runtime_id)
    assert control.change_workspace_network(workspace, 'host-shared')['revision'] == 2
    with pytest.raises(WorkspaceError, match='isolated'):
        control.change_workspace_network(workspace, 'isolated')


def test_recovery_retains_prepared_binding_and_rejects_reboot_substitution(tmp_path, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime
    project, control, transport, workspace, boot = setup(tmp_path)
    desktop(control, workspace)
    original = SystemdServiceRuntime.launch
    def crash(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', crash)
    request = control.issue_application_request_id()
    with pytest.raises(KeyboardInterrupt):
        control.launch_application('terminal', request_id=request, workspace_id=workspace)
    members = control.workspace_membership(workspace)
    assert any(row['kind'] == 'operation' for row in members['items'])
    with pytest.raises(WorkspaceError):
        control.launch_application('terminal', request_id=request, workspace_id=str(uuid4()))
    assert len(transport.started) == 1
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', original)
    boot[0] = 'boot-2'; transport.units.clear()
    with pytest.raises(RecoveryRequired, match='workspace unavailable'):
        control.launch_application('terminal', request_id=request, workspace_id=workspace)
    assert len(transport.started) == 1
    assert control.workspace_membership(workspace)['total'] >= 2


def test_legacy_arbitrary_values_and_missing_desktop_are_rejected(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    with pytest.raises(WorkspaceError, match='controller-owned'):
        control.launch_application('terminal', parameters={'workspace-number':1, 'DISPLAY':':99'})
    with pytest.raises(WorkspaceError, match='desktop'):
        control.launch_application('terminal', workspace_id=workspace)
    assert not transport.started
    assert control.workspace_capabilities()['networks'][0]['isolation'] == 'none'


def test_concurrent_duplicate_launch_and_lost_reply(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    _, control, transport, workspace, _ = setup(tmp_path)
    desktop(control, workspace)
    request = control.issue_application_request_id()
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(control.launch_application, 'terminal', request_id=request, workspace_id=workspace) for _ in range(2)]
        results = [call.result() for call in calls]
    assert results[0].runtime_id == results[1].runtime_id
    assert len(transport.started) == 2
    # The caller can discard a successful reply and retrieve its exact outcome.
    result = control.application_request_result(request)
    assert result.workspace_id == workspace
    assert result.runtime_id == results[0].runtime_id
    cancelled = control.cancel_application_launch('terminal', request_id=request, workspace_id=workspace)
    assert cancelled['status'] == 'accepted' and cancelled['runtime_id'] == result.runtime_id


def test_launch_serializes_against_deletion(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from zog.box_control.errors import ProjectBusy
    _, control, transport, workspace, _ = setup(tmp_path)
    desktop(control, workspace)
    entered, release = Event(), Event()
    original = transport.start_service
    def paused(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)
    transport.start_service = paused
    control.mutation_lock_timeout_seconds = 0.02
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(control.launch_application, 'terminal', workspace_id=workspace)
        assert entered.wait(5)
        try:
            with pytest.raises(ProjectBusy):
                control.delete_workspace(workspace)
            with pytest.raises(ProjectBusy):
                control.change_workspace_network(workspace, 'host-shared')
        finally:
            release.set()
        future.result()
    with pytest.raises(WorkspaceError, match='protect'):
        control.delete_workspace(workspace)


def test_queued_execution_and_restart_retain_attachment(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    desktop(control, workspace)
    request = control.request_application_launch('terminal', parameters={'workspace-number':1})
    assert control.evaluate().ok
    result = control.application_request_result(request.request_id)
    assert result.workspace_id == workspace
    first = control.application_runtime(result.runtime_id)
    restarted = control.restart_application_runtime(first.runtime_id)
    assert restarted.workspace_binding == first.workspace_binding
    assert restarted.runtime_id != first.runtime_id


def test_deletion_precedes_launch_and_failed_cleanup_holds_membership(tmp_path):
    from zog.box_control.model import ApplicationRuntimeState
    from zog.box_control.runtime.reference import RuntimeReferenceStore
    _, control, transport, workspace, _ = setup(tmp_path)
    control.delete_workspace(workspace)
    with pytest.raises(WorkspaceError, match='deleted'):
        desktop(control, workspace)
    other = str(uuid4()); control.register_workspace(other, 2)
    ref = desktop(control, other)
    store = RuntimeReferenceStore(control.project.runtime_reference_file)
    refs = store.load(); refs[ref.runtime_id] = replace(ref, state=ApplicationRuntimeState.FAILED, cleanup_pending=True)
    store.save(refs)
    assert control.workspace_membership(other)['items'][0]['cleanup_pending'] is True
    with pytest.raises(WorkspaceError, match='protect'):
        control.change_workspace_network(other, 'host-shared')


def test_read_only_status_has_bounded_pages_and_no_transport_mutation(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    desktop(control, workspace)
    control.launch_application('terminal', workspace_id=workspace)
    before = {p: p.read_bytes() for p in control.project.state_dir.rglob('*.json')}
    page = control.workspace_membership(workspace, limit=1)
    assert len(page['items']) == 1 and page['next']
    assert len(control.workspace_membership(workspace, after=page['next'], limit=1)['items']) == 1
    control.workspace_status(workspace); control.workspace_application_catalogue(limit=1)
    assert before == {p: p.read_bytes() for p in control.project.state_dir.rglob('*.json')}


def test_workspace_preflight_is_advisory_and_requires_desktop(tmp_path):
    _, control, transport, workspace, _ = setup(tmp_path)
    result = control.preflight_application_launch('terminal', workspace_id=workspace)
    assert result['status'] == 'blocked'
    assert any(check['code'] == 'desktop-not-ready' for check in result['checks'])
    desktop(control, workspace)
    result = control.preflight_application_launch('terminal', parameters={'workspace-number':1})
    assert any(check['code'] == 'workspace-binding' and check['status'] == 'ready' for check in result['checks'])
    assert len(transport.started) == 1


def test_membership_capacity_failure_is_explicit_and_read_only(tmp_path, monkeypatch):
    import zog.box_control.workspaces as module
    _, control, transport, workspace, _ = setup(tmp_path)
    desktop(control, workspace)
    monkeypatch.setattr(module, 'MAXIMUM_BYTES', 1)
    with pytest.raises(WorkspaceError) as raised:
        control.workspace_membership(workspace)
    assert raised.value.code == 'inspection-capacity'
    assert len(transport.started) == 1


def test_pre_workspace_operation_schema_recovers_with_default_binding(tmp_path, monkeypatch):
    import json
    from zog.box_control.runtime.systemd import SystemdServiceRuntime
    project = Project(tmp_path / 'project'); f.write_application(project)
    transport = f.FakeTransport(); control = f.control_for(project,tmp_path,transport,['boot'])
    original = SystemdServiceRuntime.launch
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', lambda *args, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
    request = control.issue_application_request_id()
    with pytest.raises(KeyboardInterrupt):
        control.launch_application('desktop', request_id=request)
    for path in (project.state_dir / 'operation').glob('*.json'):
        raw = json.loads(path.read_text())
        for field in ('references','previous_references'):
            for ref in raw[field]['runtimes'].values(): ref.pop('workspace_binding',None)
        raw['application'].pop('workspace_role',None); raw['application'].pop('workspace_binding',None)
        path.write_text(json.dumps(raw))
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', original)
    assert control.launch_application('desktop',request_id=request).workspace_binding is None
    assert len(transport.started) == 1
