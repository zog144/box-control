"""Durable lifecycle operations. Callers hold the project lock.

The operation record is authoritative while publication/cleanup is unfinished.
Runtime references and queued request results are repairable projections.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, replace
from pathlib import Path

from .events import emit, recorded, operation_context
from .durability import replace_json, remove_file, _blocked_projects
from .errors import RecoveryRequired, PersistenceError, RuntimeOperationError
from .model import ApplicationSpec, ProgramSpec, ApplicationStartPolicy, ApplicationRuntimeState
from .requests import (ApplicationRequest, _validate_request, ApplicationRequestOperation, ApplicationRequestResult,
                       ApplicationRequestStatus, ApplicationRequestStore)
from .runtime.reference import RuntimeReferenceStore
from .runtime.systemd import _launch_acceptance_error, _expected_transient

TERMINAL = {ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED}


def application_from_record(raw):
    programs = []
    for item in raw['programs']:
        item = dict(item)
        item['command'] = tuple(item['command'])
        item['environment'] = tuple(tuple(pair) for pair in item['environment'])
        item['mounts'] = tuple(tuple(pair) for pair in item['mounts'])
        programs.append(ProgramSpec(**item))
    preparation = []
    for item in raw.get('preparation', ()):
        item = dict(item)
        for key in ('command', 'environment', 'mounts'):
            item[key] = tuple(tuple(pair) if isinstance(pair, list) else pair for pair in item[key])
        preparation.append(ProgramSpec(**item))
    return ApplicationSpec(**dict(raw, programs=tuple(programs), preparation=tuple(preparation),
        writable_mounts=tuple(raw.get('writable_mounts', ())),
        dependencies=tuple(raw['dependencies']), start_policy=ApplicationStartPolicy(raw['start_policy'])))



class OperationStore:
    def __init__(self, project):
        self.directory = project.state_dir / 'operation'

    def path(self, identity):
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            raise RecoveryRequired('invalid operation identity')
        return self.directory / f'{identity}.json'

    def save(self, record):
        replace_json(self.path(record['operation_id']), record)
        recorded(record)

    def load(self, path):
        try:
            record = json.loads(path.read_text())
            if record['schema'] != 1 or self.path(record['operation_id']) != path:
                raise ValueError('invalid operation schema/identity')
            if record['phase'] not in {'prepared', 'stopping', 'launching', 'committed', 'failed', 'abandoned'}:
                raise ValueError('invalid operation phase')
            references = RuntimeReferenceStore.decode(record['references'])
            previous = RuntimeReferenceStore.decode(record['previous_references'])
            if (set(record['runtime_ids']) != set(references)
                or not set(previous) <= set(references)
                or not set(record['stop_runtime_ids']) <= set(references)
                or (record['new_runtime_id'] is not None and record['new_runtime_id'] not in references)):
                raise ValueError('invalid operation runtime identities')
            if record['operation'] not in {'launch', 'restart', 'terminate'}:
                raise ValueError('invalid operation kind')
            if record['phase'] not in {'committed', 'failed', 'abandoned'} and (record['finished'] or record['published']):
                raise ValueError('unfinished operation has terminal flags')
            if record['new_runtime_id'] is not None:
                application = application_from_record(record['application'])
                if application.name != references[record['new_runtime_id']].application:
                    raise ValueError('application snapshot disagrees with runtime identity')
                if record.get('software_binding') != application.software_binding:
                    raise ValueError('prepared application software binding disagrees with application snapshot')
                if references[record['new_runtime_id']].execution_binding != application.execution_binding:
                    raise ValueError('prepared execution identity disagrees with runtime reference')
            if not set(record['attempted_units']) <= {p.unit_name for ref in references.values() for p in ref.programs}:
                raise ValueError('start intent references an unknown unit')
            if not isinstance(record['published'], bool) or not isinstance(record['finished'], bool):
                raise ValueError('invalid completion flags')
            return record
        except (OSError, ValueError, KeyError, TypeError, RuntimeOperationError) as exc:
            raise RecoveryRequired(f'cannot read operation {path}: {exc}') from exc

    def records(self):
        records = [self.load(path) for path in sorted(self.directory.glob('*.json'))]
        request_ids = [record['request_id'] for record in records if record['request_id'] is not None]
        if len(request_ids) != len(set(request_ids)):
            raise RecoveryRequired('multiple operation records claim the same request')
        return records


class JournalReferences:
    def __init__(self, manager, record):
        self.manager, self.record = manager, record

    def save(self, references):
        record = self.record
        subset = {identity: references[identity] for identity in record['runtime_ids'] if identity in references}
        record['previous_references'] = record['references']
        record['references'] = RuntimeReferenceStore.encode(subset)
        self.manager.store.save(record)
        self.manager.references.save(references)


class IntentTransport:
    def __init__(self, manager, record, transport):
        self.manager, self.record, self.transport = manager, record, transport

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def _event(self, event, unit):
        for runtime_id, reference in self.record['references']['runtimes'].items():
            for program in reference['programs']:
                if program['unit_name'] == unit:
                    emit(event, runtime_id=runtime_id, program=program['program'], unit_name=unit,
                         invocation_id=program.get('invocation_id'))
                    return
        emit(event, unit_name=unit)

    def _action(self, name, **kwargs):
        unit=kwargs['unit_name']
        self._event('unit.'+name+'.requested', unit)
        result=getattr(self.transport,name)(**kwargs)
        self._event('unit.'+name+'.acknowledged', unit)
        return result

    def stop(self, **kwargs): return self._action('stop', **kwargs)
    def kill(self, **kwargs): return self._action('kill', **kwargs)
    def release(self, **kwargs): return self._action('release', **kwargs)
    def reset_failed(self, **kwargs): return self._action('reset_failed', **kwargs)

    def start_slice(self, **kwargs):
        unit = kwargs['slice_name']
        if self.record['slice_attempted']:
            observation = self.transport.observe(project_root=kwargs['project_root'], unit_name=unit)
            if observation.exists:
                if not _expected_transient(observation) or not observation.active:
                    raise RecoveryRequired(f'{unit}: slice does not match prepared operation')
                return
            # Slices have no executable side effects; recreation is safe.
        self.record['slice_attempted'] = True
        self.manager.store.save(self.record)
        self.transport.start_slice(**kwargs)

    def start_service(self, **kwargs):
        unit = kwargs['definition'].unit_name
        if unit in self.record['attempted_units']:
            raise RecoveryRequired(f'{unit}: refusing to repeat a recorded start')
        self.record['attempted_units'].append(unit)
        self.manager.store.save(self.record)
        self._event('service.start.requested', unit)
        try:
            result = self.transport.start_service(**kwargs)
            self._event('service.start.acknowledged', unit)
            return result
        except PersistenceError:
            raise
        except Exception as exc:
            # A lost method reply is not proof of failure, and cannot authorize
            # ordinary rollback. Recovery observes the unit on a later call.
            raise RecoveryRequired(f'{unit}: start outcome requires recovery: {exc}') from exc


class OperationManager:
    def __init__(self, project, runtime, *, boot_id_provider):
        self.project, self.runtime = project, runtime
        self.boot_id_provider = boot_id_provider
        self.store = OperationStore(project)
        self.references = RuntimeReferenceStore(project.runtime_reference_file)
        self.requests = ApplicationRequestStore(project.application_request_dir, project.application_request_result_dir)
        self.marker = project.state_dir / 'mutation-incomplete.json'

    def _point(self, record, *, preparing=False):
        replace_json(self.marker, {'schema': 2, 'operation_id': record['operation_id'], 'preparing': preparing})

    def _unpoint(self):
        replace_json(self.marker, {'schema': 1, 'status': 'mutation-incomplete'})

    def for_request(self, request_id, operation, *, application=None, target_runtime_id=None):
        if request_id is None:
            return None
        for record in self.store.records():
            if record['request_id'] != request_id:
                continue
            recorded_application = record['application']['name'] if record['application'] else None
            if (record['operation'] != operation or record['target_runtime_id'] != target_runtime_id
                or (operation == 'launch' and recorded_application != application)):
                raise RuntimeOperationError('request identity already belongs to different intent')
            if not record['finished'] or not record['published']:
                raise RecoveryRequired('request operation must recover before reuse')
            if record['phase'] != 'committed':
                raise RuntimeOperationError(record['error'] or 'request did not succeed')
            saved = RuntimeReferenceStore.decode(record['references'])
            return saved[record['new_runtime_id'] or record['target_runtime_id']]
        return None

    def prepare(self, *, application, selection, references, instance_id,
                request_id, replaces_runtime_id, boot_id, fingerprint,
                stop_runtime_ids=(), operation='launch', target_runtime_id=None):
        if request_id is not None:
            _validate_request(ApplicationRequest(request_id=request_id,
                operation=ApplicationRequestOperation(operation),
                application=application.name if operation == 'launch' else None,
                runtime_id=target_runtime_id))
        pending = None
        if application is not None:
            pending = self.runtime.prepare(application, instance_id=instance_id,
                generation=selection.generation, generation_root=selection.root,
                references=references, store=self.references,
                application_fingerprint=fingerprint, request_id=request_id,
                replaces_runtime_id=replaces_runtime_id, boot_id=boot_id)
        if pending and application.workspace_binding and application.workspace_binding['role'] == 'desktop':
            binding = dict(application.workspace_binding, desktop_runtime_id=pending.runtime_id)
            application = replace(application, workspace_binding=binding)
            pending = replace(pending, workspace_binding=binding)
        identities = list(dict.fromkeys(stop_runtime_ids))
        if pending is not None:
            identities.append(pending.runtime_id)
        before = {identity: references[identity] for identity in identities if identity in references}
        after = dict(before)
        if pending is not None:
            after[pending.runtime_id] = pending
        record = dict(schema=1, operation_id=secrets.token_hex(16), operation=operation,
            request_id=request_id, target_runtime_id=target_runtime_id,
            application=asdict(application) if application is not None else None,
            software_binding=application.software_binding if application is not None else None,
            generation_root=str(selection.root.resolve()) if selection is not None else None,
            boot_id=boot_id, prepared_boot_id=boot_id, runtime_ids=identities, stop_runtime_ids=list(stop_runtime_ids),
            new_runtime_id=pending.runtime_id if pending else None,
            phase='prepared', references=RuntimeReferenceStore.encode(after),
            previous_references=RuntimeReferenceStore.encode(before), attempted_units=[],
            slice_attempted=False, finished=False, published=False, error=None,
            created_at=time.time(), completed_at=None)
        # The preparation pointer precedes the record. If the record never
        # appears, no lifecycle action could yet have been authorized.
        self._point(record, preparing=True)
        self.store.save(record)
        self._point(record)
        references.update(after)
        self.references.save(references)
        return record

    def _repair(self, record, references):
        current = RuntimeReferenceStore.encode(references)['runtimes']
        desired = RuntimeReferenceStore.encode(RuntimeReferenceStore.decode(record['references']))['runtimes']
        previous = RuntimeReferenceStore.encode(RuntimeReferenceStore.decode(record['previous_references']))['runtimes']
        # Compare JSON-normalized values because disk arrays represent tuples.
        for identity in record['runtime_ids']:
            value = current.get(identity)
            if json.dumps(value, sort_keys=True) not in {
                json.dumps(desired.get(identity), sort_keys=True),
                json.dumps(previous.get(identity), sort_keys=True),
            }:
                raise RecoveryRequired(f'{identity}: runtime state contradicts operation record')
        references.update(RuntimeReferenceStore.decode(record['references']))
        self.references.save(references)

    def _publish(self, record, references):
        identity = record['new_runtime_id'] or record['target_runtime_id']
        reference = references.get(identity)
        if record['request_id']:
            status = {'committed': ApplicationRequestStatus.SATISFIED,
                      'failed': ApplicationRequestStatus.FAILED,
                      'abandoned': ApplicationRequestStatus.ABANDONED}[record['phase']]
            result = ApplicationRequestResult(request_id=record['request_id'],
                operation=ApplicationRequestOperation(record['operation']), status=status,
                application=reference.application if reference else None,
                target_runtime_id=record['target_runtime_id'], runtime_id=identity,
                replaced_runtime_id=reference.replaces_runtime_id if reference else None,
                workspace_id=((record.get('application') or {}).get('workspace_binding') or {}).get('workspace_id'),
                error=record['error'], updated_at=record['completed_at'])
            existing = self.requests.result(record['request_id'])
            if existing is not None and existing.status != ApplicationRequestStatus.PENDING:
                if any(getattr(existing, key) != getattr(result, key) for key in
                       ('operation', 'status', 'application', 'target_runtime_id', 'runtime_id', 'replaced_runtime_id', 'workspace_id', 'error')):
                    raise RecoveryRequired('request result contradicts authoritative operation')
            self.requests.save_result(result)
            path = self.project.application_request_dir / f"{record['request_id']}.json"
            if path.exists():
                self.requests.remove(path)
        record['published'] = True
        self.store.save(record)
        return reference

    def _finish(self, record, references, phase, error=None):
        record.update(phase=phase, error=error, completed_at=record['completed_at'] or time.time())
        JournalReferences(self, record).save(references)
        record['finished'] = all(
            references[identity].state in TERMINAL and not references[identity].cleanup_pending
            for identity in record['stop_runtime_ids'])
        if record['new_runtime_id'] and (phase != 'committed' or references[record['new_runtime_id']].state in TERMINAL):
            new = references[record['new_runtime_id']]
            record['finished'] = record['finished'] and new.state in TERMINAL and not new.cleanup_pending
        self.store.save(record)
        return self._publish(record, references)

    @operation_context
    def execute(self, record, references, *, recovering=False):
        self._point(record)
        self._repair(record, references)
        journal = JournalReferences(self, record)
        new_id = record['new_runtime_id']
        if not record['finished']:
            self.runtime.preflight()
        original = self.runtime.transport
        self.runtime.transport = IntentTransport(self, record, original)
        try:
            if record['phase'] in {'committed', 'failed', 'abandoned'}:
                if not record['finished']:
                    cleanup_ids = list(record['stop_runtime_ids'])
                    if new_id and (record['phase'] != 'committed' or references[new_id].state in TERMINAL):
                        cleanup_ids.append(new_id)
                    for identity in cleanup_ids:
                        reference = references[identity]
                        if reference.state not in TERMINAL or reference.cleanup_pending:
                            self.runtime.terminate(reference, references=references, store=journal)
                    self._finish(record, references, record['phase'], record['error'])
                else:
                    self._publish(record, references)
                return references.get(new_id or record['target_runtime_id'])

            if new_id:
                # Reproduce mount bindings before stopping replacement targets.
                pending = references[new_id]
                if pending.state == ApplicationRuntimeState.STARTING:
                    application = application_from_record(record['application'])
                    try:
                        definition = self.runtime.definition(application, new_id,
                            generation_root=Path(record['generation_root']))
                        for service, program in zip(definition.services, pending.programs, strict=True):
                            if program.mount_inventory is not None and service.mount_inventory() != program.mount_inventory:
                                raise RecoveryRequired('prepared mount inventory changed')
                            if application.persistent:
                                for source, target in service.bind_paths:
                                    if not source.is_dir() or source.resolve() != source.absolute():
                                        raise RecoveryRequired('prepared application data is missing or redirected')
                    except RuntimeOperationError as exc:
                        raise RecoveryRequired(f'prepared mount policy cannot be reproduced: {exc}') from exc
                binding = (record.get('application') or {}).get('workspace_binding')
                if binding:
                    from .workspaces import Workspaces
                    Workspaces(self.project).verify_bound(binding, original)
                saved = references[new_id]
                failure = next((program for program in saved.programs
                    if program.result not in (None, '', 'success')), None)
                if saved.state == ApplicationRuntimeState.STARTING and failure is not None:
                    saved = replace(saved, state=ApplicationRuntimeState.TERMINATING,
                        error=f'{failure.unit_name}: Result={failure.result}')
                    references[new_id] = saved
                    journal.save(references)
                if saved.error or saved.state in {ApplicationRuntimeState.FAILED, ApplicationRuntimeState.TERMINATING}:
                    self.runtime.terminate(saved, references=references, store=journal)
                    return self._finish(record, references, 'failed', saved.error or 'launch failed')
                if not Path(record['generation_root']).is_dir():
                    raise RecoveryRequired('prepared rootfs is unavailable; recovery cannot substitute it')
                # A preparation with no service-start intent can safely begin
                # after reboot. An attempted, uncommitted launch cannot.
                if (record['attempted_units'] and references[new_id].state == ApplicationRuntimeState.STARTING
                    and record['boot_id'] != self.boot_id_provider()):
                    raise RecoveryRequired('reboot interrupted an uncommitted launch; outcome unknown')

            record['phase'] = 'stopping'
            self.store.save(record)
            for identity in record['stop_runtime_ids']:
                reference = references[identity]
                if reference.state not in TERMINAL or reference.cleanup_pending:
                    stopped = self.runtime.terminate(reference, references=references, store=journal)
                    if stopped.state not in TERMINAL or stopped.cleanup_pending:
                        raise RecoveryRequired(f'{identity}: operation is waiting for cleanup')
            if not new_id:
                return self._finish(record, references, 'committed')

            pending = references[new_id]
            if pending.error or pending.state in {ApplicationRuntimeState.FAILED, ApplicationRuntimeState.TERMINATING}:
                self.runtime.terminate(pending, references=references, store=journal)
                return self._finish(record, references, 'failed', pending.error or 'launch failed')
            if pending.state in {ApplicationRuntimeState.RUNNING, ApplicationRuntimeState.TERMINATED}:
                # A launch acceptance snapshot is already durable in the journal.
                return self._finish(record, references, 'committed')

            resumed = set()
            for program in pending.programs:
                if program.unit_name not in record['attempted_units']:
                    continue
                observation = self.runtime._observe_program(program)
                if not observation.exists or not observation.invocation_id:
                    raise RecoveryRequired(f'{program.unit_name}: prior start outcome is unknown')
                if observation.result not in (None, '', 'success'):
                    evidence = replace(program, invocation_id=program.invocation_id or observation.invocation_id,
                        result=observation.result, active_state=observation.active_state,
                        sub_state=observation.sub_state)
                    failed = replace(pending, error=f'{program.unit_name}: Result={observation.result}',
                        programs=tuple(evidence if item.unit_name == program.unit_name else item for item in pending.programs),
                        state=ApplicationRuntimeState.TERMINATING)
                    references[new_id] = failed
                    journal.save(references)
                    self.runtime.terminate(failed, references=references, store=journal)
                    return self._finish(record, references, 'failed', failed.error)
                if _launch_acceptance_error(observation):
                    raise RecoveryRequired(f'{program.unit_name}: launch evidence is incomplete')
                resumed.add(program.unit_name)
            # No service-start intent exists, so this prepared identity can
            # first execute on the current boot. Record that boot durably before
            # any start; otherwise once-per-boot policy can launch it again.
            launch_boot = self.boot_id_provider()
            if not record['attempted_units'] and pending.boot_id != launch_boot:
                record.setdefault('prepared_boot_id', record['boot_id'])
                record['boot_id'] = launch_boot
                pending = replace(pending, boot_id=launch_boot)
                references[new_id] = pending
                journal.save(references)
            record['phase'] = 'launching'
            self.store.save(record)
            application = application_from_record(record['application'])
            try:
                launched = self.runtime.launch(application, instance_id=pending.instance_id,
                    generation=pending.generation, generation_root=Path(record['generation_root']),
                    references=references, store=journal, prepared=pending,
                    resumed_units=frozenset(resumed))
            except PersistenceError:
                raise
            except RuntimeOperationError as exc:
                failed = references[new_id]
                if not failed.error:
                    raise RecoveryRequired(f'launch outcome unresolved: {exc}') from exc
                self._finish(record, references, 'failed', failed.error)
                raise
            return self._finish(record, references, 'committed')
        finally:
            self.runtime.transport = original

    def run(self, **kwargs):
        references = kwargs['references']
        record = self.prepare(**kwargs)
        try:
            result = self.execute(record, references)
        except RuntimeOperationError as exc:
            if not record['finished']:
                raise RecoveryRequired(f'failed operation cleanup remains pending: {exc}') from exc
            self._unpoint()
            raise
        if not record['finished']:
            raise RecoveryRequired('operation cleanup remains pending')
        self._unpoint()
        if record['phase'] == 'failed':
            raise RuntimeOperationError(record['error'])
        return result

    def recover(self):
        if (self.project.state_dir / 'session-recovery-pending.json').exists():
            raise RecoveryRequired('interrupted session recovery audit must be finalized explicitly')
        records = self.store.records()
        pointed = None
        if self.marker.exists():
            try:
                marker = json.loads(self.marker.read_text())
                pointed = marker.get('operation_id') if marker.get('schema') == 2 else None
            except (OSError, ValueError) as exc:
                raise RecoveryRequired('unreadable mutation marker') from exc
            if (pointed is not None and marker.get('preparing') is True
                and not any(record['operation_id'] == pointed for record in records)):
                if any(not record['finished'] or not record['published'] for record in records):
                    raise RecoveryRequired('incomplete preparation conflicts with another operation')
                self.store.path(pointed)  # Validate before accepting the pointer.
                remove_file(self.marker)
                _blocked_projects.discard(self.project.path)
                return
            if pointed is None or not any(record['operation_id'] == pointed for record in records):
                raise RecoveryRequired('recovery required: interrupted session has no authoritative operation; inspection required')
        elif self.project.path in _blocked_projects:
            raise RecoveryRequired('storage fault outside a recorded operation; inspection required')
        pending = [record for record in records if not record['finished'] or not record['published'] or record['operation_id'] == pointed]
        if len(pending) > 1:
            raise RecoveryRequired('multiple unfinished operations require inspection')
        for record in pending:
            references = self.references.load()
            self.execute(record, references, recovering=True)
            if not record['finished']:
                raise RecoveryRequired('operation cleanup remains pending')
        if pending:
            remove_file(self.marker)
            _blocked_projects.discard(self.project.path)

    def recover_interrupted_session(self):
        """Explicit, evidence-checked repair of an orphaned schema-1 session.

        Caller holds ProjectLock. This is not called from automatic
        OperationManager.recover() and makes no systemd calls or new launches.
        A failed storage fsync or any inconsistent evidence keeps the guard.
        """
        if self.project.path in _blocked_projects:
            raise RecoveryRequired('storage failure interlock requires separate owner review')
        pending_path = self.project.state_dir / 'session-recovery-pending.json'
        if pending_path.exists() or pending_path.is_symlink():
            try:
                if pending_path.is_symlink():
                    raise ValueError('symlinked recovery pointer')
                pending = json.loads(pending_path.read_text())
                if (not isinstance(pending, dict) or set(pending) != {
                        'schema', 'recovery_id', 'marker_sha256'}
                        or pending['schema'] != 1
                        or not isinstance(pending['recovery_id'], str)
                        or len(pending['recovery_id']) != 32
                        or any(c not in '0123456789abcdef' for c in pending['recovery_id'])
                        or not isinstance(pending['marker_sha256'], str)):
                    raise ValueError('invalid recovery pointer')
                audit_path = (self.project.state_dir / 'session-recovery' /
                              (pending['recovery_id'] + '.json'))
                if audit_path.is_symlink() or audit_path.parent.is_symlink():
                    raise ValueError('redirected recovery audit')
                audit = json.loads(audit_path.read_text())
                if (audit.get('kind') != 'reviewed-interrupted-session-recovery'
                        or audit.get('marker_sha256') != pending['marker_sha256']):
                    raise ValueError('recovery audit and pointer disagree')
                if self.marker.exists():
                    marker = json.loads(self.marker.read_text())
                    if (marker != {'schema': 1, 'status': 'mutation-incomplete'}
                            or hashlib.sha256(self.marker.read_bytes()).hexdigest()
                            != pending['marker_sha256']):
                        raise ValueError('recovery marker changed after audit')
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise RecoveryRequired('pending session recovery audit requires investigation') from exc
            try:
                remove_file(self.marker)
                remove_file(pending_path)
            except PersistenceError:
                _blocked_projects.add(self.project.path)
                raise
            return dict(audit, recovery_id=pending['recovery_id'], resumed=True)
        try:
            if self.marker.is_symlink():
                raise ValueError('symlinked session marker')
            content = self.marker.read_bytes()
            marker = json.loads(content)
        except (OSError, ValueError) as exc:
            raise RecoveryRequired('interrupted session marker is unreadable') from exc
        if marker != {'schema': 1, 'status': 'mutation-incomplete'}:
            raise RecoveryRequired('interrupted session is not a standalone schema-1 guard')

        for name in ('retention-incomplete.json', 'build-pruning.json'):
            if (self.project.state_dir / name).exists():
                raise RecoveryRequired('another persistence/reclamation session is unfinished')
        from .application_storage import ApplicationStorage
        ApplicationStorage(self.project).admission()
        # A process scheduled to start or cancel is not conclusively finished
        # merely because its containing evaluation was terminated.
        job_dir = self.project.state_dir / 'build-job'
        for path in sorted(job_dir.glob('*.json')):
            try:
                raw = json.loads(path.read_text())
                if raw['schema'] != 1 or raw['state'] in {
                        'starting', 'unknown', 'cancelling'}:
                    raise ValueError('unresolved build job')
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RecoveryRequired('build job evidence is unavailable or unfinished') from exc

        records = self.store.records()
        if not records:
            raise RecoveryRequired('standalone session has no completed lifecycle evidence')
        refs = self.references.load()
        from .state import StateStore
        snapshot = StateStore(self.project.state_file).load()
        if not isinstance(snapshot.get('application_runtimes', {}), dict):
            raise RecoveryRequired('saved evaluation state is invalid')
        for record in records:
            if (not record['finished'] or not record['published']
                    or record['phase'] not in {'committed', 'failed', 'abandoned'}):
                raise RecoveryRequired('authoritative lifecycle operation is unfinished')
            saved = RuntimeReferenceStore.decode(record['references'])
            for runtime_id, prior in saved.items():
                current = refs.get(runtime_id)
                if current is None:
                    # Old completed history can be pruned independently from
                    # retained operation evidence. No active/cleanup candidate
                    # can safely be absent from the references.
                    if prior.state not in TERMINAL or prior.cleanup_pending:
                        raise RecoveryRequired('operation references an unavailable live runtime')
                    continue
                if (current.application != prior.application
                        or current.instance_id != prior.instance_id
                        or current.generation != prior.generation
                        or current.boot_id != prior.boot_id
                        or current.request_id != prior.request_id
                        or current.execution_binding != prior.execution_binding
                        or current.software_view != prior.software_view
                        or current.software_binding_digest != prior.software_binding_digest
                        or [(p.program, p.unit_name) for p in current.programs] !=
                           [(p.program, p.unit_name) for p in prior.programs]):
                    raise RecoveryRequired('runtime identity contradicts operation journal')
            if record['request_id']:
                result = self.requests.result(record['request_id'])
                expected_status = {
                    'committed': ApplicationRequestStatus.SATISFIED,
                    'failed': ApplicationRequestStatus.FAILED,
                    'abandoned': ApplicationRequestStatus.ABANDONED,
                }[record['phase']]
                if (result is None or result.operation.value != record['operation']
                        or result.status != expected_status
                        or result.runtime_id != (record['new_runtime_id'] or record['target_runtime_id'])):
                    raise RecoveryRequired('request result contradicts finished operation')

        authoritative_runtime_ids = {
            record['new_runtime_id'] for record in records
            if record['operation'] == 'launch'
            and record['phase'] == 'committed'
            and record['new_runtime_id'] is not None
        }
        current_boot_id = self.boot_id_provider()
        active_instances = set()
        for runtime_id, ref in refs.items():
            if ref.state in {ApplicationRuntimeState.STARTING,
                             ApplicationRuntimeState.TERMINATING}:
                raise RecoveryRequired('runtime transition has no recoverable operation')
            if ref.state in TERMINAL:
                if ref.cleanup_pending:
                    raise RecoveryRequired('terminal runtime still requires cleanup')
                continue
            if ref.state != ApplicationRuntimeState.RUNNING or ref.error:
                raise RecoveryRequired('runtime state is uncertain')
            if runtime_id not in authoritative_runtime_ids:
                raise RecoveryRequired('active runtime lacks a committed launch authority')
            if ref.boot_id != current_boot_id:
                raise RecoveryRequired('active runtime belongs to a previous boot')
            if not ref.programs or any(
                    not program.unit_name or not program.invocation_id
                    for program in ref.programs):
                raise RecoveryRequired('active runtime identity is incomplete')
            if ref.instance_id in active_instances:
                raise RecoveryRequired('multiple active runtimes claim one instance')
            active_instances.add(ref.instance_id)
            old = snapshot.get('application_runtimes', {}).get(runtime_id)
            if old is not None and (
                    old.get('application') != ref.application
                    or old.get('generation') != ref.generation):
                raise RecoveryRequired('saved evaluation state contradicts runtime identity')

        # Read all terminal outcomes, including records whose queue files
        # were removed before the interrupted evaluator finally timed out.
        for path in sorted(self.project.application_request_result_dir.glob('*.json')):
            outcome = self.requests.result(path.stem)
            if outcome is None or outcome.status == ApplicationRequestStatus.PENDING:
                raise RecoveryRequired('saved request result is missing or unfinished')
            if outcome.runtime_id is not None:
                related = refs.get(outcome.runtime_id)
                if related is None and not any(
                        record['new_runtime_id'] == outcome.runtime_id
                        or record['target_runtime_id'] == outcome.runtime_id
                        for record in records):
                    raise RecoveryRequired('saved request result has no runtime evidence')
                if related is not None and outcome.application not in (None, related.application):
                    raise RecoveryRequired('saved request result targets another application')

        queue = []
        for path in self.requests.paths():
            request = self.requests.load(path)
            outcome = self.requests.result(request.request_id)
            if (outcome is not None and
                    (outcome.operation != request.operation
                     or outcome.workspace_id != request.workspace_id)):
                raise RecoveryRequired('queued request disagrees with its saved outcome')
            if outcome is not None and outcome.status == ApplicationRequestStatus.PENDING:
                raise RecoveryRequired('queued request has unresolved prior attempt')
            queue.append(request.request_id)

        # One durable audit precedes the guard removal. Never rewrite or
        # silently unlink other evidence to manufacture a clean project.
        audit = {
            'schema': 1, 'kind': 'reviewed-interrupted-session-recovery',
            'marker_sha256': hashlib.sha256(content).hexdigest(),
            'observed_at': time.time(), 'boot_id': self.boot_id_provider(),
            'operation_ids': sorted(r['operation_id'] for r in records),
            'runtime_ids': sorted(refs),
            'queued_request_ids': sorted(queue),
            'safety': 'persisted-record-consistency-only',
            'kernel_readiness': 'not-established',
        }
        audit_dir = self.project.state_dir / 'session-recovery'
        identity = secrets.token_hex(16)
        from .durability import ensure_directory
        try:
            ensure_directory(audit_dir)
            replace_json(audit_dir / (identity + '.json'), audit)
            replace_json(pending_path, {
                'schema': 1, 'recovery_id': identity,
                'marker_sha256': audit['marker_sha256'],
            })
            remove_file(self.marker)
            remove_file(pending_path)
        except PersistenceError:
            # Even an unlink whose directory fsync failed must retain a
            # cross-process journal, not silently reopen mutation admission.
            _blocked_projects.add(self.project.path)
            raise
        return dict(audit, recovery_id=identity, resumed=False)

    def abandon(self, identity):
        record = self.store.load(self.store.path(identity))
        if record['phase'] in {'committed', 'failed'}:
            raise RuntimeOperationError('a known operation outcome cannot be abandoned')
        if record['phase'] == 'abandoned' and record['finished'] and record['published']:
            return record
        if self.marker.exists():
            try:
                marker = json.loads(self.marker.read_text())
            except (OSError, ValueError) as exc:
                raise RecoveryRequired('unreadable mutation marker') from exc
            if marker.get('operation_id') != identity:
                raise RecoveryRequired('abandonment cannot clear an unrelated mutation block')
        references = self.references.load()
        self._point(record)
        self._repair(record, references)
        if record['phase'] != 'abandoned':
            record.update(phase='abandoned', error='abandoned; original outcome unknown', completed_at=time.time())
            self.store.save(record)
            if record['new_runtime_id']:
                runtime_id = record['new_runtime_id']
                references[runtime_id] = replace(references[runtime_id],
                    state=ApplicationRuntimeState.TERMINATING, error=record['error'])
                JournalReferences(self, record).save(references)
        self.execute(record, references, recovering=True)
        if record['finished']:
            remove_file(self.marker)
            _blocked_projects.discard(self.project.path)
        return record
