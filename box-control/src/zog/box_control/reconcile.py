from __future__ import annotations

from dataclasses import replace

from .diagnostics import Fault, fault_from_exception
from .boot import current_boot_id
from .operations import OperationManager
from .dependency import DependencyClosure, resolve
from .errors import (
    BoxControlError,
    PersistenceError,
    ConfigurationError,
    LifecycleOperationPending,
    RuntimeOperationError,
)
from .generation import GenerationStore
from .model import (
    ApplicationRuntimeState,
    ApplicationSpec,
    ApplicationStartPolicy,
    ReconcileReport,
)
from .requests import (
    ApplicationRequest,
    ApplicationRequestOperation,
    ApplicationRequestResult,
    ApplicationRequestStatus,
    ApplicationRequestStore,
)
from .runtime.reference import (
    ApplicationRuntimeReference,
    RuntimeReferenceStore,
    new_application_instance_id,
)
from .specification import discover_applications
from .state import StateStore


_TERMINAL = frozenset(
    {ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED}
)
_NONTERMINAL = frozenset(ApplicationRuntimeState) - _TERMINAL


class Reconciler:
    """One serialized project reconciliation transaction."""

    def __init__(
        self,
        project,
        *,
        image_provider,
        systemd_runtime,
        boot_id_provider=current_boot_id,
    ):
        self.project = project
        self.image_provider = image_provider
        self.systemd_runtime = systemd_runtime
        self.boot_id_provider = boot_id_provider
        self.operations = OperationManager(project, systemd_runtime, boot_id_provider=boot_id_provider)
        self.state = StateStore(project.state_file)
        self.references = RuntimeReferenceStore(project.runtime_reference_file)
        self.requests = ApplicationRequestStore(
            project.application_request_dir,
            project.application_request_result_dir,
        )

    @staticmethod
    def _protected_generations(references):
        return {
            reference.generation
            for reference in references.values()
            if reference.state in _NONTERMINAL or reference.cleanup_pending
        }

    def _storage_generations(self):
        from .application_storage import ApplicationStorage
        return {r['generation'] for r in ApplicationStorage(self.project).records() if r['state'] != 'deleted'}

    def _protected_software(self, references, applications):
        from .application_software import protection_from_binding, protection_from_reference
        views, artifacts = set(), set()
        for reference in references.values():
            if reference.state in _NONTERMINAL or reference.cleanup_pending:
                found_views, found_artifacts = protection_from_reference(reference)
                views.update(found_views)
                artifacts.update(found_artifacts)
        for record in self.operations.store.records():
            if not record['finished'] or not record['published']:
                found_views, found_artifacts = protection_from_binding(record.get('software_binding'))
                views.update(found_views)
                artifacts.update(found_artifacts)
        for application in applications.values():
            if application.software_view:
                views.add(application.software_view)
        return views, artifacts

    def _eligible_software_reclamation(self, references):
        from .application_software import protection_from_binding, protection_from_reference
        views, artifacts = set(), set()
        for reference in references.values():
            found_views, found_artifacts = protection_from_reference(reference)
            views.update(found_views)
            artifacts.update(found_artifacts)
        for record in self.operations.store.records():
            found_views, found_artifacts = protection_from_binding(record.get('software_binding'))
            views.update(found_views)
            artifacts.update(found_artifacts)
        return views, artifacts

    @staticmethod
    def _report_reclamation(report, reclaimed, *, phase):
        if reclaimed:
            report.messages.append(
                f"reclaimed {len(reclaimed)} unused rootfs generation(s) {phase}: "
                + ", ".join(generation.fingerprint[:16] for generation in reclaimed)
            )

    def _observe_references(self, references, report):
        for runtime_id in sorted(references):
            before = references[runtime_id]
            if before.state in _TERMINAL and not before.cleanup_pending:
                continue
            observed = (before if before.state in _TERMINAL
                        else self.systemd_runtime.observe_reference(before))
            if observed.state in _TERMINAL:
                observed = self.systemd_runtime.cleanup(
                    observed, references=references, store=self.references
                )
            elif observed.state == ApplicationRuntimeState.TERMINATING:
                observed = self.systemd_runtime.terminate(
                    observed, references=references, store=self.references
                )
            references[runtime_id] = observed
            if observed.cleanup_error:
                report.messages.append(f"{runtime_id}: cleanup pending: {observed.cleanup_error}")
            if observed.state != before.state:
                report.messages.append(
                    f"{observed.application}/{runtime_id}: {before.state.value} -> "
                    f"{observed.state.value} from systemd observation"
                )
        self.references.save(references)

    @staticmethod
    def _live_references(references, *, application=None, instance_id=None):
        return RuntimeReferenceStore.launch_candidates(
            references, application=application, instance_id=instance_id
        )

    @classmethod
    def _live_runtime_ids(cls, references, application):
        return [
            reference.runtime_id
            for reference in cls._live_references(references, application=application)
        ]

    def _record_state(
        self, state, applications, closure, references, selection, *, boot_id
    ):
        state["boot_id"] = boot_id
        state["rootfs"] = {
            "generation": selection.generation,
            "status": "selected",
            "packages": list(selection.manifest.get("packages", ())),
            "distribution_packages": list(
                selection.manifest.get("distribution_packages", ())
            ),
        }
        application_records = {}
        state["applications"] = application_records
        for name in closure.applications:
            spec = applications[name]
            resolution = closure.resolution_for(name)
            application_records[name] = {
                "runtime_fingerprint": resolution.runtime_fingerprint,
                "dependencies": list(resolution.dependencies),
                "runtime_ids": self._live_runtime_ids(references, name),
                "start_policy": spec.start_policy.value,
                "multiple_instances": spec.multiple_instances,
                "software_view": spec.software_view,
            }
        state["application_runtimes"] = {
            runtime_id: {
                "application": reference.application,
                "instance_id": reference.instance_id,
                "generation": reference.generation,
                "software_view": reference.software_view,
                "software_binding_digest": reference.software_binding_digest,
                "software_manifest_digest": reference.software_manifest_digest,
                "software_artifacts": list(reference.software_artifacts),
                "software_mounts": [list(pair) for pair in reference.software_mounts],
                "state": reference.state.value,
                "slice_name": reference.slice_name,
                "request_id": reference.request_id,
                "replaces_runtime_id": reference.replaces_runtime_id,
                "boot_id": reference.boot_id,
                "error": reference.error,
                "cleanup_pending": reference.cleanup_pending,
                "cleanup_error": reference.cleanup_error,
                "programs": [
                    {
                        "program": program.program,
                        "command": list(program.command),
                        "unit_name": program.unit_name,
                        "invocation_id": program.invocation_id,
                        "active_state": program.active_state,
                        "sub_state": program.sub_state,
                        "result": program.result,
                        "main_pid": program.main_pid,
                        "control_group": program.control_group,
                    }
                    for program in reference.programs
                ],
            }
            for runtime_id, reference in sorted(references.items())
        }
        state["generation"] = int(state.get("generation", 0)) + 1

    @staticmethod
    def _application_fingerprint(closure: DependencyClosure, application: str) -> str:
        return closure.resolution_for(application).runtime_fingerprint

    @staticmethod
    def _accepted_launch(reference):
        if reference.error or reference.state == ApplicationRuntimeState.FAILED:
            raise RuntimeOperationError(reference.error or "application launch failed")
        if reference.state not in (
            ApplicationRuntimeState.RUNNING, ApplicationRuntimeState.TERMINATED
        ):
            raise LifecycleOperationPending("application launch has not committed")
        return reference

    def _launch(
        self,
        application: ApplicationSpec,
        *,
        selection,
        closure: DependencyClosure,
        references: dict[str, ApplicationRuntimeReference],
        instance_id: str,
        request_id: str | None,
        replaces_runtime_id: str | None,
        boot_id: str,
        stop_runtime_ids=(),
        operation="launch",
        target_runtime_id=None,
        selected_program=None,
    ) -> ApplicationRuntimeReference:
        if request_id is not None:
            existing = self.references.for_request(references, request_id)
            if existing is not None:
                return self._accepted_launch(existing)
        from .application_storage import ApplicationStorage
        application = ApplicationStorage(self.project).bind(application, selection.generation)
        from .application_software import resolve_binding
        application = resolve_binding(self.project, application, selection)
        if selected_program is not None:
            try:
                chosen = application.program(selected_program)
            except KeyError as exc:
                raise ConfigurationError(
                    f"application {application.name} does not declare program {selected_program}"
                ) from exc
            application = replace(
                application, programs=(chosen,), selected_program=selected_program,
            )
        elif application.name == "certify-manage" and len(application.programs) != 1:
            raise ConfigurationError("certify-manage requires exact program selection")
        return self.operations.run(application=application, selection=selection,
            references=references, instance_id=instance_id, request_id=request_id,
            replaces_runtime_id=replaces_runtime_id, boot_id=boot_id,
            fingerprint=self._application_fingerprint(closure, application.name),
            stop_runtime_ids=stop_runtime_ids, operation=operation,
            target_runtime_id=target_runtime_id, selected_program=selected_program)

    def _terminate_live(self, references, live):
        remaining = []
        for reference in live:
            result = self.systemd_runtime.terminate(
                reference, references=references, store=self.references
            )
            references[reference.runtime_id] = result
            if result.state in _NONTERMINAL or result.cleanup_pending:
                remaining.append(result)
        return tuple(remaining)

    @staticmethod
    def _is_stale_runtime(references, reference):
        identity = (reference.created_at, reference.runtime_id)
        return any(
            candidate.instance_id == reference.instance_id
            and candidate.runtime_id != reference.runtime_id
            and (candidate.created_at, candidate.runtime_id) > identity
            for candidate in references.values()
        )

    def _launch_application_loaded(
        self,
        application_name: str,
        *,
        applications,
        closure,
        references,
        selection,
        boot_id,
        request_id=None,
        pending_replaced_runtime_id=None,
        workspace_id=None,
        program_name=None,
    ):
        if program_name is not None and request_id is None:
            raise ConfigurationError('selected program requires a durable request ID')
        historical = self.operations.for_request(
            request_id, 'launch', application=application_name,
            selected_program=program_name,
        )
        if historical is not None:
            return historical
        if request_id is not None:
            existing = self.references.for_request(references, request_id)
            if existing is not None:
                return self._accepted_launch(existing)
        try:
            application = applications[application_name]
        except KeyError as exc:
            raise ConfigurationError(f"unknown application: {application_name}") from exc

        from .workspaces import Workspaces, WorkspaceError
        application = Workspaces(self.project).resolve(application, workspace_id, self.systemd_runtime.transport, boot_id=boot_id)
        if program_name is not None and (
                workspace_id is not None or application.multiple_instances
                or application.start_policy != ApplicationStartPolicy.EXTERNALLY_CONTROLLED
                or not application.persistent or application.execution_identity != 'application'):
            raise ConfigurationError(
                'selected-program execution requires a persistent externally controlled managed singleton'
            )
        replaces_runtime_id = pending_replaced_runtime_id
        live = []
        if not application.multiple_instances:
            live = self._live_references(references, application=application.name)
            if any((r.workspace_binding or {}).get('workspace_id') != workspace_id for r in live):
                raise WorkspaceError('cross-workspace-singleton', 'single-instance application already belongs to another workspace')
            if live:
                if program_name is not None:
                    raise LifecycleOperationPending(
                        'selected-program execution conflicts with unresolved application runtime'
                    )
                replaces_runtime_id = live[-1].runtime_id

        instance_id = new_application_instance_id(
            application.name,
            multiple_instances=application.multiple_instances,
            request_id=request_id,
        )
        return self._launch(
            application,
            selection=selection,
            closure=closure,
            references=references,
            instance_id=instance_id,
            request_id=request_id,
            replaces_runtime_id=replaces_runtime_id,
            boot_id=boot_id,
            stop_runtime_ids=tuple(reference.runtime_id for reference in live),
            selected_program=program_name,
        )

    def _restart_application_runtime_loaded(
        self,
        runtime_id,
        *,
        applications,
        closure,
        references,
        selection,
        boot_id,
        request_id=None,
    ):
        historical = self.operations.for_request(request_id, 'restart', target_runtime_id=runtime_id)
        if historical is not None:
            return historical
        if request_id is not None:
            existing = self.references.for_request(references, request_id)
            if existing is not None:
                return self._accepted_launch(existing)
        try:
            previous = references[runtime_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown application runtime: {runtime_id}") from exc
        if previous.request_id and any(
                record.get('selected_program') is not None
                and record['request_id'] == previous.request_id
                for record in self.operations.store.records()):
            raise ConfigurationError(
                'selected-program execution cannot be restarted; submit a new exact-program request'
            )
        if self._is_stale_runtime(references, previous):
            raise ConfigurationError(
                f"application runtime is no longer current for its instance: {runtime_id}"
            )
        try:
            application = applications[previous.application]
        except KeyError as exc:
            raise ConfigurationError(
                f"cannot restart undeclared application: {previous.application}"
            ) from exc

        if previous.workspace_binding:
            from .workspaces import Workspaces, WorkspaceError
            if previous.workspace_binding['role'] == 'desktop':
                raise WorkspaceError('desktop-restart-requires-idle-seat', 'stop desktop and all attachments, then explicitly start a new desktop')
            if application.workspace_role != 'client':
                raise WorkspaceError('application-ineligible', 'restart definition no longer opts into workspace attachment')
            Workspaces(self.project).verify_bound(previous.workspace_binding, self.systemd_runtime.transport)
            application = Workspaces(self.project).attach(application, previous.workspace_binding)
        live = self._live_references(references, instance_id=previous.instance_id)
        return self._launch(
            application,
            selection=selection,
            closure=closure,
            references=references,
            instance_id=previous.instance_id,
            request_id=request_id,
            replaces_runtime_id=previous.runtime_id,
            boot_id=boot_id,
            stop_runtime_ids=tuple(reference.runtime_id for reference in live),
            operation="restart", target_runtime_id=runtime_id,
        )

    def _terminate_application_runtime_loaded(self, runtime_id, *, references, request_id=None):
        try:
            reference = references[runtime_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown application runtime: {runtime_id}") from exc
        if self._is_stale_runtime(references, reference):
            raise ConfigurationError(
                f"application runtime is no longer current for its instance: {runtime_id}"
            )
        if reference.workspace_binding and reference.workspace_binding['role'] == 'desktop':
            from .workspaces import Workspaces, WorkspaceError
            members = Workspaces(self.project).members(reference.workspace_binding['workspace_id'])
            if any(item.get('runtime_id') != runtime_id for item in members['items']) or members['next']:
                raise WorkspaceError('workspace-in-use', 'stop attached clients and resolve pending launches before stopping desktop')
        return self.operations.run(application=None, selection=None,
            references=references, instance_id=reference.instance_id,
            request_id=request_id, replaces_runtime_id=None,
            boot_id=self.boot_id_provider(), fingerprint=None,
            stop_runtime_ids=(runtime_id,), operation="terminate", target_runtime_id=runtime_id)

    @staticmethod
    def _request_result(
        request,
        status,
        *,
        reference=None,
        error=None,
        replaced_runtime_id=None,
    ):
        return ApplicationRequestResult(
            request_id=request.request_id,
            operation=request.operation,
            status=status,
            workspace_id=request.workspace_id,
            application=(
                reference.application if reference is not None else request.application
            ),
            target_runtime_id=request.runtime_id,
            runtime_id=(
                reference.runtime_id if reference is not None else request.runtime_id
            ),
            replaced_runtime_id=(
                reference.replaces_runtime_id
                if reference is not None
                else replaced_runtime_id
            ),
            error=error,
            program_name=request.program_name,
        )

    def _queued_boot_launches(self, applications):
        """Current queued boot launches suppress a redundant policy start.

        Malformed requests remain the responsibility of _process_requests;
        they must not change automatic policy decisions.
        """
        selected = set()
        for path in self.requests.paths():
            try:
                request = self.requests.load(path)
                if (request.operation != ApplicationRequestOperation.LAUNCH
                        or request.program_name is not None
                        or request.workspace_id is not None
                        or request.application not in applications):
                    continue
                spec = applications[request.application]
                if (spec.multiple_instances or spec.start_policy not in {
                        ApplicationStartPolicy.KEEP_RUNNING,
                        ApplicationStartPolicy.START_ONCE_PER_BOOT}):
                    continue
                prior = self.requests.result(request.request_id)
                if prior is None or prior.status == ApplicationRequestStatus.PENDING:
                    selected.add(request.application)
            except (ConfigurationError, RuntimeOperationError):
                continue
        return selected

    def _prior_policy_launch(self, request, prior, *, applications, closure,
                             references, selection, boot_id):
        """Match a queued-before-launch request to a committed policy start.

        A later explicit launch or a pending/uncertain request MUST NOT be
        absorbed by this special boot ordering recovery case.
        """
        if prior is not None or request.workspace_id is not None:
            return None
        spec = applications.get(request.application)
        if (spec is None or spec.multiple_instances
                or spec.start_policy not in {
                    ApplicationStartPolicy.KEEP_RUNNING,
                    ApplicationStartPolicy.START_ONCE_PER_BOOT}):
            return None
        compatible = []
        for operation in self.operations.store.records():
            if (operation['operation'] != 'launch'
                    or operation['request_id'] is not None
                    or operation['phase'] != 'committed'
                    or not operation['finished'] or not operation['published']
                    or operation['boot_id'] != boot_id
                    or operation['generation_root'] is None
                    or operation['created_at'] < request.created_at
                    or operation['stop_runtime_ids']
                    or operation['application']['name'] != request.application):
                continue
            runtime_id = operation['new_runtime_id']
            ref = references.get(runtime_id)
            if (ref is None or ref.request_id is not None
                    or ref.boot_id != boot_id
                    or ref.generation != selection.generation
                    or ref.state != ApplicationRuntimeState.RUNNING
                    or ref.error
                    or ref.application_fingerprint != self._application_fingerprint(
                        closure, request.application)
                    or not ref.programs
                    or any(p.active_state != 'active' for p in ref.programs)
                    or self._is_stale_runtime(references, ref)):
                continue
            compatible.append(ref)
        if len(compatible) != 1:
            return None
        live = self._live_references(references, application=request.application)
        if len(live) != 1 or live[0].runtime_id != compatible[0].runtime_id:
            return None
        return compatible[0]

    def _process_requests(
        self,
        *,
        applications,
        closure,
        references,
        selection,
        boot_id,
        report,
    ):
        for path in self.requests.paths():
            try:
                request = self.requests.load(path)
            except ConfigurationError as exc:
                report.ok = False
                report.errors.append(str(exc))
                report.faults.append(fault_from_exception(exc))
                continue

            prior = self.requests.result(request.request_id)
            if prior is not None and (
                    prior.program_name != request.program_name
                    or prior.operation != request.operation
                    or prior.workspace_id != request.workspace_id):
                raise RecoveryRequired(
                    'saved request result contradicts queued selected-program intent'
                )
            if prior is not None and prior.status in {
                ApplicationRequestStatus.SATISFIED,
                ApplicationRequestStatus.FAILED,
                ApplicationRequestStatus.ABANDONED,
                ApplicationRequestStatus.CANCELLED,
            }:
                self.requests.remove(path)
                report.messages.append(
                    f"application request {request.request_id}: already {prior.status.value}"
                )
                continue

            try:
                if request.operation == ApplicationRequestOperation.LAUNCH:
                    reference = (None if request.program_name is not None else
                        self._prior_policy_launch(
                            request, prior, applications=applications,
                            closure=closure, references=references, selection=selection,
                            boot_id=boot_id,
                        ))
                    if reference is None:
                        reference = self._launch_application_loaded(
                            request.application or "",
                            workspace_id=request.workspace_id,
                            applications=applications,
                            closure=closure,
                            references=references,
                            selection=selection,
                            boot_id=boot_id,
                            request_id=request.request_id,
                            program_name=request.program_name,
                            pending_replaced_runtime_id=(
                                prior.replaced_runtime_id if prior is not None else None
                            ),
                        )
                    else:
                        report.messages.append(
                            f"application request {request.request_id}: "
                            f"matched committed boot-policy runtime {reference.runtime_id}"
                        )
                elif request.operation == ApplicationRequestOperation.RESTART:
                    reference = self._restart_application_runtime_loaded(
                        request.runtime_id or "",
                        applications=applications,
                        closure=closure,
                        references=references,
                        selection=selection,
                        boot_id=boot_id,
                        request_id=request.request_id,
                    )
                else:
                    reference = self._terminate_application_runtime_loaded(
                        request.runtime_id or "", references=references, request_id=request.request_id
                    )
            except LifecycleOperationPending as exc:
                self.requests.save_result(
                    self._request_result(
                        request,
                        ApplicationRequestStatus.PENDING,
                        error=str(exc),
                        replaced_runtime_id=exc.replaced_runtime_id,
                    )
                )
                report.messages.append(
                    f"application request {request.request_id}: pending: {exc}"
                )
                continue
            except PersistenceError:
                raise
            except BoxControlError as exc:
                self.requests.save_result(
                    self._request_result(
                        request, ApplicationRequestStatus.FAILED, error=str(exc),
                        reference=self.references.for_request(references, request.request_id),
                    )
                )
                self.requests.remove(path)
                report.ok = False
                report.errors.append(
                    f"application request {request.request_id} failed: {exc}"
                )
                report.faults.append(fault_from_exception(exc, request_id=request.request_id))
                continue

            self.requests.save_result(
                self._request_result(
                    request, ApplicationRequestStatus.SATISFIED, reference=reference
                )
            )
            self.requests.remove(path)
            report.messages.append(
                f"application request {request.request_id}: satisfied by runtime "
                f"{reference.runtime_id}"
            )

    @staticmethod
    def _started_during_boot(references, application, boot_id):
        return any(
            reference.purpose == "application" and reference.application == application and reference.boot_id == boot_id
            for reference in references.values()
        )

    def reconcile(self):
        report = ReconcileReport(ok=True)
        try:
            self.project.require_state_allowed()
            self.systemd_runtime.preflight()
            boot_id = self.boot_id_provider()

            applications = discover_applications(self.project.application_dir)
            report.messages.append(f"discovered {len(applications)} application(s)")
            closure = resolve(applications)
            report.messages.append(
                f"resolved {len(closure.applications)} application runtime definition(s)"
            )

            state = self.state.load()
            references = self.references.load()
            generation_store = GenerationStore(self.project.rootfs_dir)

            self._observe_references(references, report)
            reclaimed = generation_store.reclaim_unreferenced(
                self._protected_generations(references) | self._storage_generations()
            )
            self._report_reclamation(report, reclaimed, phase="before image selection")

            selection = self.image_provider.ensure(self.project)
            report.messages.append(
                ("selected existing" if selection.reused else "selected")
                + f" rootfs generation {selection.generation[:16]}"
            )

            queued_boot_launches = self._queued_boot_launches(applications)
            for name in closure.applications:
                spec = applications[name]
                live = self._live_references(references, application=name)
                should_start = False
                if spec.start_policy == ApplicationStartPolicy.KEEP_RUNNING:
                    should_start = not live
                elif spec.start_policy == ApplicationStartPolicy.START_ONCE_PER_BOOT:
                    should_start = not live and not self._started_during_boot(
                        references, name, boot_id
                    )

                if should_start and name in queued_boot_launches:
                    should_start = False
                    report.messages.append(
                        f"{name}: queued explicit boot launch takes precedence over "
                        "automatic policy start"
                    )

                if should_start:
                    launched = self._launch(
                        spec,
                        selection=selection,
                        closure=closure,
                        references=references,
                        instance_id=new_application_instance_id(
                            spec.name,
                            multiple_instances=spec.multiple_instances,
                        ),
                        request_id=None,
                        replaces_runtime_id=None,
                        boot_id=boot_id,
                    )
                    report.messages.append(
                        f"{name}/{launched.runtime_id}: started {len(launched.programs)} "
                        "program service(s)"
                    )
                    if (
                        spec.start_policy == ApplicationStartPolicy.KEEP_RUNNING
                        and launched.state in _TERMINAL
                    ):
                        report.ok = False
                        report.errors.append(
                            f"{name}/{launched.runtime_id}: KEEP_RUNNING application "
                            "became terminal during launch"
                        )
                        report.faults.append(Fault("keep-running-terminal", report.errors[-1], runtime_ids=(launched.runtime_id,)))
                else:
                    report.messages.append(
                        f"{name}: {len(live)} live/unresolved application runtime(s); "
                        f"policy={spec.start_policy.value}"
                    )

            self._process_requests(
                applications=applications,
                closure=closure,
                references=references,
                selection=selection,
                boot_id=boot_id,
                report=report,
            )

            declared_names = set(applications)
            for reference in references.values():
                if (
                    reference.application not in declared_names
                    and reference.state in _NONTERMINAL
                ):
                    report.messages.append(
                        f"{reference.application}/{reference.runtime_id}: undeclared "
                        "application runtime remains observed and protects its generation"
                    )

            pruned = self.references.prune_completed(references)
            if pruned:
                report.messages.append(
                    f"pruned {len(pruned)} completed application-runtime history record(s)"
                )
            self.references.save(references)
            reclaimed = generation_store.reclaim_unreferenced(
                self._protected_generations(references) | self._storage_generations() | {selection.generation}
            )
            self._report_reclamation(report, reclaimed, phase="after reconciliation")

            from .application_software import ApplicationSoftwareReclaimer
            protected_views, protected_artifacts = self._protected_software(
                references, applications
            )
            eligible_views, eligible_artifacts = self._eligible_software_reclamation(
                references
            )
            reclaimed_views, reclaimed_artifacts = ApplicationSoftwareReclaimer(
                self.project
            ).reclaim_unreferenced(
                protected_views,
                protected_artifacts,
                eligible_views=eligible_views,
                eligible_artifacts=eligible_artifacts,
            )
            if reclaimed_views or reclaimed_artifacts:
                report.messages.append(
                    "reclaimed application software: "
                    f"{len(reclaimed_views)} view(s), {len(reclaimed_artifacts)} artifact(s)"
                )

            self._record_state(
                state,
                applications,
                closure,
                references,
                selection,
                boot_id=boot_id,
            )
            self.state.save(state)

        except PersistenceError:
            raise
        except BoxControlError as exc:
            report.ok = False
            report.errors.append(str(exc))
            report.faults.append(fault_from_exception(exc))
        return report

    def _loaded_application_context(self):
        self.project.require_state_allowed()
        self.systemd_runtime.preflight()
        applications = discover_applications(self.project.application_dir)
        closure = resolve(applications)
        references = self.references.load()
        self._observe_references(references, ReconcileReport(ok=True))
        selection = self.image_provider.ensure(self.project)
        return applications, closure, references, selection, self.boot_id_provider()

    def launch_application(self, application_name, *, request_id=None,
                           workspace_id=None, program_name=None):
        context = self._loaded_application_context()
        return self._launch_application_loaded(
            application_name,
            applications=context[0],
            closure=context[1],
            references=context[2],
            selection=context[3],
            boot_id=context[4],
            request_id=request_id,
            workspace_id=workspace_id,
            program_name=program_name,
        )

    def restart_application_runtime(self, runtime_id, *, request_id=None):
        context = self._loaded_application_context()
        return self._restart_application_runtime_loaded(
            runtime_id,
            applications=context[0],
            closure=context[1],
            references=context[2],
            selection=context[3],
            boot_id=context[4],
            request_id=request_id,
        )

    def terminate_application_runtime(self, runtime_id, *, request_id=None):
        self.project.require_state_allowed()
        self.systemd_runtime.preflight()
        references = self.references.load()
        try:
            result = self._terminate_application_runtime_loaded(
                runtime_id, references=references, request_id=request_id
            )
        except LifecycleOperationPending:
            self.references.save(references)
            raise
        self.references.prune_completed(references)
        self.references.save(references)
        return result
