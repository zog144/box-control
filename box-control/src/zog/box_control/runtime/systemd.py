from __future__ import annotations
from ..events import runtime_action

import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from functools import wraps

from ..errors import RuntimeOperationError, PersistenceError, RecoveryRequired
from ..durability import ensure_directory
from ..model import ApplicationRuntimeState, ApplicationSpec, ProgramSpec
from .reference import (
    ApplicationRuntimeReference,
    ProgramRuntimeReference,
    RuntimeReferenceStore,
    new_application_runtime_id,
)

_MINIMUM_SYSTEMD_VERSION = 250
_UNIT_COMPONENT = re.compile(r"^[A-Za-z0-9-]+$")
_ACTIVE_STATES = frozenset({"activating", "active", "deactivating", "reloading"})


class RuntimeIntegrityError(RuntimeOperationError):
    """Observed systemd state no longer matches the state Zog launched."""


def _bounded_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.transport.operation_budget(self.operation_timeout_seconds):
            return method(self, *args, **kwargs)
    return wrapped


def _expected_transient(observation):
    # systemd writes its own transient fragment; that path is not a persistent
    # administrator-supplied unit file. Only the exact system-manager path is allowed.
    return (observation.transient and not observation.drop_in_paths
            and observation.fragment_path in (
                "", f"/run/systemd/transient/{observation.unit_name}"))


def _component(value: str, *, label: str) -> str:
    if not value or not _UNIT_COMPONENT.fullmatch(value):
        raise RuntimeOperationError(
            f"{label} must contain only letters, digits, and hyphens: {value!r}"
        )
    return value


def project_unit_prefix(project) -> str:
    return f"zog-{_component(project.project_identity, label='project identity')}"


def application_slice_name(project, runtime_id: str) -> str:
    _component(runtime_id, label="application runtime identity")
    return f"{project_unit_prefix(project)}-{runtime_id}.slice"


def program_unit_name(project, runtime_id: str, program: str) -> str:
    _component(runtime_id, label="application runtime identity")
    _component(program, label="program name")
    return f"{project_unit_prefix(project)}-{runtime_id}-{program}.service"


