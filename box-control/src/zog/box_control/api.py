from __future__ import annotations
from .diagnostics import fault_from_exception
from .inspection import inspect_recovery

import time
from contextlib import contextmanager

from .boot import current_boot_id
from .retention import Retention
from .durability import mutation_guard
from .errors import BoxControlError, RuntimeOperationError
from .model import ReconcileReport
from .images import ImageBuildProvider, ImageProvider
from .locking import ProjectLock
from .project import Project
from .reconcile import Reconciler
from .runtime.reference import RuntimeReferenceStore
from .runtime.root_control import RootControlSystemdTransport
from .runtime.systemd import SystemdServiceRuntime, SystemdTransport
from .requests import (
    ApplicationRequest,
    ApplicationRequestOperation,
    ApplicationRequestStore,
    ApplicationRequestStatus,
)


class BoxControl:
    """Public library facade shared by PROJECT_EVALUATION and station-access."""

    def __init__(
        self,
        project: Project,
        *,
        image_provider: ImageProvider | None = None,
        systemd_transport: SystemdTransport | None = None,
        boot_id_provider=current_boot_id,
        mutation_lock_timeout_seconds=5.0,
    ):
        self.mutation_lock_timeout_seconds = mutation_lock_timeout_seconds
        self.project = project
        self.image_provider = image_provider or ImageBuildProvider()
        self.systemd_transport = systemd_transport or RootControlSystemdTransport()
        self.boot_id_provider = boot_id_provider

    @classmethod
    def discover(
        cls,
        *,
        image_provider: ImageProvider | None = None,
        systemd_transport: SystemdTransport | None = None,
        boot_id_provider=current_boot_id,
        mutation_lock_timeout_seconds=5.0,
    ) -> "BoxControl":
        return cls(
            Project.discover(),
            image_provider=image_provider,
            systemd_transport=systemd_transport,
            boot_id_provider=boot_id_provider,
            mutation_lock_timeout_seconds=mutation_lock_timeout_seconds,
        )

    def _reconciler(self):
        return Reconciler(
            self.project,
            image_provider=self.image_provider,
            systemd_runtime=SystemdServiceRuntime(
                self.project, self.systemd_transport
            ),
            boot_id_provider=self.boot_id_provider,
        )

    @contextmanager
    def _mutation(self, *, build_entity=None, launch_intent=None):
        self.project.require_state_allowed()
        # Lock bootstrap may create state/, but the guard synchronizes its
        # ancestry before allowing any controller or external lifecycle work.
        with ProjectLock(self.project.lock_file, timeout_seconds=self.mutation_lock_timeout_seconds):
            if launch_intent is not None:
                from .workspaces import Workspaces
                application, request, workspace, parameters = launch_intent
                selected = Workspaces(self.project).selected(workspace, parameters)
                Workspaces(self.project).validate_request(request, application, selected)
            from .cancellation import recover_cancellation
            recover_cancellation(self.project)
            from .application_storage import ApplicationStorage
            ApplicationStorage(self.project).admission()
            from .build_jobs import BuildJobs
            BuildJobs(self).admission(build_entity)
            BuildJobs(self).prune()
            retention = Retention(self.project)
            retention.resume()
            self._reconciler().operations.recover()
            retention.prune()
            with mutation_guard(self.project):
                self.project.ensure_state()
                yield

    def evaluate(self):
        """Perform one serialized PROJECT_EVALUATION."""
        try:
            with self._mutation():
                return self._reconciler().reconcile()
        except BoxControlError as exc:
            return ReconcileReport(ok=False, errors=[str(exc)], faults=[fault_from_exception(exc)])
        except Exception as exc:
            # The exception must leave _mutation first so its interlock remains
            # in place before we convert the failure to a caller-facing report.
            return ReconcileReport(ok=False, errors=[f"unexpected error: {exc}"], faults=[fault_from_exception(exc)])

    def observe_application_runtime_credentials(self, runtime_id, *, limit=50):
        """Read kernel credentials of recorded systemd runtime members.

        root-control independently resolves the persisted units and cgroups;
        callers cannot supply a PID, username, desired UID or service name.
        Partial evidence is explicit and is never described as verified.
        """
        if type(limit) is not int or not 1 <= limit <= 64:
            raise RuntimeOperationError("credential process limit must be 1 through 64")
        result = self.systemd_transport.application_runtime_credentials(
            project_root=self.project.path, runtime_id=runtime_id, limit=limit,
        )
        if (not isinstance(result, dict)
                or result.get("schema") != "zog-runtime-credential-observation-v1"
                or result.get("runtime_id") != runtime_id
                or not isinstance(result.get("programs"), list)):
            raise RuntimeOperationError(
                "root-control returned an invalid runtime credential observation"
            )
        return result

    def observe_application_runtime_mounts(self, runtime_id):
        """Observe a recorded runtime's actual kernel mount namespaces read-only.

        The caller supplies only the immutable runtime identity. root-control
        derives every unit, process, PID and /proc path from persisted launch
        evidence and returns a bounded non-atomic observation.
        """
        result = self.systemd_transport.application_runtime_mounts(
            project_root=self.project.path,
            runtime_id=runtime_id,
        )
        if not isinstance(result, dict) or result.get("schema") != "zog-runtime-mount-observation-v1":
            raise RuntimeOperationError("root-control returned an unsupported runtime mount observation")
        if result.get("runtime_id") != runtime_id:
            raise RuntimeOperationError("root-control returned a mismatched runtime mount observation")
        return result

    def application_runtime_mounts(self, runtime_id):
        """Bounded, read-only expected mount inventory; never probes or repairs."""
        from .inspection import _snapshot, RecoveryInspection
        from .runtime.reference import RuntimeReferenceStore
        from .errors import RuntimeOperationError
        from .mounts import VERSION
        from copy import deepcopy
        with _snapshot(self.project, RecoveryInspection()) as available:
            if not available:
                raise RuntimeOperationError('mount inspection busy; retry')
            path = self.project.runtime_reference_file
            if path.exists() and path.stat().st_size > 8 * 1024 * 1024:
                raise RuntimeOperationError('mount inspection exceeds 8 MiB bound')
            reference = RuntimeReferenceStore(path).load().get(runtime_id)
            if reference is None:
                raise RuntimeOperationError('unknown runtime identity')
            inventories = [p.mount_inventory for p in reference.programs if p.mount_inventory]
            writable_destinations = sorted({
                item['destination']
                for inventory in inventories
                for item in inventory.get('mounts', ())
                if item.get('access') == 'read-write'
                and item.get('kind') == 'application-data'
            })
            return dict(
                schema=VERSION,
                runtime_id=runtime_id,
                generation=reference.generation,
                software_view=reference.software_view,
                software_binding_digest=reference.software_binding_digest,
                software_manifest_digest=reference.software_manifest_digest,
                software_artifacts=list(reference.software_artifacts),
                read_only_software_mounts=[list(pair) for pair in reference.software_mounts],
                writable_state_destinations=writable_destinations,
                basis='expected-launched',
                observed_mounts='not-probed',
                programs=[dict(
                    program=p.program,
                    invocation_id=p.invocation_id,
                    inventory=deepcopy(p.mount_inventory),
                    status='recorded' if p.mount_inventory is not None else 'legacy-unrecorded',
                ) for p in reference.programs],
            )

    def workspace_capabilities(self):
        from .workspaces import VERSION
        return dict(schema=VERSION, supported=True, networks=[dict(id='host-shared', isolation='none', sharing='all-workspaces-and-host', owner='host-init')],
                    desktop_transport='unix', parameters=['workspace-number'], arbitrary_parameters=False,
                    live_acceptance=False, cancellation_response='structured-outcome-v1',
                    launch_preconditions=['registered-workspace', 'eligible-application', 'ready-desktop-incarnation', 'compatible-preparation', 'singleton-same-workspace'])

    def register_workspace(self, workspace_id, number, *, network='host-shared'):
        from .workspaces import Workspaces
        with ProjectLock(self.project.lock_file, timeout_seconds=self.mutation_lock_timeout_seconds):
            with mutation_guard(self.project):
                return Workspaces(self.project).register(workspace_id, number, network=network)

    def change_workspace_network(self, workspace_id, network):
        from .workspaces import Workspaces
        with ProjectLock(self.project.lock_file, timeout_seconds=self.mutation_lock_timeout_seconds):
            with mutation_guard(self.project):
                return Workspaces(self.project).change(workspace_id, network=network)

    def delete_workspace(self, workspace_id):
        from .workspaces import Workspaces
        with ProjectLock(self.project.lock_file, timeout_seconds=self.mutation_lock_timeout_seconds):
            with mutation_guard(self.project):
                return Workspaces(self.project).change(workspace_id, delete=True)

    def workspace_membership(self, workspace_id, *, after=None, limit=50):
        from .workspaces import Workspaces, WorkspaceError
        from .inspection import _snapshot, RecoveryInspection
        with _snapshot(self.project, RecoveryInspection()) as available:
            if not available:
                raise WorkspaceError('inspection-busy', 'retry read-only workspace inspection')
            Workspaces(self.project).load(workspace_id)
            return Workspaces(self.project).members(workspace_id, after=after, limit=limit)

    def workspace_status(self, workspace_id):
        from .workspaces import Workspaces, WorkspaceError
        from .inspection import _snapshot, RecoveryInspection
        with _snapshot(self.project, RecoveryInspection()) as available:
            if not available:
                raise WorkspaceError('inspection-busy', 'retry read-only workspace inspection')
            raw = Workspaces(self.project).load(workspace_id)
            return dict(raw, membership=Workspaces(self.project).members(workspace_id), observed_at=time.time(), readiness='not-probed')

    def workspace_desktop_access(self, workspace_id):
        from .workspaces import Workspaces, WorkspaceError, VERSION
        from .inspection import _snapshot, RecoveryInspection
        with _snapshot(self.project, RecoveryInspection()) as available:
            if not available:
                raise WorkspaceError('inspection-busy', 'retry desktop inspection')
            raw = Workspaces(self.project).load(workspace_id)
            refs, _, _ = Workspaces(self.project).evidence()
            candidates = [r for r in refs.values() if (r.workspace_binding or {}).get('workspace_id') == workspace_id
                          and (r.workspace_binding or {}).get('role') == 'desktop'
                          and r.state.value == 'running' and r.workspace_binding['incarnation'] == raw['incarnation']]
            if len(candidates) != 1:
                raise WorkspaceError('desktop-not-ready', 'workspace has no unique running desktop')
            binding = dict(candidates[0].workspace_binding, role='client')
        # Remote probes never hold the read-only snapshot lock.
        info = self.systemd_transport.workspace_call('verify', project_root=self.project.path, binding=binding)
        return dict(schema=VERSION, workspace_id=workspace_id, desktop_runtime_id=binding['desktop_runtime_id'],
                    display=binding['display'], vnc_endpoint=binding['vnc_endpoint'], ready=info['ready'], advisory=True, observed_at=time.time())

    def workspace_application_catalogue(self, *, after=None, limit=50):
        from .specification import discover_applications
        from .workspaces import VERSION, WorkspaceError
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError('invalid-limit', 'limit must be 1 through 100')
        from .workspaces import MAXIMUM_RECORDS, MAXIMUM_BYTES
        from itertools import islice
        paths = list(islice(self.project.application_dir.glob('*/application.py'), MAXIMUM_RECORDS + 1))
        if len(paths) > MAXIMUM_RECORDS or sum(path.stat().st_size for path in paths) > MAXIMUM_BYTES:
            raise WorkspaceError('inspection-capacity', 'application catalogue exceeds supported bound')
        applications = discover_applications(self.project.application_dir)
        names = [n for n in sorted(applications) if after is None or n > after]
        items = [dict(name=n, description=applications[n].description, role=applications[n].workspace_role,
                      eligible=applications[n].workspace_role == 'client',
                      reason=None if applications[n].workspace_role == 'client' else 'application-ineligible') for n in names[:limit]]
        return dict(schema=VERSION, items=items, next=names[limit-1] if len(names) > limit else None, advisory=True)

    def prepare_application(self, application_name, *, upgrade=False, retry=False):
        """Explicitly prepare/upgrade; poll refresh_application_preparation to completion."""
        from .application_storage import ApplicationStorage
        return ApplicationStorage(self.project).begin(self, application_name, upgrade=upgrade, retry=retry)

    def application_preparation(self, application_name):
        from .application_storage import ApplicationStorage
        return ApplicationStorage(self.project).load(application_name)

    def refresh_application_preparation(self, application_name):
        from .application_storage import ApplicationStorage
        return ApplicationStorage(self.project).refresh(self, application_name)

    def abandon_application_preparation(self, application_name):
        """Stop preparation, retain partial data and uncertainty; never roll back migrations."""
        from .application_storage import ApplicationStorage
        return ApplicationStorage(self.project).refresh(self, application_name, abandon=True)

    def delete_application_storage(self, application_name, *, storage_id):
        from .application_storage import ApplicationStorage
        return ApplicationStorage(self.project).delete(self, application_name, storage_id=storage_id)

    def cancel_application_launch(self, application_name, *, request_id, workspace_id=None, parameters=None):
        from .cancellation import cancel_launch
        return cancel_launch(self, application_name, request_id, workspace_id=workspace_id, parameters=parameters)

    def launch_application(self, application_name: str, *, request_id: str | None = None,
                           workspace_id=None, parameters=None, program_name=None):
        """Launch or replace according to the application's instance policy."""
        from .workspaces import Workspaces
        with self._mutation(launch_intent=(application_name, request_id, workspace_id, parameters)):
            workspace_id = Workspaces(self.project).selected(workspace_id, parameters)
            reconciler = self._reconciler()
            if request_id is not None:
                Retention(self.project).validate(request_id)
            historical = reconciler.operations.for_request(
                request_id, 'launch', application=application_name,
                selected_program=program_name,
            )
            if historical is not None:
                return historical
            self._bind_direct_request(
                request_id, ApplicationRequestOperation.LAUNCH,
                application=application_name, workspace_id=workspace_id,
                program_name=program_name,
            )
            return reconciler.launch_application(
                application_name, request_id=request_id,
                workspace_id=workspace_id, program_name=program_name,
            )

    def restart_application_runtime(
        self, runtime_id: str, *, request_id: str | None = None
    ):
        """Create a new runtime identity for the same application instance."""
        with self._mutation():
            reconciler = self._reconciler()
            if request_id is not None:
                Retention(self.project).validate(request_id)
            historical = reconciler.operations.for_request(request_id, 'restart', target_runtime_id=runtime_id)
            if historical is not None:
                return historical
            self._bind_direct_request(request_id, ApplicationRequestOperation.RESTART, runtime_id=runtime_id)
            return reconciler.restart_application_runtime(runtime_id, request_id=request_id)

    def terminate_application_runtime(self, runtime_id: str, *, request_id: str | None = None):
        """Terminate exactly one immutable application runtime identity."""
        with self._mutation():
            reconciler = self._reconciler()
            if request_id is not None:
                Retention(self.project).validate(request_id)
            historical = reconciler.operations.for_request(request_id, 'terminate', target_runtime_id=runtime_id)
            if historical is not None:
                return historical
            self._bind_direct_request(request_id, ApplicationRequestOperation.TERMINATE, runtime_id=runtime_id)
            return reconciler.terminate_application_runtime(runtime_id, request_id=request_id)

    def application_runtimes(self):
        """Read persisted application-runtime records without taking mutation lock."""
        return RuntimeReferenceStore(self.project.runtime_reference_file).load()

    def application_runtime(self, runtime_id: str):
        """Return one exact immutable runtime identity, or None when unknown."""
        return self.application_runtimes().get(runtime_id)

    def current_application_runtimes(self, application_name: str | None = None):
        """Return live/unresolved runtimes newest first."""
        store = RuntimeReferenceStore(self.project.runtime_reference_file)
        return store.current(store.load(), application=application_name)

    def application_runtime_history(
        self, application_name: str | None = None, *, limit: int | None = 25
    ):
        """Return successful and failed completed runtimes in one history."""
        store = RuntimeReferenceStore(self.project.runtime_reference_file)
        return store.history(
            store.load(), application=application_name, limit=limit
        )

    def application_instance_runtimes(self, instance_id: str):
        """Return every runtime generation of one application instance."""
        store = RuntimeReferenceStore(self.project.runtime_reference_file)
        return store.ordered(store.load(), instance_id=instance_id)

    def _request_store(self):
        return ApplicationRequestStore(
            self.project.application_request_dir,
            self.project.application_request_result_dir,
        )

    def _bind_direct_request(self, request_id, operation, *, application=None,
                             runtime_id=None, workspace_id=None, program_name=None):
        if request_id is None:
            return
        store = self._request_store()
        store.submit(ApplicationRequest(
            request_id, operation, application, runtime_id,
            time.time(), workspace_id, program_name,
        ))
        result = store.result(request_id)
        if result is not None and result.status != ApplicationRequestStatus.PENDING:
            # A result without an operation can be a pre-launch validation failure.
            # Never turn that completed request into a new execution.
            raise RuntimeOperationError(result.error or 'request already completed; inspect its result')

    def issue_application_request_id(self):
        """Issue an identity valid for first submission for 48 hours; accepts no work."""
        self.project.require_state_allowed()
        with ProjectLock(self.project.lock_file, timeout_seconds=self.mutation_lock_timeout_seconds):
            return Retention(self.project).issue()

    def _submit_request(self, operation, *, application=None, runtime_id=None,
                        request_id=None, workspace_id=None, parameters=None,
                        program_name=None):
        self.project.require_state_allowed()
        from .workspaces import Workspaces, WorkspaceError
        workspace_id = Workspaces(self.project).selected(workspace_id, parameters)
        request = ApplicationRequest(
            request_id=request_id or self.issue_application_request_id(),
            operation=operation,
            application=application,
            runtime_id=runtime_id,
            created_at=time.time(),
            workspace_id=workspace_id,
            program_name=program_name,
        )
        with self._mutation(launch_intent=(application, request.request_id, workspace_id, None) if operation == ApplicationRequestOperation.LAUNCH else None):
            if workspace_id is not None and Workspaces(self.project).load(workspace_id)['state'] != 'registered':
                raise WorkspaceError('workspace-deleted', 'workspace is deleted')
            Retention(self.project).validate(request.request_id)
            self._request_store().submit(request)
        return request

    def request_application_launch(
        self, application_name: str, *, request_id: str | None = None,
        workspace_id=None, parameters=None, program_name=None
    ):
        """Queue an idempotent launch/replacement for PROJECT_EVALUATION."""
        return self._submit_request(
            ApplicationRequestOperation.LAUNCH,
            application=application_name,
            request_id=request_id, workspace_id=workspace_id, parameters=parameters,
            program_name=program_name,
        )

    def request_application_restart(
        self, runtime_id: str, *, request_id: str | None = None
    ):
        """Queue an idempotent restart of one exact runtime identity."""
        return self._submit_request(
            ApplicationRequestOperation.RESTART,
            runtime_id=runtime_id,
            request_id=request_id,
        )

    def request_application_termination(
        self, runtime_id: str, *, request_id: str | None = None
    ):
        """Queue idempotent termination of one exact runtime identity."""
        return self._submit_request(
            ApplicationRequestOperation.TERMINATE,
            runtime_id=runtime_id,
            request_id=request_id,
        )

    def observe_issuer_request(self, request_id: str):
        """Trust-rooted, read-only certificate issuer evidence for one saved request.

        The privileged reader derives storage, UID, policy and operation
        identities from durable controller records, not caller-supplied JSON.
        """
        return self.systemd_transport.issuer_observe(
            project_root=self.project.path, request_id=request_id,
        )

    def inspect_issuer_result(self, request_id: str):
        """Read a selected inspect result through privileged, invocation-scoped evidence.

        Caller supplies only a durable request identity. Root-control resolves
        the exact saved runtime, issuer grant and journal invocation.
        """
        return self.systemd_transport.issuer_inspect_result(
            project_root=self.project.path, request_id=request_id,
        )

    def application_request_result(self, request_id: str):
        return self._request_store().result(request_id)

    def issue_build_request_id(self):
        from .build_jobs import BuildJobs
        self.project.require_state_allowed()
        with ProjectLock(self.project.lock_file, timeout_seconds=self.mutation_lock_timeout_seconds):
            return BuildJobs(self).issue()

    def register_build_root(self, *, resource_id, **inputs):
        from .build_jobs import BuildJobs
        with self._mutation(build_entity='resource:'+resource_id):
            return BuildJobs(self).register(resource_id, **inputs)

    def inspect_build_root(self, resource_id):
        """Read-only remote registration progress, even with a mutation marker."""
        from .build_jobs import BuildJobs
        from .errors import RecoveryRequired
        self.project.require_state_allowed()
        local = BuildJobs(self).load('resource:'+resource_id)
        remote = self.systemd_transport.build_registration_status(
            project_root=self.project.path, resource_id=resource_id)
        if remote.get('resource_id') != resource_id or (remote.get('state') != 'absent' and remote.get('intent') != local['intent']):
            raise RecoveryRequired('registration observation differs from saved intent')
        return remote

    def submit_build_job(self, *, request_id, **request):
        from .build_jobs import BuildJobs
        job_id = BuildJobs.job_id(request_id)
        with self._mutation(build_entity='job:'+job_id):
            return BuildJobs(self).submit(request_id, request)

    def list_build_jobs(self, *, after=None, limit=50):
        """Read-only, ID-ordered pages; concurrent changes are not a snapshot."""
        from .build_jobs import BuildJobs
        from .build_protocol import identity
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('limit must be an integer from 1 through 100')
        if after is not None:
            identity(after)
        store = BuildJobs(self)
        records = []
        for path in sorted(store.directory.glob('*.json')):
            if after is not None and path.stem <= after:
                continue
            try:
                raw = store.load('job:' + path.stem)
            except RuntimeOperationError:
                if not path.exists():
                    continue  # Concurrent retention.
                raise
            fields = ('job_id', 'runtime_id', 'state', 'outcome', 'exit_code', 'signal',
                      'process_cleanup_complete', 'resources_released')
            records.append({**{key: raw.get(key) for key in fields},
                            'log_identity_available': bool(raw.get('boot_id') and raw.get('invocation_id'))})
            if len(records) > limit:
                break
        page = records[:limit]
        return dict(jobs=page, has_more=len(records) > limit,
                    next_after=page[-1]['job_id'] if page else after)

    def application_processes(self, runtime_id, program, *, limit=50):
        """Read a bounded live process snapshot without a project mutation lock."""
        return self.systemd_transport.application_processes(project_root=self.project.path,
                            runtime_id=runtime_id, program=program, limit=limit)

    def build_job_processes(self, job_id, *, limit=50):
        """Read the saved command and current processes of a finite build job."""
        return self.systemd_transport.build_job_processes(project_root=self.project.path,
                                                         job_id=job_id, limit=limit)

    def build_job_logs(self, job_id, *, cursor=None, limit=50):
        """Read saved invocation logs without acquiring the mutation lock."""
        return self.systemd_transport.build_job_logs(project_root=self.project.path,
                                                     job_id=job_id, cursor=cursor, limit=limit)

    def inspect_build_job(self, job_id):
        from .build_jobs import BuildJobs
        return BuildJobs(self).load('job:'+job_id)

    def refresh_build_job(self, job_id):
        from .build_jobs import BuildJobs
        with self._mutation(build_entity='job:'+job_id):
            return BuildJobs(self).refresh(job_id)

    def cancel_build_job(self, job_id):
        from .build_jobs import BuildJobs
        with self._mutation(build_entity='job:'+job_id):
            return BuildJobs(self).cancel(job_id)

    def release_build_job(self, job_id):
        from .build_jobs import BuildJobs
        with self._mutation(build_entity='job:'+job_id):
            return BuildJobs(self).release_job(job_id)

    def release_build_root(self, resource_id):
        from .build_jobs import BuildJobs
        with self._mutation(build_entity='resource:'+resource_id):
            return BuildJobs(self).release_root(resource_id)

    def preflight_application_launch(self, application_name: str, *, workspace_id=None, parameters=None):
        """Advisory read-only launch checks; does not accept or prepare work."""
        from .preflight import preflight_launch
        result = preflight_launch(self, application_name)
        if result.get("snapshot") == "busy":
            return result
        from .workspaces import Workspaces, WorkspaceError
        from .specification import discover_applications
        from .inspection import _snapshot, RecoveryInspection
        selected = None
        try:
            with _snapshot(self.project, RecoveryInspection()) as available:
                if not available:
                    raise WorkspaceError('inspection-busy', 'workspace inspection is busy')
                selected = Workspaces(self.project).selected(workspace_id, parameters)
                application = discover_applications(self.project.application_dir).get(application_name)
                if application and application.workspace_role:
                    if selected is None:
                        raise WorkspaceError('workspace-required', 'select a registered workspace')
                    if Workspaces(self.project).load(selected)['state'] != 'registered':
                        raise WorkspaceError('workspace-deleted', 'workspace is deleted')
                    if not application.multiple_instances:
                        refs, _, _ = Workspaces(self.project).evidence()
                        if any((r.workspace_binding or {}).get('workspace_id') != selected for r in RuntimeReferenceStore.launch_candidates(refs, application=application_name)):
                            raise WorkspaceError('cross-workspace-singleton', 'singleton belongs to another workspace')
                elif selected is not None:
                    raise WorkspaceError('application-ineligible', 'application does not opt into workspace networking/display')
            if application and application.workspace_role == 'client':
                self.workspace_desktop_access(selected)
            if application and application.workspace_role:
                result['checks'].append(dict(code='workspace-binding', status='ready', message='Workspace binding is available; readiness remains advisory.'))
        except WorkspaceError as exc:
            result['checks'].append(dict(code=exc.code, status='blocked', message=str(exc)))
            result['status'] = 'blocked'
        except Exception:
            result['checks'].append(dict(code='workspace-unverified', status='unable-to-verify', message='Workspace inspection could not verify readiness.'))
            if result['status'] != 'blocked':
                result['status'] = 'unable-to-verify'
        return result

    def recovery_explanation(self, *, operation_id=None, limit=50):
        """Explain saved recovery evidence without transport calls or mutations."""
        from .explanations import explain_recovery
        return explain_recovery(self.project, operation_id=operation_id, limit=limit)

    def recover_interrupted_session(self):
        """Explicit reviewed recovery of an orphaned evaluation session marker.

        This deliberately bypasses ordinary mutation admission ONLY while
        checking the standalone schema-1 guard, under the project lock.
        No application operation, cleanup, build or systemd action is run.
        Any ambiguity preserves the original mutation block.
        """
        self.project.require_state_allowed()
        with ProjectLock(self.project.lock_file,
                         timeout_seconds=self.mutation_lock_timeout_seconds):
            return self._reconciler().operations.recover_interrupted_session()

    def recovery_status(self):
        """Inspect persisted recovery evidence without writes or systemd access.

        Advisory snapshot only; every mutation still performs its own recovery.
        """
        return inspect_recovery(self.project)

    def application_operations(self):
        """Inspect authoritative operation records without lifecycle mutation."""
        return self._reconciler().operations.store.records()

    def abandon_application_operation(self, operation_id):
        """Explicitly abandon an uncertain operation, retaining cleanup protection."""
        self.project.require_state_allowed()
        with ProjectLock(self.project.lock_file, timeout_seconds=self.mutation_lock_timeout_seconds):
            return self._reconciler().operations.abandon(operation_id)

    def application_logs(self, runtime_id, program, *, cursor=None, limit=50):
        """Read a bounded journal page; never reconcile or acquire mutation lock."""
        return self.systemd_transport.application_logs(project_root=self.project.path,
            runtime_id=runtime_id, program=program, cursor=cursor, limit=limit)

    def observe_application_runtime(self, runtime_id):
        from .station import observe_runtime
        return observe_runtime(self, runtime_id)

    def execute_application_operation(self, operation, target, *, request_id):
        """Direct execution with a JSON-safe outcome; no background queue."""
        from .station import execute_operation
        return execute_operation(self, operation, target, request_id=request_id)
