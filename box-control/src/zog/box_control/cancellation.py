"""Withdraw queued work without running lifecycle recovery or guessing outcomes."""
import json
import re

from .durability import mutation_guard, replace_json, remove_file, _blocked_projects
from .errors import RuntimeOperationError, PersistenceError, RecoveryRequired
from .locking import ProjectLock
from .operations import OperationStore
from .requests import (ApplicationRequestOperation as Operation, ApplicationRequestStatus as Status,
                       ApplicationRequestResult, ApplicationRequestStore)
from .retention import Retention
from .runtime.reference import RuntimeReferenceStore


def recover_cancellation(project):
    """Finish only an explicitly journalled cancellation. Caller holds ProjectLock."""
    marker = project.state_dir / 'mutation-incomplete.json'
    if not marker.exists():
        return
    intent = json.loads(marker.read_text())
    if intent.get('schema') != 5:
        return
    request_id = intent.get('request_id')
    application = intent.get('application')
    if not isinstance(request_id, str) or not re.fullmatch('[A-Za-z0-9-]{1,200}', request_id):
        raise RecoveryRequired('invalid cancellation intent')
    if not isinstance(application, str) or not re.fullmatch('[A-Za-z0-9-]+', application):
        raise RecoveryRequired('invalid cancellation application')
    if any(r['request_id'] == request_id for r in OperationStore(project).records()):
        raise RecoveryRequired('cancellation conflicts with lifecycle evidence')
    if any(r.request_id == request_id for r in RuntimeReferenceStore(project.runtime_reference_file).load().values()):
        raise RecoveryRequired('cancellation conflicts with runtime evidence')
    store = ApplicationRequestStore(project.application_request_dir, project.application_request_result_dir)
    path = project.application_request_dir / (request_id + '.json')
    queued = store.load(path) if path.exists() else None
    result = store.result(request_id)
    for item in (queued, result):
        if item and (item.operation != Operation.LAUNCH or item.application != application or item.workspace_id != intent.get('workspace_id')):
            raise RecoveryRequired('cancellation intent disagrees with request')
    if result and (result.status not in {Status.PENDING, Status.CANCELLED} or result.runtime_id or result.replaced_runtime_id):
        raise RecoveryRequired('cancellation result conflicts with execution evidence')
    if result is None or result.status != Status.CANCELLED:
        store.save_result(ApplicationRequestResult(request_id, Operation.LAUNCH, Status.CANCELLED, application=application, workspace_id=intent.get('workspace_id')))
    store.remove(path)
    remove_file(marker)
    _blocked_projects.discard(project.path)


def cancel_launch(control, application, request_id, *, workspace_id=None, parameters=None):
    if not isinstance(request_id, str) or not re.fullmatch('[A-Za-z0-9-]{1,200}', request_id):
        raise RuntimeOperationError('invalid launch request identity')
    project = control.project
    project.require_state_allowed()
    with ProjectLock(project.lock_file, timeout_seconds=control.mutation_lock_timeout_seconds):
        from .workspaces import Workspaces
        if workspace_id is not None or parameters is not None:
            selected = Workspaces(project).selected(workspace_id, parameters)
            Workspaces(project).validate_request(request_id, application, selected)
        recover_cancellation(project)
        store = control._request_store()
        result = store.result(request_id)
        path = project.application_request_dir / (request_id + '.json')
        queued = store.load(path) if path.exists() else None
        operations = [r for r in OperationStore(project).records() if r['request_id'] == request_id]
        if len(operations) > 1:
            raise RuntimeOperationError('conflicting operation evidence')
        for item in (queued, result):
            if item and (item.operation != Operation.LAUNCH or item.application != application):
                raise RuntimeOperationError('request belongs to another application or operation')
        record = operations[0] if operations else None
        if record and (record['operation'] != 'launch' or not record['application'] or record['application']['name'] != application):
            raise RuntimeOperationError('request belongs to another application or operation')
        if not (queued or result or record):
            raise RuntimeOperationError('missing or expired launch evidence')
        try:
            Retention(project).validate(request_id)
        except PersistenceError:
            _blocked_projects.add(project.path)
            raise
        response = dict(request_id=request_id, application=application, runtime_id=None,
                        operation_id=record['operation_id'] if record else None)
        if record:
            if record['phase'] == 'committed':
                return dict(response, status='accepted', runtime_id=record['new_runtime_id'])
            if record['phase'] in ('failed', 'abandoned'):
                return dict(response, status=record['phase'], runtime_id=record['new_runtime_id'])
            return dict(response, status='uncertain' if record['attempted_units'] or record['slice_attempted'] else 'pending')
        if result and result.status != Status.PENDING:
            return dict(response, status='accepted' if result.status == Status.SATISFIED else result.status.value,
                        runtime_id=result.runtime_id)
        if result and (result.runtime_id or result.replaced_runtime_id):
            return dict(response, status='uncertain')
        references = RuntimeReferenceStore(project.runtime_reference_file).load()
        if any(ref.request_id == request_id for ref in references.values()):
            return dict(response, status='uncertain')
        if queued is None:
            raise RuntimeOperationError('incomplete launch evidence')
        with mutation_guard(project):
            replace_json(project.state_dir / 'mutation-incomplete.json',
                         dict(schema=5, request_id=request_id, application=application, workspace_id=queued.workspace_id))
            recover_cancellation(project)
        return dict(response, status='cancelled')
