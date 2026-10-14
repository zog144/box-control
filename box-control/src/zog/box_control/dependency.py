from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .errors import DependencyError
from .model import ApplicationSpec


@dataclass(frozen=True)
class ApplicationResolution:
    name: str
    dependencies: tuple[str, ...]
    dependency_closure_applications: tuple[str, ...]
    runtime_fingerprint: str


@dataclass(frozen=True)
class DependencyGraph:
    dependencies: tuple[tuple[str, tuple[str, ...]], ...]
    reverse_dependencies: tuple[tuple[str, tuple[str, ...]], ...]

    def dependencies_for(self, name: str) -> tuple[str, ...]:
        return dict(self.dependencies).get(name, ())

    def reverse_dependencies_for(self, name: str) -> tuple[str, ...]:
        return dict(self.reverse_dependencies).get(name, ())


@dataclass(frozen=True)
class DependencyClosure:
    applications: tuple[str, ...]
    graph: DependencyGraph
    resolutions: tuple[ApplicationResolution, ...]

    def resolution_for(self, name: str) -> ApplicationResolution:
        for resolution in self.resolutions:
            if resolution.name == name:
                return resolution
        raise KeyError(name)


def _program_payload(program) -> dict:
    return {
        "name": program.name,
        "command": list(program.command),
        "execution_timeout_seconds": program.execution_timeout_seconds,
        "environment": [list(item) for item in program.environment],
        "working_directory": program.working_directory,
        "mounts": [list(item) for item in program.mounts],
        "user": program.user,
        "group": program.group,
        "description": program.description,
    }


def _runtime_payload(spec: ApplicationSpec) -> dict:
    return {
        "name": spec.name,
        "dependencies": list(spec.dependencies),
        "start_policy": spec.start_policy.value,
        "multiple_instances": spec.multiple_instances,
        "workspace_role": spec.workspace_role,
        "persistent": spec.persistent,
        "writable_mounts": spec.writable_mounts,
        "preparation_revision": spec.preparation_revision,
        "preparation": [_program_payload(step) for step in spec.preparation],
        "description": spec.description,
        "programs": [_program_payload(program) for program in spec.programs],
    }


def _fingerprint(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def resolve(specs: dict[str, ApplicationSpec]) -> DependencyClosure:
    """Validate declarations and order evaluation; do not require live dependencies.

    Each application's start policy still controls its own automatic launch.
    These edges neither auto-start externally-controlled applications nor impose
    runtime availability, readiness, or cascading-stop requirements.
    """
    dependency_edges = {
        name: tuple(sorted(set(spec.dependencies))) for name, spec in specs.items()
    }
    reverse: dict[str, set[str]] = {name: set() for name in specs}
    for name in sorted(specs):
        for dependency in dependency_edges[name]:
            if dependency not in specs:
                raise DependencyError(f"application dependency not found: {dependency}")
            reverse[dependency].add(name)

    ordered: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise DependencyError(f"application dependency cycle detected at {name}")
        if name in visited:
            return
        if name not in specs:
            raise DependencyError(f"application dependency not found: {name}")
        visiting.add(name)
        for dependency in dependency_edges[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in sorted(specs):
        visit(name)

    closure: dict[str, tuple[str, ...]] = {}
    for name in ordered:
        applications = {name}
        for dependency in dependency_edges[name]:
            applications.update(closure[dependency])
        closure[name] = tuple(sorted(applications))

    graph = DependencyGraph(
        dependencies=tuple((name, dependency_edges[name]) for name in sorted(specs)),
        reverse_dependencies=tuple(
            (name, tuple(sorted(reverse[name]))) for name in sorted(specs)
        ),
    )
    resolutions = tuple(
        ApplicationResolution(
            name=name,
            dependencies=dependency_edges[name],
            dependency_closure_applications=closure[name],
            runtime_fingerprint=_fingerprint(_runtime_payload(specs[name])),
        )
        for name in ordered
    )
    return DependencyClosure(tuple(ordered), graph, resolutions)
