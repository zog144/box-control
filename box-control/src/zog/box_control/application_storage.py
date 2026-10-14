"""Application-owned storage and explicit, non-replayable preparation attempts.

All mutations hold the project lock. A preparation intent is durable before any
service starts. Refresh observes that identity; it never repeats an attempted
command. A failed migration is not a rollback.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
import secrets
import stat
import time

from .durability import replace_json, remove_file, _blocked_projects
from .errors import BoxControlError, RecoveryRequired, RuntimeOperationError, PersistenceError
from .locking import ProjectLock
from .model import ApplicationRuntimeState
from .operations import application_from_record, OperationStore
from .runtime.reference import RuntimeReferenceStore
from .specification import discover_applications


class ApplicationStorage:
    def __init__(self, project):
        self.project = project
        self.directory = project.state_dir / 'application-storage'
        self.marker = project.state_dir / 'mutation-incomplete.json'

    def path(self, application):
        if not isinstance(application, str) or not re.fullmatch('[A-Za-z0-9-]+', application):
            raise RuntimeOperationError('invalid application identity')
        return self.directory / (application + '.json')

    def load(self, application):
        path = self.path(application)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            if (raw['schema'] != 1 or raw['application'] != application
                or not re.fullmatch('[a-f0-9]{32}', raw['storage_id'])
                or raw['state'] not in {'preparing', 'ready', 'failed', 'uncertain', 'deleting', 'deleted'}):
                raise ValueError('invalid storage record')
            return raw
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryRequired(f'cannot read application storage: {exc}') from exc

    def save(self, raw):
        replace_json(self.path(raw['application']), raw)

    def records(self):
        return [self.load(p.stem) for p in sorted(self.directory.glob('*.json'))]

    def admission(self):
        for raw in self.records():
            if raw['state'] in {'preparing', 'uncertain', 'deleting'} or raw.get('cleanup_pending'):
                raise RecoveryRequired(f"preparation for {raw['application']} requires refresh or explicit abandonment")

    @staticmethod
    def bind_execution(application, binding):
        """Resolve symbolic app identity to stable numeric execution properties.

        The returned application snapshot contains the exact privileged grant,
        including its ID/version and numeric IDs. Legacy applications are unchanged.
        """
        if application.execution_identity is None:
            if binding is not None:
                raise RecoveryRequired('legacy application cannot acquire an execution identity binding')
            return application
        if application.execution_identity != "application" or not application.persistent:
            raise RecoveryRequired('unsupported execution identity contract')
        if (not isinstance(binding, dict)
                or binding.get("schema") != "zog-execution-identity-v1"
                or binding.get("project") is None
                or binding.get("application") != application.name
                or type(binding.get("uid")) is not int
                or type(binding.get("gid")) is not int
                or not 1000 <= binding["uid"] < 2**31 - 1
                or not 1000 <= binding["gid"] < 2**31 - 1):
            raise RecoveryRequired("invalid controller-issued execution identity binding")
        numeric_user, numeric_group = str(binding["uid"]), str(binding["gid"])
        # Only symbolic declarations (with parser-enforced absence of explicit
        # user/group) are transformed. Callers cannot supply arbitrary numbers.
        return replace(
            application, execution_binding=dict(binding),
            programs=tuple(replace(p, user=numeric_user, group=numeric_group)
                           for p in application.programs),
            preparation=tuple(replace(p, user=numeric_user, group=numeric_group)
                              for p in application.preparation),
        )

    @staticmethod
    def signature(application):
        # Normal service command changes do not invalidate a prepared environment.
        payload = dict(mounts=application.writable_mounts,
                       owners=sorted({(p.user, p.group or '') for p in application.programs}),
                       preparation=[asdict(p) for p in application.preparation])
        if application.execution_identity is not None:
            payload["execution_identity"] = application.execution_identity
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def bind(self, application, generation):
        raw = self.load(application.name)
        if not application.persistent:
            if raw and raw['state'] != 'deleted':
                raise RuntimeOperationError('persistent storage exists; explicitly delete it before changing storage policy')
            return application
        if (raw is None or raw['state'] != 'ready' or raw.get('cleanup_pending')
            or raw['revision'] != application.preparation_revision
            or raw['signature'] != self.signature(application)
            or raw['generation'] != generation):
            raise RuntimeOperationError('application environment requires explicit preparation or upgrade')
        base = self.project.mounts_dir / 'persistent' / raw['storage_id']
        for name in raw['mounts']:
            path = base / name
            if not path.is_dir() or path.is_symlink() or path.resolve() != path.absolute():
                raise RuntimeOperationError('prepared writable input is missing or redirected; restore it before launch')
            if application.execution_identity == 'application':
                observed = path.lstat()
                grant = raw['definition'].get('execution_binding')
                if (not isinstance(grant, dict)
                        or (observed.st_uid, observed.st_gid) != (grant.get('uid'), grant.get('gid'))
                        or stat.S_IMODE(observed.st_mode) != 0o700):
                    raise RuntimeOperationError(
                        'prepared controller-managed storage ownership or mode has drifted; restore approved state'
                    )
        binding = raw['definition'].get('execution_binding')
        if application.execution_identity is not None:
            if (binding is None or binding.get("project") != str(self.project.path)
                    or binding.get("application") != application.name):
                raise RecoveryRequired("prepared execution identity is missing or scoped to another project")
        return self.bind_execution(
            replace(application, storage_id=raw['storage_id']), binding
        )

    @contextmanager
    def session(self, control, application, *, resume=False):
        self.project.require_state_allowed()
        with ProjectLock(self.project.lock_file, timeout_seconds=control.mutation_lock_timeout_seconds):
            if self.load(application) is None:
                raise RuntimeOperationError('no preparation record')
            if self.marker.exists():
                marker = json.loads(self.marker.read_text())
                if not (resume and marker.get('schema') == 4 and marker.get('application') == application):
                    raise RecoveryRequired('another interrupted mutation requires recovery')
            elif self.project.path in _blocked_projects:
                raise RecoveryRequired('storage barrier failed; reopen controller after inspecting storage')
            replace_json(self.marker, dict(schema=4, application=application))
            try:
                yield
                remove_file(self.marker)
                _blocked_projects.discard(self.project.path)
            except RuntimeOperationError:
                remove_file(self.marker)
                _blocked_projects.discard(self.project.path)
                raise
            except BaseException:
                _blocked_projects.add(self.project.path)
                raise

    def begin(self, control, application_name, *, upgrade=False, retry=False):
        # Use normal admission and recovery before making a new preparation intent.
        with control._mutation():
            applications = discover_applications(self.project.application_dir)
            if application_name not in applications:
                raise RuntimeOperationError('unknown application')
            application = applications[application_name]
            if not application.persistent:
                raise RuntimeOperationError('application does not declare persistent storage')
            raw = self.load(application_name)
            selection = control.image_provider.ensure(self.project)
            from .application_software import resolve_binding
            application = resolve_binding(self.project, application, selection)
            from .mounts import validate
            for program in application.programs + application.preparation:
                validate(selection.root, program.mounts, program.command, require_targets=True)
            signature = self.signature(application)
            if raw and raw['state'] != 'deleted':
                if raw['state'] == 'ready' and raw['revision'] == application.preparation_revision and raw['signature'] == signature and raw['generation'] == selection.generation:
                    self.bind(application, selection.generation)
                    return raw
                if raw['signature'] != signature and raw['revision'] == application.preparation_revision:
                    raise RuntimeOperationError('changed preparation requires a new preparation_revision')
                if not upgrade:
                    raise RuntimeOperationError('existing environment requires explicit upgrade=True')
                if raw['state'] == 'failed' and not retry:
                    raise RuntimeOperationError('failed preparation requires explicit retry=True after inspecting migrations')
                if raw['mounts'] != list(application.writable_mounts):
                    raise RuntimeOperationError('changing persistent mount names is not supported in this pass')
            references = RuntimeReferenceStore(self.project.runtime_reference_file).load()
            if RuntimeReferenceStore.launch_candidates(references, application=application_name):
                raise RuntimeOperationError('stop the application and complete its cleanup before preparation')
            if any(not r['finished'] or not r['published'] for r in OperationStore(self.project).records()):
                raise RuntimeOperationError('unfinished lifecycle operations prevent preparation')
            storage_id = raw['storage_id'] if raw and raw['state'] != 'deleted' else secrets.token_hex(16)
            previous = list(raw.get('attempts', [])) if raw else []
            if raw and raw.get('attempt_id'):
                previous.append({k: raw.get(k) for k in ('attempt_id', 'revision', 'generation', 'state', 'steps', 'error')})
            binding = None
            if application.execution_identity is not None:
                # No prepared state or runtime mutation precedes grant resolution.
                # The privileged mechanism never provisions via this RPC.
                binding = control.systemd_transport.resolve_execution_identity(
                    project_root=self.project.path, application=application.name
                )
                if not isinstance(binding, dict) or binding.get('project') != str(self.project.path):
                    raise RecoveryRequired('root-control returned a mismatched execution identity')
                if raw and raw['state'] != 'deleted':
                    previous_binding = raw['definition'].get('execution_binding')
                    if previous_binding != binding:
                        raise RecoveryRequired('prepared execution identity changed; explicit storage migration required')
            elif raw and raw['state'] != 'deleted' and raw['definition'].get('execution_binding'):
                raise RecoveryRequired('existing persistent storage has a controller-managed identity; cannot downgrade')
            bound = self.bind_execution(replace(application, storage_id=storage_id), binding)
            raw = dict(schema=1, application=application_name, storage_id=storage_id,
                       state='preparing', revision=application.preparation_revision,
                       signature=signature, generation=selection.generation,
                       root=str(selection.root), definition=asdict(bound),
                       mounts=list(application.writable_mounts), attempt_id=secrets.token_hex(16),
                       attempts=previous, steps=[], cleanup_pending=False, error=None)
            replace_json(self.marker, dict(schema=4, application=application_name))
            self.save(raw)
        # Separate acquisition permits a crash here: the preparing record blocks launches.
        return self.refresh(control, application_name)

    @staticmethod
    def _durable_preparation_completion(raw, step, ref, current_boot):
        """Return a proven successful terminal invocation, or None.

        systemd may unload a fast-exiting transient unit before refresh.
        A saved *completed* runtime reference can bridge that gap, but only
        when it identifies this precise preparation/step and has durable
        terminal success evidence from a known boot and invocation. This
        never treats a missing unit or an accepted start as completion.
        """
        if (ref.purpose != 'preparation'
                or ref.runtime_id != step['runtime_id']
                or ref.application != raw['application']
                or ref.instance_id != 'prepare-' + raw['storage_id']
                or ref.generation != raw['generation']
                or ref.state != ApplicationRuntimeState.TERMINATED
                or ref.error is not None
                or ref.completed_at is None
                or ref.cleanup_pending
                or len(ref.programs) != 1
                or not ref.boot_id):
            return None
        program = ref.programs[0]
        if (program.program != step['program']
                or not program.unit_name
                or not program.invocation_id
                or program.result != 'success'
                or program.active_state != 'inactive'
                or program.sub_state != 'dead'):
            return None
        if step.get('unit_name') is not None and step['unit_name'] != program.unit_name:
            return None
        if step.get('attempt_id') is not None and step['attempt_id'] != raw['attempt_id']:
            return None
        if step.get('invocation_id') is not None and step['invocation_id'] != program.invocation_id:
            return None
        # New steps durably bind a boot before starting; their terminal
        # evidence remains authoritative after reboot. Old steps lack that
        # additional independent binding: accept only on the same boot.
        if step.get('boot_id') is not None:
            if step['boot_id'] != ref.boot_id:
                return None
        elif ref.boot_id != current_boot:
            return None
        return program.invocation_id

    def refresh(self, control, application_name, *, abandon=False):
        with self.session(control, application_name, resume=True):
            raw = self.load(application_name)
            if raw is None:
                raise RecoveryRequired('no preparation record; inspect interrupted admission')
            if raw['state'] == 'deleting':
                raise RuntimeOperationError('repeat delete_application_storage to finish deletion')
            if raw['state'] in {'ready', 'deleted'}:
                return raw
            application = application_from_record(raw['definition'])
            runtime = control._reconciler().systemd_runtime
            store = RuntimeReferenceStore(self.project.runtime_reference_file)
            references = store.load()
            step = raw['steps'][-1] if raw['steps'] else None
            if step and not step['cleanup_complete']:
                ref = references.get(step['runtime_id'])
                if ref is None:
                    raw.update(state='uncertain', error='preparation runtime evidence is missing')
                    self.save(raw)
                    return raw
                if step['outcome'] is None and not abandon:
                    try:
                        observation = runtime._observe_program(ref.programs[0])
                        if not observation.exists:
                            invocation = self._durable_preparation_completion(
                                raw, step, ref, control.boot_id_provider()
                            )
                            if invocation is None:
                                raw.update(state='uncertain', error='preparation completion is unknown; do not replay migration')
                                self.save(raw)
                                return raw
                            step['outcome'] = 'success'
                            step['invocation_id'] = invocation
                            step['completion_source'] = 'durable-terminal-runtime'
                        else:
                            if ref.boot_id != control.boot_id_provider():
                                raw.update(state='uncertain', error='preparation completion is unknown; do not replay migration')
                                self.save(raw)
                                return raw
                            if observation.active:
                                return raw
                            if not observation.invocation_id or not observation.result:
                                raw.update(state='uncertain', error='preparation completion evidence is incomplete')
                                self.save(raw)
                                return raw
                            step['outcome'] = observation.result
                            step['invocation_id'] = observation.invocation_id
                            step['completion_source'] = 'observed-unit'
                    except PersistenceError:
                        raise
                    except Exception as exc:
                        raw.update(state='uncertain', error=str(exc))
                        self.save(raw)
                        return raw
                    # Save completion evidence before reset/unload can erase it.
                    self.save(raw)
                if abandon:
                    step['outcome'] = step['outcome'] or 'abandoned-unknown'
                    raw.update(state='failed', error='explicitly abandoned; writable changes were not undone')
                    self.save(raw)
                # A fast-exit launch may already have durably completed
                # teardown. Never reopen or reconstruct that absent unit.
                if ref.cleanup_pending:
                    ref = runtime.terminate(ref, references=references, store=store)
                step['cleanup_complete'] = not ref.cleanup_pending
                raw['cleanup_pending'] = ref.cleanup_pending
                self.save(raw)
                if ref.cleanup_pending:
                    return raw
                if step['outcome'] != 'success':
                    raw.update(state='failed', error=raw['error'] or 'preparation command failed: ' + str(step['outcome']))
                    self.save(raw)
                    return raw
            if abandon:
                raw.update(state='failed', cleanup_pending=False, error='explicitly abandoned; writable changes were not undone')
                self.save(raw)
                return raw
            if raw['state'] == 'failed':
                return raw
            index = len(raw['steps'])
            if index == len(application.preparation):
                # Every command was witnessed complete and its cleanup drained.
                # Flush the prepared tree before acknowledging its durable readiness.
                control.systemd_transport.sync_application_storage(
                    project_root=self.project.path, application=application_name,
                    storage_id=raw['storage_id'])
                raw.update(state='ready', cleanup_pending=False, error=None, prepared_at=time.time())
                self.save(raw)
                return raw
            program = application.preparation[index]
            prepared_app = replace(application, programs=(program,))
            ref = runtime.prepare(prepared_app, instance_id='prepare-' + raw['storage_id'],
                                  generation=raw['generation'], generation_root=Path(raw['root']),
                                  references=references, store=store, boot_id=control.boot_id_provider())
            ref = replace(ref, purpose='preparation')
            references[ref.runtime_id] = ref
            store.save(references)
            # From this durable point, an absent unit means uncertainty, never permission to retry.
            step = dict(program=program.name, runtime_id=ref.runtime_id, outcome=None,
                        cleanup_complete=False, invocation_id=None,
                        boot_id=ref.boot_id, unit_name=ref.programs[0].unit_name,
                        attempt_id=raw['attempt_id'])
            raw['steps'].append(step)
            raw.update(state='preparing', cleanup_pending=True)
            self.save(raw)
            try:
                runtime.launch(prepared_app, instance_id=ref.instance_id, generation=raw['generation'],
                               generation_root=Path(raw['root']), references=references, store=store,
                               boot_id=ref.boot_id, prepared=ref)
            except PersistenceError:
                raise
            except Exception as exc:
                saved = store.load().get(ref.runtime_id)
                if saved:
                    step['runtime_error'] = saved.error
                    step['program_results'] = [p.result for p in saved.programs]
                raw.update(state='uncertain', error=str(exc))
                self.save(raw)
            return raw

    def delete(self, control, application_name, *, storage_id):
        """Explicit deletion, resumable after partial removal. Caller supplies inspected identity."""
        with self.session(control, application_name, resume=True):
            raw = self.load(application_name)
            if storage_id != raw['storage_id']:
                raise RuntimeOperationError('storage identity mismatch')
            if raw['state'] == 'deleted':
                return raw
            if raw['state'] in {'preparing', 'uncertain'} or raw['cleanup_pending']:
                raise RecoveryRequired('finish or abandon preparation and its cleanup before deletion')
            refs = RuntimeReferenceStore(self.project.runtime_reference_file).load()
            if RuntimeReferenceStore.launch_candidates(refs, application=application_name):
                raise RecoveryRequired('stop application and complete cleanup before deleting storage')
            if any(not op['finished'] or not op['published'] for op in OperationStore(self.project).records()):
                raise RecoveryRequired('unfinished lifecycle operation protects storage')
            base = self.project.mounts_dir / 'persistent' / storage_id
            if base.resolve() != base.absolute() or base.is_symlink():
                raise RecoveryRequired('storage directory is redirected')
            raw['state'] = 'deleting'
            self.save(raw)
            control.systemd_transport.delete_application_storage(
                project_root=self.project.path, application=application_name, storage_id=storage_id)
            raw.update(state='deleted', deleted_at=time.time())
            self.save(raw)
            return raw