@dataclass(frozen=True)
class SystemdServiceDefinition:
    """Normalized material state for one transient program service."""

    unit_name: str
    slice_name: str
    application_name: str
    program_name: str
    runtime_id: str
    command: tuple[str, ...]
    generation_root: Path
    root_directory: Path
    rootfs_bind_paths: tuple[tuple[Path, str], ...]
    bind_paths: tuple[tuple[Path, str], ...]
    read_only_bind_paths: tuple[tuple[Path, str], ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: str = "/"
    description: str | None = None
    user: str = "regular"
    group: str | None = None
    type: str = "exec"
    exit_type: str = "cgroup"
    kill_mode: str = "control-group"
    restart: str = "no"
    remain_after_exit: bool = False
    protect_system: str = "strict"
    private_tmp: bool = True
    private_mounts: bool = True
    mount_api_vfs: bool = True
    standard_output: str = "journal"
    standard_error: str = "journal"
    tasks_max: int = (2**64 - 1)
    timeout_stop_microseconds: int = 10_000_000
    execution_timeout_seconds: int | None = None
    workspace_binding: dict | None = None
    persistent_storage_id: str | None = None
    software_binding: dict | None = None
    execution_binding: dict | None = None
    host_bindings: tuple[dict, ...] = ()

    def mount_inventory(self):
        from ..mounts import inventory
        return inventory(
            self.generation_root,
            self.bind_paths,
            read_only_mounts=self.read_only_bind_paths,
            persistent_storage_id=self.persistent_storage_id,
            workspace_binding=self.workspace_binding,
            software_binding=self.software_binding,
            host_bindings=self.host_bindings,
        )

    def expected_properties(self) -> tuple[tuple[str, object], ...]:
        """Canonical values persisted and compared against live systemd state."""
        properties: list[tuple[str, object]] = [
            ("Type", self.type),
            ("ExitType", self.exit_type),
            ("KillMode", self.kill_mode),
            ("Restart", self.restart),
            ("RemainAfterExit", self.remain_after_exit),
            ("Slice", self.slice_name),
            ("RootDirectory", str(self.root_directory.resolve())),
            ("ExecStart", list(self.command)),
            ("Environment", [f"{key}={value}" for key, value in self.environment]),
            ("WorkingDirectory", self.working_directory),
            (
                "BindPaths",
                [[str(source.resolve()), target] for source, target in self.bind_paths],
            ),
            ("ProtectSystem", self.protect_system),
            ("PrivateTmp", self.private_tmp),
            ("PrivateMounts", self.private_mounts),
            ("MountAPIVFS", self.mount_api_vfs),
            ("StandardOutput", self.standard_output),
            ("StandardError", self.standard_error),
            ("User", self.user),
            ("TasksMax", self.tasks_max),
            ("TimeoutStopUSec", self.timeout_stop_microseconds),
        ]
        if self.workspace_binding:
            binding = self.workspace_binding
            properties.append(('NetworkNamespacePath', binding['namespace_path']))
        all_read_only = self.rootfs_bind_paths + self.read_only_bind_paths
        if all_read_only:
            properties.append(
                ("BindReadOnlyPaths", [
                    [str(source.resolve()), target]
                    for source, target in all_read_only
                ])
            )
        if self.execution_timeout_seconds is not None:
            properties.append(("RuntimeMaxUSec", self.execution_timeout_seconds * 1_000_000))
        if self.group is not None:
            properties.append(("Group", self.group))
        return tuple(properties)

    def to_transport_dict(self) -> dict:
        return {
            "mount_inventory": self.mount_inventory(),
            "workspace_binding": self.workspace_binding,
            "persistent_storage_id": self.persistent_storage_id,
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "unit_name": self.unit_name,
            "slice_name": self.slice_name,
            "application_name": self.application_name,
            "program_name": self.program_name,
            "runtime_id": self.runtime_id,
            "command": list(self.command),
            "generation_root": str(self.generation_root.resolve()),
            "root_directory": str(self.root_directory.resolve()),
            "rootfs_bind_paths": [
                [str(source.resolve()), target]
                for source, target in self.rootfs_bind_paths
            ],
            "bind_paths": [
                [str(source.resolve()), target] for source, target in self.bind_paths
            ],
            "read_only_bind_paths": [
                [str(source.resolve()), target]
                for source, target in self.read_only_bind_paths
            ],
            "software_binding": self.software_binding,
            "execution_binding": self.execution_binding,
            "host_bindings": [dict(item) for item in self.host_bindings],
            "environment": [[key, value] for key, value in self.environment],
            "working_directory": self.working_directory,
            "description": self.description,
            "user": self.user,
            "group": self.group,
            "type": self.type,
            "exit_type": self.exit_type,
            "kill_mode": self.kill_mode,
            "restart": self.restart,
            "remain_after_exit": self.remain_after_exit,
            "protect_system": self.protect_system,
            "private_tmp": self.private_tmp,
            "private_mounts": self.private_mounts,
            "mount_api_vfs": self.mount_api_vfs,
            "standard_output": self.standard_output,
            "standard_error": self.standard_error,
            "tasks_max": self.tasks_max,
            "timeout_stop_microseconds": self.timeout_stop_microseconds,
        }


@dataclass(frozen=True)
class SystemdApplicationDefinition:
    runtime_id: str
    application: str
    slice_name: str
    services: tuple[SystemdServiceDefinition, ...]


@dataclass(frozen=True)
class SystemdUnitObservation:
    unit_name: str
    exists: bool
    transient: bool = True
    fragment_path: str = ""
    drop_in_paths: tuple[str, ...] = ()
    invocation_id: str | None = None
    active_state: str = "inactive"
    sub_state: str = "dead"
    result: str | None = None
    main_pid: int | None = None
    control_group: str | None = None
    properties: tuple[tuple[str, object], ...] = ()

    @property
    def active(self) -> bool:
        return self.exists and self.active_state in _ACTIVE_STATES

    def property_map(self) -> dict[str, object]:
        return dict(self.properties)


class SystemdTransport(Protocol):
    def operation_budget(self, timeout_seconds: float): ...

    def version(self) -> int: ...

    def start_slice(self, *, project_root: Path, slice_name: str, description: str) -> None: ...

    def start_service(
        self,
        *,
        project_root: Path,
        generation: str,
        definition: SystemdServiceDefinition,
    ) -> None: ...

    def observe(self, *, project_root: Path, unit_name: str) -> SystemdUnitObservation: ...

    def kill(self, *, project_root: Path, unit_name: str, signal_number: int) -> None: ...

    def stop(self, *, project_root: Path, unit_name: str) -> None: ...

    def reset_failed(self, *, project_root: Path, unit_name: str) -> None: ...

    def release(self, *, project_root: Path, unit_name: str) -> None: ...

    def release_runtime_root(self, *, project_root: Path, runtime_id: str) -> None: ...


def service_definition(
    project,
    application: ApplicationSpec,
    runtime_id: str,
    spec: ProgramSpec,
    *,
    generation_root: Path,
) -> SystemdServiceDefinition:
    mounts = tuple(
        (
            (project.mounts_dir / "persistent" / application.storage_id / mount_name
             if application.persistent and application.storage_id else
             project.mounts_dir / application.name / runtime_id / spec.name / mount_name),
            target,
        )
        for mount_name, target in spec.mounts
    )
    from ..mounts import validate
    validate(generation_root, mounts, spec.command, require_targets=True)
    binding = application.workspace_binding
    if binding:
        from ..mounts import validate_workspace_targets
        validate_workspace_targets(generation_root)
    if binding and binding['role'] == 'desktop':
        mounts += ((Path(binding['x11_directory']), '/tmp/.X11-unix'), (Path(binding['access_directory']), '/run/zog-workspace'))

    from ..application_software import read_only_mounts, runtime_environment
    software_mounts = read_only_mounts(
        project, application.name, application.software_binding
    )
    read_only_mounts_value = software_mounts
    if binding and binding['role'] == 'client':
        read_only_mounts_value += (
            (Path(binding['access_directory']), '/run/zog-workspace'),
            (Path(binding['x11_directory']), '/tmp/.X11-unix'),
        )
    # Host capability sources are never selected by application specifications.
    # Only a privileged, external administrator policy grants them to a program.
    from ..host_bindings import resolve as resolve_host_bindings
    host_bindings = resolve_host_bindings(project.path, application.name, spec.name)
    read_only_mounts_value += tuple(
        (Path(item['source']), item['destination']) for item in host_bindings
    )
    slice_name = application_slice_name(project, runtime_id)
    environment = runtime_environment(application.software_binding, spec.environment)
    from .scaffold import rootfs_bind_paths, runtime_root_path
    rootfs_mounts = rootfs_bind_paths(
        generation_root,
        writable_destinations=tuple(target for _source, target in mounts),
    )

    # Material systemd mount properties must have exactly one effective mount
    # for each destination. Parent/child overlays are supported (for example
    # read-only /var plus writable /var/lib/app); exact duplicates are not.
    destinations = {}
    for access, values in (
        ("read-only rootfs", rootfs_mounts),
        ("read-only controller", read_only_mounts_value),
        ("writable", mounts),
    ):
        for _source, target in values:
            previous = destinations.setdefault(target, access)
            if previous != access:
                raise RuntimeOperationError(
                    f"conflicting runtime mount destination {target}: "
                    f"{previous} and {access}"
                )
            if sum(1 for _s, candidate in values if candidate == target) > 1:
                raise RuntimeOperationError(
                    f"duplicate runtime mount destination: {target}"
                )
    runtime_root = runtime_root_path(project, runtime_id)
    if application.execution_identity is not None:
        grant = application.execution_binding
        if (not isinstance(grant, dict)
                or grant.get("schema") != "zog-execution-identity-v1"
                or grant.get("project") != str(project.path)
                or grant.get("application") != application.name
                or spec.user != str(grant.get("uid"))
                or spec.group != str(grant.get("gid"))):
            raise RecoveryRequired('application execution identity is unbound or mismatched')
    if application.persistent and not application.storage_id:
        raise RecoveryRequired('persistent application has no bound storage identity')
    return SystemdServiceDefinition(
        unit_name=program_unit_name(project, runtime_id, spec.name),
        slice_name=slice_name,
        application_name=application.name,
        program_name=spec.name,
        runtime_id=runtime_id,
        command=spec.command,
        generation_root=generation_root,
        execution_timeout_seconds=spec.execution_timeout_seconds,
        persistent_storage_id=application.storage_id if application.persistent else None,
        workspace_binding=binding,
        root_directory=runtime_root,
        rootfs_bind_paths=rootfs_mounts,
        bind_paths=mounts,
        read_only_bind_paths=read_only_mounts_value,
        software_binding=application.software_binding,
        execution_binding=application.execution_binding,
        host_bindings=host_bindings,
        environment=environment,
        working_directory=spec.working_directory or "/",
        description=spec.description,
        user=spec.user,
        group=spec.group,
    )


def application_definition(
    project,
    application: ApplicationSpec,
    runtime_id: str,
    *,
    generation_root: Path,
) -> SystemdApplicationDefinition:
    return SystemdApplicationDefinition(
        runtime_id=runtime_id,
        application=application.name,
        slice_name=application_slice_name(project, runtime_id),
        services=tuple(
            service_definition(
                project,
                application,
                runtime_id,
                program,
                generation_root=generation_root,
            )
            for program in application.programs
        ),
    )


def _normalized_expected(
    observation: SystemdUnitObservation,
    requested: tuple[tuple[str, object], ...],
) -> tuple[tuple[str, object], ...]:
    observed = observation.property_map()
    missing = [name for name, _value in requested if name not in observed]
    if missing:
        raise RuntimeIntegrityError(
            f"{observation.unit_name}: systemd did not expose material properties: "
            + ", ".join(missing)
        )
    for name, requested_value in requested:
        if observed[name] != requested_value:
            raise RuntimeIntegrityError(
                f"{observation.unit_name}: systemd did not apply requested {name}: "
                f"requested {requested_value!r}, effective {observed[name]!r}"
            )
    return tuple((name, observed[name]) for name, _value in requested)


def _launch_acceptance_error(observation: SystemdUnitObservation) -> str | None:
    """Accept exec completion, including an observable successful short run.

    Invocation identity alone is insufficient: an activating service may not
    have executed, and a failed service may still have living descendants.
    """
    if not observation.exists or not observation.invocation_id:
        return "service did not become observable with an Invocation ID"
    if observation.result not in (None, "", "success"):
        return f"required program failed during launch: Result={observation.result}"
    if observation.active_state in ("active", "reloading"):
        return None
    if observation.active_state == "inactive" and observation.result == "success":
        return None
    return (
        "required program did not complete exec acceptance: "
        f"{observation.active_state}/{observation.sub_state}"
    )


class SystemdServiceRuntime:
    """Unified application/program lifecycle over transient systemd units."""

    def __init__(
        self,
        project,
        transport: SystemdTransport,
        *,
        stop_grace_seconds: float = 5.0,
        poll_interval_seconds: float = 0.1,
        operation_timeout_seconds: float = 120.0,
    ):
        self.project = project
        self.transport = transport
        self.stop_grace_seconds = stop_grace_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.operation_timeout_seconds = operation_timeout_seconds

    def preflight(self) -> None:
        version = self.transport.version()
        if version < _MINIMUM_SYSTEMD_VERSION:
            raise RuntimeOperationError(
                f"systemd {version} is unsupported; version {_MINIMUM_SYSTEMD_VERSION}+ "
                "is required for ExitType=cgroup"
            )

    def definition(
        self,
        application: ApplicationSpec,
        runtime_id: str,
        *,
        generation_root: Path,
    ) -> SystemdApplicationDefinition:
        return application_definition(
            self.project,
            application,
            runtime_id,
            generation_root=generation_root,
        )

    def _observe_program(self, reference: ProgramRuntimeReference) -> SystemdUnitObservation:
        observation = self.transport.observe(
            project_root=self.project.path,
            unit_name=reference.unit_name,
        )
        if not observation.exists:
            return observation
        if not _expected_transient(observation):
            raise RuntimeIntegrityError(
                f"{reference.unit_name}: expected a transient unit without persistent unit/drop-in files"
            )
        if reference.invocation_id is not None and observation.invocation_id != reference.invocation_id:
            raise RuntimeIntegrityError(
                f"{reference.unit_name}: invocation ID changed from "
                f"{reference.invocation_id!r} to {observation.invocation_id!r}"
            )
        expected = dict(reference.expected_properties)
        observed = observation.property_map()
        for name, value in expected.items():
            if name not in observed:
                raise RuntimeIntegrityError(
                    f"{reference.unit_name}: material systemd property {name} is unavailable"
                )
            if observed[name] != value:
                raise RuntimeIntegrityError(
                    f"{reference.unit_name}: systemd property {name} differs from launched state: "
                    f"expected {value!r}, observed {observed[name]!r}"
                )
        return observation

    def observe_reference(
        self, reference: ApplicationRuntimeReference
    ) -> ApplicationRuntimeReference:
        programs: list[ProgramRuntimeReference] = []
        any_active = False
        any_failed = False
        launch_error = None
        for program in reference.programs:
            observation = self._observe_program(program)
            if reference.state == ApplicationRuntimeState.STARTING:
                problem = _launch_acceptance_error(observation)
                if problem and launch_error is None:
                    launch_error = (
                        "launch transaction interrupted or failed before every required program executed: "
                        f"{program.unit_name}: {problem}"
                    )
            any_active = any_active or observation.active
            any_failed = any_failed or (
                observation.exists
                and observation.result not in (None, "", "success")
                and not observation.active
            )
            retained_failure = (reference.cleanup_pending
                and reference.state in (ApplicationRuntimeState.TERMINATING,
                    ApplicationRuntimeState.FAILED, ApplicationRuntimeState.TERMINATED)
                and program.result not in (None, "", "success"))
            programs.append(
                replace(
                    program,
                    invocation_id=(program.invocation_id or observation.invocation_id),
                    active_state=observation.active_state if observation.exists else "inactive",
                    sub_state=observation.sub_state if observation.exists else "dead",
                    result=(program.result if not observation.exists or retained_failure else observation.result),
                    main_pid=observation.main_pid,
                    control_group=observation.control_group,
                )
            )

        error = reference.error
        if launch_error:
            error = error or launch_error
            state = (
                ApplicationRuntimeState.TERMINATING
                if any_active
                else ApplicationRuntimeState.FAILED
            )
        elif any_active:
            state = (
                ApplicationRuntimeState.TERMINATING
                if reference.state == ApplicationRuntimeState.TERMINATING
                else ApplicationRuntimeState.RUNNING
            )
        elif any_failed or error or reference.state == ApplicationRuntimeState.FAILED:
            state = ApplicationRuntimeState.FAILED
        else:
            state = ApplicationRuntimeState.TERMINATED
        completed_at = reference.completed_at
        if state in (ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED) and completed_at is None:
            completed_at = time.time()
        return replace(
            reference,
            state=state,
            programs=tuple(programs),
            error=error,
            completed_at=completed_at,
        )

    def prepare(
        self,
        application: ApplicationSpec,
        *,
        instance_id: str,
        generation: str,
        generation_root: Path,
        references: dict[str, ApplicationRuntimeReference],
        store: RuntimeReferenceStore,
        application_fingerprint: str | None = None,
        request_id: str | None = None,
        replaces_runtime_id: str | None = None,
        boot_id: str | None = None,
    ) -> ApplicationRuntimeReference:
        if application.execution_identity is not None:
            verified = self.transport.resolve_execution_identity(
                project_root=self.project.path, application=application.name
            )
            if verified != application.execution_binding:
                raise RecoveryRequired(
                    "prepared application execution identity no longer matches protected grant"
                )
        runtime_id = new_application_runtime_id()
        while runtime_id in references or (
            self.project.mounts_dir / application.name / runtime_id
        ).exists():
            runtime_id = new_application_runtime_id()
        definition = self.definition(
            application, runtime_id, generation_root=generation_root
        )
        pending = ApplicationRuntimeReference(
            runtime_id=runtime_id,
            application=application.name,
            instance_id=instance_id,
            generation=generation,
            state=ApplicationRuntimeState.STARTING,
            workspace_binding=application.workspace_binding,
            application_fingerprint=application_fingerprint,
            execution_binding=application.execution_binding,
            software_view=(application.software_binding or {}).get("view"),
            software_binding_digest=(application.software_binding or {}).get("binding_digest"),
            software_manifest_digest=(application.software_binding or {}).get("view_manifest_digest"),
            software_artifacts=tuple(
                (application.software_binding or {}).get("reclamation_protection", {}).get("artifacts", ())
            ),
            software_mounts=tuple(
                (str(source.resolve()), target)
                for source, target in (
                    definition.services[0].read_only_bind_paths
                    if definition.services else ()
                )
                if target.startswith("/applications/")
            ),
            request_id=request_id,
            replaces_runtime_id=replaces_runtime_id,
            boot_id=boot_id,
            slice_name=definition.slice_name,
            created_at=time.time(),
            programs=tuple(
                ProgramRuntimeReference(
                    program=program.name,
                    unit_name=service.unit_name,
                    command=program.command,
                    description=program.description,
                    invocation_id=None,
                    expected_properties=service.expected_properties(),
                    mount_inventory=service.mount_inventory(),
                )
                for program, service in zip(
                    application.programs, definition.services, strict=True
                )
            ),
        )
        return pending

    @_bounded_operation
    def launch(
        self,
        application: ApplicationSpec,
        *,
        instance_id: str,
        generation: str,
        generation_root: Path,
        references: dict[str, ApplicationRuntimeReference],
        store: RuntimeReferenceStore,
        application_fingerprint: str | None = None,
        request_id: str | None = None,
        replaces_runtime_id: str | None = None,
        boot_id: str | None = None,
        prepared: ApplicationRuntimeReference | None = None,
        resumed_units: frozenset[str] = frozenset(),
    ) -> ApplicationRuntimeReference:
        self.preflight()
        pending = prepared or self.prepare(
            application, instance_id=instance_id, generation=generation,
            generation_root=generation_root, references=references, store=store,
            application_fingerprint=application_fingerprint, request_id=request_id,
            replaces_runtime_id=replaces_runtime_id, boot_id=boot_id,
        )
        runtime_id = pending.runtime_id
        definition = self.definition(application, runtime_id, generation_root=generation_root)
        if prepared is not None:
            if definition.slice_name != pending.slice_name or len(definition.services) != len(pending.programs):
                raise RecoveryRequired("prepared definition no longer matches the launch implementation")
            for service, program in zip(definition.services, pending.programs, strict=True):
                if program.mount_inventory is not None and service.mount_inventory() != program.mount_inventory:
                    raise RecoveryRequired("prepared mount inventory cannot be reproduced exactly")
                if (service.unit_name != program.unit_name or
                    (service.unit_name not in resumed_units and service.expected_properties() != program.expected_properties)):
                    raise RecoveryRequired("prepared service configuration cannot be reproduced exactly")
        references[runtime_id] = pending
        store.save(references)

        started_units: list[str] = []
        try:
            # Reserve this data namespace even after its history entry is pruned.
            ensure_directory(self.project.mounts_dir / application.name / runtime_id,
                             exist_ok=prepared is not None)
            self.transport.start_slice(
                project_root=self.project.path,
                slice_name=definition.slice_name,
                description=f"Zog application runtime {application.name}/{runtime_id}",
            )
            updated_programs: list[ProgramRuntimeReference] = []
            for program_ref, service in zip(pending.programs, definition.services, strict=True):
                for source, _target in service.bind_paths:
                    workspace_sources = set((application.workspace_binding or {}).get(k) for k in ('x11_directory', 'access_directory'))
                    if str(source) in workspace_sources:
                        if not source.is_dir():
                            raise RecoveryRequired('workspace input missing; cannot recreate it')
                    elif not application.persistent:
                        ensure_directory(source)
                # The transport may raise after systemd has accepted the start.
                started_units.append(service.unit_name)
                if service.unit_name not in resumed_units:
                    self.transport.start_service(
                        project_root=self.project.path,
                        generation=generation,
                        definition=service,
                    )
                observation = self.transport.observe(
                    project_root=self.project.path,
                    unit_name=service.unit_name,
                )
                if not observation.exists or not observation.invocation_id:
                    raise RuntimeOperationError(
                        f"{service.unit_name}: service did not become observable with an Invocation ID"
                    )
                if not _expected_transient(observation):
                    raise RuntimeIntegrityError(
                        f"{service.unit_name}: launched unit is not the expected transient unit"
                    )
                requested = service.expected_properties()
                effective = _normalized_expected(observation, requested)
                updated_programs.append(
                    replace(
                        program_ref,
                        invocation_id=observation.invocation_id,
                        expected_properties=effective,
                        active_state=observation.active_state,
                        sub_state=observation.sub_state,
                        result=observation.result,
                        main_pid=observation.main_pid,
                        control_group=observation.control_group,
                    )
                )
                # Persist each newly learned Invocation ID before starting the next
                # service so a mid-launch crash remains attributable.
                pending = replace(pending, programs=tuple(updated_programs) + pending.programs[len(updated_programs):])
                references[runtime_id] = pending
                store.save(references)
                problem = _launch_acceptance_error(observation)
                if problem:
                    raise RuntimeOperationError(f"{service.unit_name}: {problem}")

            launched = self.observe_reference(
                replace(pending, programs=tuple(updated_programs))
            )
            if launched.error or launched.state not in (
                ApplicationRuntimeState.RUNNING, ApplicationRuntimeState.TERMINATED
            ):
                raise RuntimeOperationError(launched.error or "launch transaction failed")
            references[runtime_id] = launched
            store.save(references)
            if launched.state == ApplicationRuntimeState.TERMINATED:
                launched = self.cleanup(launched, references=references, store=store)
            return launched
        except PersistenceError:
            raise
        except Exception as exc:
            for unit_name in reversed(started_units):
                try:
                    self.transport.kill(
                        project_root=self.project.path,
                        unit_name=unit_name,
                        signal_number=15,
                    )
                except PersistenceError:
                    raise
                except Exception:
                    pass
            # A failed launch attempt remains a first-class runtime-history
            # entry. If cleanup itself leaves a live service, mark the runtime
            # TERMINATING so its generation stays protected until observation
            # proves the cgroup empty.
            observed_programs: list[ProgramRuntimeReference] = []
            any_active = False
            for program in pending.programs:
                try:
                    observation = self.transport.observe(
                        project_root=self.project.path, unit_name=program.unit_name
                    )
                except PersistenceError:
                    raise
                except Exception:
                    # An unavailable observation cannot prove that cleanup
                    # emptied the cgroup. Keep the generation protected.
                    observation = SystemdUnitObservation(
                        unit_name=program.unit_name, exists=True,
                        active_state="deactivating", sub_state="unknown",
                        invocation_id=program.invocation_id,
                    )
                any_active = any_active or observation.active
                observed_programs.append(
                    replace(
                        program,
                        invocation_id=(program.invocation_id or observation.invocation_id),
                        active_state=(observation.active_state if observation.exists else "inactive"),
                        sub_state=(observation.sub_state if observation.exists else "dead"),
                        # Rollback can lose observation or see reset state after
                        # failure evidence was already committed to the store.
                        result=(program.result
                                if program.result not in (None, "", "success")
                                else observation.result or program.result),
                        main_pid=observation.main_pid,
                        control_group=observation.control_group,
                    )
                )
            failed_state = (
                ApplicationRuntimeState.TERMINATING
                if any_active
                else ApplicationRuntimeState.FAILED
            )
            failed = replace(
                pending,
                state=failed_state,
                programs=tuple(observed_programs),
                error=str(exc),
                completed_at=(time.time() if failed_state == ApplicationRuntimeState.FAILED else None),
            )
            references[runtime_id] = failed
            store.save(references)
            # Stop jobs cover starts accepted before their reply was lost.
            self.cleanup(failed, references=references, store=store)
            raise

    @_bounded_operation
    @runtime_action
    def cleanup(self, reference, *, references, store):
        """Retryable teardown; persist evidence before resetting or unloading.

        Terminal runtime outcome and cleanup progress are independent. A pending
        cleanup protects the generation and cannot be removed by history pruning.
        """
        if not reference.cleanup_pending:
            return reference
        current = reference
        problems = []
        for program in current.programs:
            try:
                observation = self._observe_program(program)
                # Capture exit evidence BEFORE StopUnit/ResetFailedUnit alter it.
                saved = replace(
                    program,
                    invocation_id=program.invocation_id or observation.invocation_id,
                    result=(program.result if program.result not in (None, "", "success")
                            else observation.result or program.result),
                    active_state=observation.active_state if observation.exists else "inactive",
                    sub_state=observation.sub_state if observation.exists else "dead",
                    main_pid=observation.main_pid,
                    control_group=observation.control_group or program.control_group,
                )
                current = replace(current, programs=tuple(
                    saved if item.unit_name == program.unit_name else item
                    for item in current.programs
                ))
                references[current.runtime_id] = current
                store.save(references)
                if observation.exists:
                    self.transport.stop(project_root=self.project.path, unit_name=program.unit_name)
                    after = self._observe_program(saved)
                    if after.active:
                        raise RuntimeOperationError(f"{program.unit_name}: stop has not emptied the service")
                    saved = replace(saved,
                        result=(saved.result if saved.result not in (None, "", "success")
                                else after.result or saved.result),
                        active_state=after.active_state if after.exists else "inactive",
                        sub_state=after.sub_state if after.exists else "dead", main_pid=after.main_pid)
                    current = replace(current, programs=tuple(
                        saved if item.unit_name == saved.unit_name else item for item in current.programs))
                    references[current.runtime_id] = current
                    store.save(references)
                    if after.exists and after.active_state == "failed":
                        self.transport.reset_failed(project_root=self.project.path, unit_name=program.unit_name)
                self.transport.release(project_root=self.project.path, unit_name=program.unit_name)
                after = self.transport.observe(project_root=self.project.path, unit_name=program.unit_name)
                if after.exists:
                    raise RuntimeOperationError(f"{program.unit_name}: awaiting unit unloading")
            except PersistenceError:
                raise
            except Exception as exc:
                problems.append(f"{program.unit_name}: {exc}")
        # Do not stop the application slice while member cleanup is unresolved.
        if not problems and current.slice_name:
            try:
                observation = self.transport.observe(project_root=self.project.path, unit_name=current.slice_name)
                if observation.exists:
                    if not _expected_transient(observation):
                        raise RuntimeIntegrityError("application slice is not the expected transient unit")
                    self.transport.stop(project_root=self.project.path, unit_name=current.slice_name)
                    observation = self.transport.observe(project_root=self.project.path, unit_name=current.slice_name)
                    if observation.active:
                        raise RuntimeOperationError("application slice remains active")
                    if observation.exists and observation.active_state == "failed":
                        self.transport.reset_failed(project_root=self.project.path, unit_name=current.slice_name)
                self.transport.release(project_root=self.project.path, unit_name=current.slice_name)
                if self.transport.observe(project_root=self.project.path, unit_name=current.slice_name).exists:
                    raise RuntimeOperationError("awaiting application slice unloading")
                self.transport.release_runtime_root(
                    project_root=self.project.path,
                    runtime_id=current.runtime_id,
                )
            except PersistenceError:
                raise
            except Exception as exc:
                problems.append(f"{current.slice_name}: {exc}")
        if not problems:
            current = replace(
                current,
                state=(ApplicationRuntimeState.FAILED if current.error or current.state == ApplicationRuntimeState.FAILED
                       else ApplicationRuntimeState.TERMINATED),
                completed_at=current.completed_at or time.time(),
            )
        current = replace(current, cleanup_pending=bool(problems),
                          cleanup_error="; ".join(problems) if problems else None)
        references[current.runtime_id] = current
        store.save(references)
        return current

    @_bounded_operation
    @runtime_action
    def terminate(
        self,
        reference: ApplicationRuntimeReference,
        *,
        references: dict[str, ApplicationRuntimeReference],
        store: RuntimeReferenceStore,
    ) -> ApplicationRuntimeReference:
        self.preflight()
        observed = self.observe_reference(reference)
        if observed.state in (ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED):
            references[reference.runtime_id] = observed
            store.save(references)
            return self.cleanup(observed, references=references, store=store)

        terminating = replace(observed, state=ApplicationRuntimeState.TERMINATING)
        references[reference.runtime_id] = terminating
        store.save(references)

        active_units = [
            program.unit_name
            for program in terminating.programs
            if program.active_state in _ACTIVE_STATES
        ]
        try:
            for unit_name in active_units:
                self.transport.kill(
                    project_root=self.project.path,
                    unit_name=unit_name,
                    signal_number=2,  # SIGINT
                )

            deadline = time.monotonic() + max(0.0, self.stop_grace_seconds)
            while active_units and time.monotonic() < deadline:
                time.sleep(self.poll_interval_seconds)
                still_active: list[str] = []
                for unit_name in active_units:
                    observation = self.transport.observe(
                        project_root=self.project.path,
                        unit_name=unit_name,
                    )
                    if observation.active:
                        still_active.append(unit_name)
                active_units = still_active

            for unit_name in active_units:
                self.transport.kill(
                    project_root=self.project.path,
                    unit_name=unit_name,
                    signal_number=15,  # SIGTERM
                )

        except RuntimeOperationError:
            # The unit may have exited between observation and signaling, or a
            # reply may have been lost. Cleanup determines the remaining work.
            pass

        return self.cleanup(terminating, references=references, store=store)
