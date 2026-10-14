from __future__ import annotations

from .diagnostics import Fault
from dataclasses import dataclass, field
from enum import Enum


class RuntimeState(str, Enum):
    ABSENT = "absent"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class ApplicationRuntimeState(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


class ApplicationStartPolicy(str, Enum):
    """When PROJECT_EVALUATION may launch an application automatically."""

    START_ONCE_PER_BOOT = "start-once-per-boot"
    KEEP_RUNNING = "keep-running"
    EXTERNALLY_CONTROLLED = "externally-controlled"


@dataclass(frozen=True)
class ProgramSpec:
    """One runnable command/service inside an application.

    Program no longer means a build unit. One ProgramSpec maps to one systemd
    service inside an ApplicationRuntime.
    """

    name: str
    command: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    working_directory: str | None = None
    mounts: tuple[tuple[str, str], ...] = ()
    user: str = "regular"
    group: str | None = None
    description: str | None = None

    execution_timeout_seconds: int | None = None

    def env_dict(self) -> dict[str, str]:
        return dict(self.environment)


@dataclass(frozen=True)
class ApplicationSpec:
    """Runtime composition reconciled by box-control."""

    name: str
    programs: tuple[ProgramSpec, ...]
    dependencies: tuple[str, ...] = ()
    start_policy: ApplicationStartPolicy = ApplicationStartPolicy.EXTERNALLY_CONTROLLED
    multiple_instances: bool = False
    description: str | None = None

    persistent: bool = False
    writable_mounts: tuple[str, ...] = ()
    preparation_revision: str | None = None
    preparation: tuple[ProgramSpec, ...] = ()
    workspace_role: str | None = None
    workspace_binding: dict | None = None
    software_view: str | None = None
    software_binding: dict | None = None  # Bound by controller, never caller declaration.
    storage_id: str | None = None  # Bound by controller, never caller declaration.
    execution_identity: str | None = None  # Symbolic 'application', not a caller-selected UID.
    execution_binding: dict | None = None  # Privileged immutable grant bound before preparation.

    def program(self, name: str) -> ProgramSpec:
        for program in self.programs:
            if program.name == name:
                return program
        raise KeyError(name)


@dataclass(frozen=True)
class ProgramRuntime:
    application_runtime_id: str
    program: str
    unit_name: str
    command: tuple[str, ...] = ()
    state: RuntimeState = RuntimeState.ABSENT
    invocation_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ApplicationRuntime:
    runtime_id: str
    application: str
    instance_id: str
    generation: str
    state: ApplicationRuntimeState
    programs: tuple[ProgramRuntime, ...] = ()
    request_id: str | None = None
    replaces_runtime_id: str | None = None
    boot_id: str | None = None
    error: str | None = None


@dataclass
class ReconcileReport:
    ok: bool
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    faults: list[Fault] = field(default_factory=list)

    def summary(self) -> str:
        lines = ["box-control reconciliation: " + ("OK" if self.ok else "FAILED")]
        lines.extend("  " + message for message in self.messages)
        lines.extend("  ERROR: " + error for error in self.errors)
        return "\n".join(lines)
