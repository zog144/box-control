from __future__ import annotations

import json
import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..errors import RuntimeOperationError
from ..durability import replace_json
from ..model import ApplicationRuntimeState

_RUNTIME_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERENCE_SCHEMA = 5
SUPPORTED_REFERENCE_SCHEMAS = frozenset({2, 3, 4, REFERENCE_SCHEMA})
COMPLETED_HISTORY_LIMIT = 25


def new_application_runtime_id(length: int = 6) -> str:
    return "".join(secrets.choice(_RUNTIME_ALPHABET) for _ in range(length))


def _stable_suffix(value: str, length: int = 6) -> str:
    number = int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest(), "big")
    characters = []
    for _index in range(length):
        number, offset = divmod(number, len(_RUNTIME_ALPHABET))
        characters.append(_RUNTIME_ALPHABET[offset])
    return "".join(characters)


def new_application_instance_id(
    application: str,
    *,
    multiple_instances: bool,
    request_id: str | None = None,
) -> str:
    """Return the stable singleton identity or a unique multi-instance identity."""
    if not multiple_instances:
        return application
    suffix = (
        _stable_suffix(request_id)
        if request_id is not None
        else "".join(secrets.choice(_RUNTIME_ALPHABET) for _ in range(6))
    )
    return f"{application}-{suffix}"


@dataclass(frozen=True)
class ProgramRuntimeReference:
    program: str
    unit_name: str
    command: tuple[str, ...] = ()
    description: str | None = None
    invocation_id: str | None = None
    expected_properties: tuple[tuple[str, object], ...] = ()
    active_state: str | None = None
    sub_state: str | None = None
    result: str | None = None
    main_pid: int | None = None
    control_group: str | None = None
    mount_inventory: dict | None = None


@dataclass(frozen=True)
class ApplicationRuntimeReference:
    runtime_id: str
    application: str
    instance_id: str
    generation: str
    state: ApplicationRuntimeState
    application_fingerprint: str | None = None
    software_view: str | None = None
    software_binding_digest: str | None = None
    software_manifest_digest: str | None = None
    software_artifacts: tuple[str, ...] = ()
    software_mounts: tuple[tuple[str, str], ...] = ()
    execution_binding: dict | None = None
    request_id: str | None = None
    replaces_runtime_id: str | None = None
    boot_id: str | None = None
    slice_name: str = ""
    programs: tuple[ProgramRuntimeReference, ...] = ()
    error: str | None = None
    created_at: float = 0.0
    completed_at: float | None = None
    workspace_binding: dict | None = None
    purpose: str = "application"
    cleanup_pending: bool = True
    cleanup_error: str | None = None


