"""Workspace contract v1. Callers hold ProjectLock for admission/mutation.

Membership is derived from authoritative queued requests, operation journals and
runtime references, never from display strings. Bounded inspection fails closed
when the complete ownership set cannot be inspected.
"""
from dataclasses import replace
import json
from itertools import islice, chain
from pathlib import Path
import secrets
import time
from uuid import UUID

from .durability import replace_json
from .errors import RuntimeOperationError, RecoveryRequired
from .model import ApplicationRuntimeState
from .runtime.reference import RuntimeReferenceStore
from .operations import OperationStore
from .requests import ApplicationRequestStore, ApplicationRequestStatus

VERSION = 'zog-workspace-v1'
TERMINAL = {ApplicationRuntimeState.FAILED, ApplicationRuntimeState.TERMINATED}
MAXIMUM_RECORDS = 512
MAXIMUM_BYTES = 8 * 1024 * 1024


class WorkspaceError(RuntimeOperationError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

    def response(self):
        return dict(schema=VERSION, ok=False, reason=dict(code=self.code, message=str(self)))


def identity(value):
    try:
        result = str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise WorkspaceError('invalid-workspace', 'workspace must be a canonical UUID') from None
    if result != value:
        raise WorkspaceError('invalid-workspace', 'workspace must be a canonical UUID')
    return result


class Workspaces:
    def __init__(self, project):
        self.project = project
        self.directory = project.state_dir / 'workspace'

    def path(self, workspace_id):
        return self.directory / (identity(workspace_id) + '.json')

    def load(self, workspace_id):
        path = self.path(workspace_id)
        if not path.exists():
            raise WorkspaceError('unknown-workspace', 'workspace is not registered')
        try:
            if path.stat().st_size > MAXIMUM_BYTES:
                raise WorkspaceError('inspection-capacity', 'workspace record exceeds supported byte bound')
            raw = json.loads(path.read_text())
            if (raw['schema'] != VERSION or raw['workspace_id'] != workspace_id
                or type(raw['number']) is not int or not 1 <= raw['number'] <= 9999
                or raw['state'] not in {'registered', 'deleted'}):
                raise ValueError('invalid workspace record')
            return raw
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise WorkspaceError('workspace-record-invalid', str(exc)) from exc

    def save(self, raw):
        replace_json(self.path(raw['workspace_id']), raw)

    def all(self):
        paths = list(islice(self.directory.glob('*.json'), MAXIMUM_RECORDS + 1))
        if len(paths) > MAXIMUM_RECORDS:
            raise WorkspaceError('inspection-capacity', 'workspace catalogue exceeds supported bound')
        return [self.load(path.stem) for path in sorted(paths)]

    def selected(self, workspace_id=None, parameters=None):
        parameters = {} if parameters is None else parameters
        if not isinstance(parameters, dict) or set(parameters) - {'workspace-number'}:
            raise WorkspaceError('unsupported-parameters', 'only workspace-number is accepted; display/network/credentials are controller-owned')
        if parameters:
            number = parameters['workspace-number']
            if type(number) is not int or not 1 <= number <= 9999:
                raise WorkspaceError('invalid-workspace-number', 'workspace-number must be an integer from 1 to 9999')
            matches = [r for r in self.all() if r['number'] == number]
            if len(matches) != 1:
                raise WorkspaceError('unknown-workspace', 'workspace-number has no unique registration')
            if workspace_id is not None and matches[0]['workspace_id'] != identity(workspace_id):
                raise WorkspaceError('workspace-mismatch', 'workspace UUID and number disagree')
            workspace_id = matches[0]['workspace_id']
        return identity(workspace_id) if workspace_id is not None else None

    def register(self, workspace_id, number, *, network='host-shared'):
        workspace_id = identity(workspace_id)
        if type(number) is not int or not 1 <= number <= 9999:
            raise WorkspaceError('invalid-workspace-number', 'workspace number must be 1 through 9999')
        if network != 'host-shared':
            raise WorkspaceError('unsupported-network', 'contract v1 supports explicit shared host networking only')
        records = self.all()
        for record in records:
            if record['workspace_id'] == workspace_id:
                if record['number'] != number or record['network'] != network or record['state'] == 'deleted':
                    raise WorkspaceError('workspace-conflict', 'workspace identity already belongs to different or deleted intent')
                return record
            if record['number'] == number:
                raise WorkspaceError('workspace-number-used', 'workspace numbers are never reused, including after deletion')
        if len(records) >= MAXIMUM_RECORDS:
            raise WorkspaceError('inspection-capacity', 'workspace catalogue is full')
        record = dict(schema=VERSION, workspace_id=workspace_id, number=number,
                      network=network, revision=1, incarnation=secrets.token_hex(16),
                      state='registered', created_at=time.time())
        self.save(record)
        return record

    def evidence(self):
        reference_path = self.project.runtime_reference_file
        paths = list(islice(chain((self.project.state_dir / 'operation').glob('*.json'), self.project.application_request_dir.glob('*.json')), MAXIMUM_RECORDS + 1))
        if len(paths) > MAXIMUM_RECORDS or sum(p.stat().st_size for p in paths + ([reference_path] if reference_path.exists() else [])) > MAXIMUM_BYTES:
            raise WorkspaceError('inspection-capacity', 'ownership inspection exceeds supported bound; no teardown or admission permitted')
        references = RuntimeReferenceStore(reference_path).load()
        operations = OperationStore(self.project).records()
        requests = ApplicationRequestStore(self.project.application_request_dir, self.project.application_request_result_dir)
        queued = [requests.load(p) for p in requests.paths()]
        return references, operations, queued

    def members(self, workspace_id, *, after=None, limit=50):
        identity(workspace_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError('invalid-limit', 'limit must be between 1 and 100')
        references, operations, queued = self.evidence()
        rows = []
        for ref in references.values():
            binding = ref.workspace_binding or {}
            if binding.get('workspace_id') == workspace_id and (ref.state not in TERMINAL or ref.cleanup_pending):
                rows.append(dict(key='runtime:' + ref.runtime_id, kind='runtime', runtime_id=ref.runtime_id,
                                 application=ref.application, state=ref.state.value, cleanup_pending=ref.cleanup_pending,
                                 role=binding['role'], desktop_runtime_id=binding.get('desktop_runtime_id')))
        for record in operations:
            binding = (record.get('application') or {}).get('workspace_binding') or {}
            related = [ref for ref in RuntimeReferenceStore.decode(record['references']).values()
                       if (ref.workspace_binding or {}).get('workspace_id') == workspace_id]
            if (binding.get('workspace_id') == workspace_id or related) and (not record['finished'] or not record['published']):
                rows.append(dict(key='operation:' + record['operation_id'], kind='operation', operation_id=record['operation_id'],
                                 request_id=record['request_id'], state=record['phase'], runtime_id=record['new_runtime_id']))
        for request in queued:
            if request.workspace_id == workspace_id:
                rows.append(dict(key='request:' + request.request_id, kind='request', request_id=request.request_id,
                                 application=request.application, state='queued'))
        rows.sort(key=lambda row: row['key'])
        filtered = [row for row in rows if after is None or row['key'] > after]
        page = filtered[:limit]
        return dict(schema=VERSION, workspace_id=workspace_id, complete=True, items=page,
                    next=page[-1]['key'] if len(filtered) > limit else None, total=len(rows))

    def change(self, workspace_id, *, network=None, delete=False):
        raw = self.load(workspace_id)
        if raw['state'] == 'deleted':
            if delete:
                return raw
            raise WorkspaceError('workspace-deleted', 'workspace was deleted')
        if self.members(workspace_id)['total']:
            raise WorkspaceError('workspace-in-use', 'attached runtimes, queued requests or unresolved operations protect workspace resources')
        if network is not None and network != 'host-shared':
            raise WorkspaceError('unsupported-network', 'isolated networks are not implemented')
        raw.update(state='deleted' if delete else 'registered', revision=raw['revision'] + 1,
                   incarnation=secrets.token_hex(16))
        self.save(raw)
        # Old socket/authority epochs are retained, never rebound or reused.
        return raw

    def validate_request(self, request_id, application, workspace_id):
        if request_id is None:
            return
        store = ApplicationRequestStore(self.project.application_request_dir, self.project.application_request_result_dir)
        path = self.project.application_request_dir / (request_id + '.json')
        items = [store.result(request_id)]
        if path.exists():
            items.append(store.load(path))
        for item in items:
            if item and (item.operation.value != 'launch' or item.application != application or item.workspace_id != workspace_id):
                raise WorkspaceError('request-intent-conflict', 'request identity belongs to different intent (application/workspace)')
        for record in OperationStore(self.project).records():
            if record['request_id'] == request_id:
                declared = record.get('application') or {}
                prior = (declared.get('workspace_binding') or {}).get('workspace_id')
                if record['operation'] != 'launch' or declared.get('name') != application or prior != workspace_id:
                    raise WorkspaceError('request-intent-conflict', 'request identity belongs to different intent (application/workspace)')

    def resolve(self, application, workspace_id, transport, *, boot_id):
        if not application.workspace_role:
            if workspace_id is not None:
                raise WorkspaceError('application-ineligible', 'application does not opt into workspace networking/display')
            return application
        if workspace_id is None:
            raise WorkspaceError('workspace-required', 'workspace-enabled application needs a registered workspace')
        raw = self.load(workspace_id)
        if raw['state'] != 'registered':
            raise WorkspaceError('workspace-deleted', 'workspace is deleted')
        refs, operations, queued = self.evidence()
        desktops = [r for r in refs.values() if (r.workspace_binding or {}).get('workspace_id') == workspace_id
                    and (r.workspace_binding or {}).get('role') == 'desktop'
                    and (r.state not in TERMINAL or r.cleanup_pending)]
        if application.workspace_role == 'desktop':
            # Explicit replacement of a desktop requires an entirely idle seat.
            if desktops or any((r.workspace_binding or {}).get('workspace_id') == workspace_id
                               and (r.state not in TERMINAL or r.cleanup_pending) for r in refs.values()):
                raise WorkspaceError('workspace-in-use', 'stop attached applications and desktop before a new desktop incarnation')
            # Allocate a fresh authority/socket epoch before preparing the desktop operation.
            raw.update(incarnation=secrets.token_hex(16))
            self.save(raw)
            binding = dict(schema=VERSION, workspace_id=workspace_id, number=raw['number'], network=raw['network'],
                           revision=raw['revision'], incarnation=raw['incarnation'], role='desktop',
                           desktop_runtime_id=None, boot_id=boot_id)
            binding.update(transport.workspace_call('prepare', project_root=self.project.path, binding=binding,
                                                    user=application.programs[0].user, group=application.programs[0].group))
        else:
            if len(desktops) != 1 or desktops[0].state != ApplicationRuntimeState.RUNNING or desktops[0].boot_id != boot_id:
                raise WorkspaceError('desktop-not-ready', 'workspace requires one live desktop incarnation')
            desktop = desktops[0]
            expected = dict(desktop.programs[0].expected_properties)
            if any(p.user != expected['User'] or p.group != expected.get('Group') for p in application.programs):
                raise WorkspaceError('workspace-user-mismatch', 'workspace clients must use the desktop host account and group')
            binding = dict(desktop.workspace_binding, role='client', desktop_runtime_id=desktop.runtime_id)
            for member in desktop.programs:
                observed = transport.observe(project_root=self.project.path, unit_name=member.unit_name)
                if not observed.active or observed.invocation_id != member.invocation_id:
                    raise WorkspaceError('desktop-not-ready', 'desktop invocation is unavailable or changed')
            transport.workspace_call('verify', project_root=self.project.path, binding=binding)
        return self.attach(application, binding)

    def attach(self, application, binding):
        programs = []
        for program in application.programs:
            replacements = {'{workspace-display}': binding['display'], '{workspace-xauthority}': '/run/zog-workspace/Xauthority',
                            '{workspace-vnc-socket}': '/run/zog-workspace/vnc.sock'}
            command = tuple(replacements.get(part, part) for part in program.command)
            environment = dict(program.environment, DISPLAY=binding['display'], XAUTHORITY='/run/zog-workspace/Xauthority')
            programs.append(replace(program, command=command, environment=tuple(sorted(environment.items()))))
        return replace(application, programs=tuple(programs), workspace_binding=binding)

    def verify_bound(self, binding, transport):
        raw = self.load(binding['workspace_id'])
        if raw['state'] != 'registered' or any(raw[key] != binding[key] for key in ('revision', 'incarnation', 'network', 'number')):
            raise RecoveryRequired('prepared workspace binding changed; substitution is prohibited')
        try:
            transport.workspace_call('verify', project_root=self.project.path, binding=binding)
        except Exception as exc:
            raise RecoveryRequired(f'prepared workspace unavailable: {exc}') from exc