class RuntimeReferenceStore:
    """Atomic expected-launched records keyed by application runtime identity."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, ApplicationRuntimeReference]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeOperationError(
                f"cannot read runtime reference store {self.path}: {exc}"
            ) from exc
        return self.decode(raw)

    @staticmethod
    def decode(raw):
        if not isinstance(raw, dict) or raw.get("schema") not in SUPPORTED_REFERENCE_SCHEMAS:
            raise RuntimeOperationError(
                "runtime reference store uses an unsupported runtime schema"
            )
        runtimes = raw.get("runtimes", {})
        if not isinstance(runtimes, dict):
            raise RuntimeOperationError("runtime reference store runtimes must be an object")

        result: dict[str, ApplicationRuntimeReference] = {}
        try:
            for runtime_id, item in runtimes.items():
                programs = tuple(
                    ProgramRuntimeReference(
                        program=str(program["program"]),
                        unit_name=str(program["unit_name"]),
                        command=tuple(
                            str(value) for value in program.get(
                                "command",
                                dict(program.get("expected_properties", ())).get(
                                    "ExecStart", ()
                                ),
                            )
                        ),
                        description=program.get("description"),
                        invocation_id=(
                            str(program["invocation_id"])
                            if program.get("invocation_id") is not None
                            else None
                        ),
                        expected_properties=tuple(
                            (str(pair[0]), pair[1])
                            for pair in program.get("expected_properties", ())
                        ),
                        active_state=program.get("active_state"),
                        sub_state=program.get("sub_state"),
                        result=program.get("result"),
                        main_pid=(
                            int(program["main_pid"])
                            if program.get("main_pid") is not None
                            else None
                        ),
                        control_group=program.get("control_group"),
                        mount_inventory=program.get("mount_inventory"),
                    )
                    for program in item.get("programs", ())
                )
                reference = ApplicationRuntimeReference(
                    runtime_id=str(item["runtime_id"]),
                    application=str(item["application"]),
                    instance_id=str(item.get("instance_id", item["application"])),
                    generation=str(item["generation"]),
                    state=ApplicationRuntimeState(str(item["state"])),
                    application_fingerprint=item.get("application_fingerprint"),
                    software_view=item.get("software_view"),
                    software_binding_digest=item.get("software_binding_digest"),
                    software_manifest_digest=item.get("software_manifest_digest"),
                    software_artifacts=tuple(str(value) for value in item.get("software_artifacts", ())),
                    software_mounts=tuple(
                        (str(pair[0]), str(pair[1])) for pair in item.get("software_mounts", ())
                    ),
                    execution_binding=item.get("execution_binding"),
                    request_id=item.get("request_id"),
                    replaces_runtime_id=item.get("replaces_runtime_id"),
                    boot_id=item.get("boot_id"),
                    slice_name=str(item.get("slice_name", "")),
                    programs=programs,
                    error=item.get("error"),
                    workspace_binding=item.get("workspace_binding"),
                    purpose=item.get("purpose", "application"),
                    cleanup_pending=bool(item.get("cleanup_pending", True)),
                    cleanup_error=item.get("cleanup_error"),
                    created_at=float(item.get("created_at", 0.0)),
                    completed_at=(
                        float(item["completed_at"])
                        if item.get("completed_at") is not None
                        else None
                    ),
                )
                if reference.purpose not in {'application', 'preparation'}:
                    raise ValueError('invalid runtime purpose')
                if reference.runtime_id != runtime_id:
                    raise RuntimeOperationError(
                        f"runtime reference key/id mismatch: {runtime_id!r}"
                    )
                result[runtime_id] = reference
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RuntimeOperationError(f"invalid runtime reference store: {exc}") from exc
        return result

    @staticmethod
    def ordered(
        references: dict[str, ApplicationRuntimeReference],
        *,
        application: str | None = None,
        instance_id: str | None = None,
    ) -> tuple[ApplicationRuntimeReference, ...]:
        selected = (
            reference
            for reference in references.values()
            if application is None or reference.application == application
            if instance_id is None or reference.instance_id == instance_id
        )
        return tuple(
            sorted(selected, key=lambda item: (item.created_at, item.runtime_id), reverse=True)
        )

    @staticmethod
    def launch_candidates(references, *, application=None, instance_id=None):
        """Oldest-first runtimes requiring resolution before replacement.

        Terminal outcomes remain candidates while cleanup is pending.
        """
        terminal = {ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED}
        return tuple(
            sorted(
                (
                    reference
                    for reference in references.values()
                    if reference.purpose == "application"
                    and (reference.state not in terminal or reference.cleanup_pending)
                    and (application is None or reference.application == application)
                    and (instance_id is None or reference.instance_id == instance_id)
                ),
                key=lambda item: (item.created_at, item.runtime_id),
            )
        )

    @classmethod
    def current(
        cls,
        references: dict[str, ApplicationRuntimeReference],
        *,
        application: str | None = None,
        instance_id: str | None = None,
    ) -> tuple[ApplicationRuntimeReference, ...]:
        terminal = {ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED}
        return tuple(
            reference
            for reference in cls.ordered(
                references, application=application, instance_id=instance_id
            )
            if reference.purpose == "application" and reference.state not in terminal
        )

    @classmethod
    def history(
        cls,
        references: dict[str, ApplicationRuntimeReference],
        *,
        application: str | None = None,
        instance_id: str | None = None,
        limit: int | None = COMPLETED_HISTORY_LIMIT,
    ) -> tuple[ApplicationRuntimeReference, ...]:
        terminal = {ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED}
        result = tuple(
            sorted(
                (
                    reference
                    for reference in references.values()
                    if reference.state in terminal
                    and (
                        application is None or reference.application == application
                    )
                    and (instance_id is None or reference.instance_id == instance_id)
                ),
                key=lambda item: (
                    item.completed_at
                    if item.completed_at is not None
                    else item.created_at,
                    item.runtime_id,
                ),
                reverse=True,
            )
        )
        return result if limit is None else result[: max(0, limit)]

    @staticmethod
    def for_request(
        references: dict[str, ApplicationRuntimeReference], request_id: str
    ) -> ApplicationRuntimeReference | None:
        matches = [
            reference
            for reference in references.values()
            if reference.request_id == request_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: (item.created_at, item.runtime_id))

    def prune_completed(
        self,
        references: dict[str, ApplicationRuntimeReference],
        *,
        limit: int = COMPLETED_HISTORY_LIMIT,
    ) -> tuple[str, ...]:
        """Prune only Zog-owned completed runtime-history records.

        External evidence such as journald remains entirely outside this store
        and is never touched by runtime-history retention.
        """
        completed = [
            reference
            for reference in references.values()
            if reference.purpose != "preparation" and not reference.cleanup_pending and reference.state in (
                ApplicationRuntimeState.TERMINATED, ApplicationRuntimeState.FAILED
            )
        ]
        completed.sort(
            key=lambda item: (
                item.completed_at if item.completed_at is not None else item.created_at,
                item.runtime_id,
            )
        )
        remove = completed[:-limit] if limit > 0 else completed
        removed = tuple(reference.runtime_id for reference in remove)
        for runtime_id in removed:
            references.pop(runtime_id, None)
        return removed

    @staticmethod
    def encode(references):
        runtimes = {}
        for runtime_id, reference in sorted(references.items()):
            if runtime_id != reference.runtime_id:
                raise RuntimeOperationError(
                    f"runtime reference key/id mismatch: {runtime_id!r}"
                )
            runtimes[runtime_id] = {
                "runtime_id": reference.runtime_id,
                "application": reference.application,
                "instance_id": reference.instance_id,
                "generation": reference.generation,
                "state": reference.state.value,
                "application_fingerprint": reference.application_fingerprint,
                "software_view": reference.software_view,
                "software_binding_digest": reference.software_binding_digest,
                "software_manifest_digest": reference.software_manifest_digest,
                "software_artifacts": list(reference.software_artifacts),
                "software_mounts": [list(pair) for pair in reference.software_mounts],
                "execution_binding": reference.execution_binding,
                "request_id": reference.request_id,
                "replaces_runtime_id": reference.replaces_runtime_id,
                "boot_id": reference.boot_id,
                "slice_name": reference.slice_name,
                "error": reference.error,
                "created_at": reference.created_at,
                "completed_at": reference.completed_at,
                "workspace_binding": reference.workspace_binding,
                "purpose": reference.purpose,
                "cleanup_pending": reference.cleanup_pending,
                "cleanup_error": reference.cleanup_error,
                "programs": [
                    {
                        "program": program.program,
                        "unit_name": program.unit_name,
                        "command": list(program.command),
                        "description": program.description,
                        "invocation_id": program.invocation_id,
                        "expected_properties": [
                            [name, value] for name, value in program.expected_properties
                        ],
                        "active_state": program.active_state,
                        "sub_state": program.sub_state,
                        "result": program.result,
                        "main_pid": program.main_pid,
                        "control_group": program.control_group,
                        "mount_inventory": program.mount_inventory,
                    }
                    for program in reference.programs
                ],
            }
        raw = {"schema": REFERENCE_SCHEMA, "runtimes": runtimes}
        return raw

    def save(self, references: dict[str, ApplicationRuntimeReference]) -> None:
        replace_json(self.path, self.encode(references))
