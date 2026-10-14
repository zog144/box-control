from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from .errors import ConfigurationError, RuntimeOperationError
from .durability import ensure_directory, replace_json, remove_file


REQUEST_SCHEMA = 1
_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]+$")
_REQUEST_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class ApplicationRequestOperation(str, Enum):
    LAUNCH = "launch"
    RESTART = "restart"
    TERMINATE = "terminate"


class ApplicationRequestStatus(str, Enum):
    PENDING = "pending"
    SATISFIED = "satisfied"
    FAILED = "failed"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


def new_request_id(length: int = 12) -> str:
    return "".join(secrets.choice(_REQUEST_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class ApplicationRequest:
    request_id: str
    operation: ApplicationRequestOperation
    application: str | None = None
    runtime_id: str | None = None
    created_at: float = 0.0
    workspace_id: str | None = None


@dataclass(frozen=True)
class ApplicationRequestResult:
    request_id: str
    operation: ApplicationRequestOperation
    status: ApplicationRequestStatus
    application: str | None = None
    target_runtime_id: str | None = None
    runtime_id: str | None = None
    replaced_runtime_id: str | None = None
    error: str | None = None
    updated_at: float = 0.0
    workspace_id: str | None = None


def _validate_request(request: ApplicationRequest) -> None:
    if not _REQUEST_ID.fullmatch(request.request_id):
        raise ConfigurationError(
            "application request identity must contain only letters, digits, and hyphens"
        )
    if request.operation == ApplicationRequestOperation.LAUNCH:
        if not request.application or request.runtime_id is not None:
            raise ConfigurationError(
                "launch request requires application and must not specify runtime_id"
            )
    elif not request.runtime_id or request.application is not None:
        raise ConfigurationError(
            f"{request.operation.value} request requires runtime_id and must not specify application"
        )


class ApplicationRequestStore:
    """Atomic one-file-per-request queue and durable request outcomes."""

    def __init__(self, request_dir: Path, result_dir: Path):
        self.request_dir = request_dir
        self.result_dir = result_dir

    def submit(self, request: ApplicationRequest) -> Path:
        _validate_request(request)
        ensure_directory(self.request_dir)
        path = self.request_dir / f"{request.request_id}.json"
        prior = self.result(request.request_id)
        if prior is not None:
            same_target = (
                prior.application == request.application
                if request.operation == ApplicationRequestOperation.LAUNCH
                else prior.target_runtime_id == request.runtime_id
            )
            if prior.operation == request.operation and same_target and prior.workspace_id == request.workspace_id:
                return self.result_dir / f"{request.request_id}.json"
            raise RuntimeOperationError(
                f"application request identity is already used: {request.request_id}"
            )
        payload = {
            "schema": REQUEST_SCHEMA,
            "request_id": request.request_id,
            "operation": request.operation.value,
            "application": request.application,
            "runtime_id": request.runtime_id,
            "workspace_id": request.workspace_id,
            "created_at": request.created_at or time.time(),
        }
        if path.exists():
            existing = self.load(path)
            if (
                existing.operation == request.operation
                and existing.application == request.application
                and existing.runtime_id == request.runtime_id
                and existing.workspace_id == request.workspace_id
            ):
                return path
            raise RuntimeOperationError(
                f"application request identity is already used: {request.request_id}"
            )
        self._write_new(path, payload)
        return path

    def paths(self) -> tuple[Path, ...]:
        if not self.request_dir.is_dir():
            return ()
        return tuple(sorted(self.request_dir.glob("*.json")))

    def load(self, path: Path) -> ApplicationRequest:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema") != REQUEST_SCHEMA:
                raise ValueError("unsupported request schema")
            request = ApplicationRequest(
                request_id=str(raw["request_id"]),
                operation=ApplicationRequestOperation(str(raw["operation"])),
                application=(str(raw["application"]) if raw.get("application") else None),
                runtime_id=(str(raw["runtime_id"]) if raw.get("runtime_id") else None),
                workspace_id=raw.get("workspace_id"),
                created_at=float(raw.get("created_at", 0.0)),
            )
            if path.stem != request.request_id:
                raise ValueError("request filename does not match request_id")
            _validate_request(request)
            return request
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"cannot read application request {path}: {exc}") from exc

    def result(self, request_id: str) -> ApplicationRequestResult | None:
        path = self.result_dir / f"{request_id}.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema") != REQUEST_SCHEMA:
                raise ValueError("unsupported request-result schema")
            result = ApplicationRequestResult(
                request_id=str(raw["request_id"]),
                operation=ApplicationRequestOperation(str(raw["operation"])),
                status=ApplicationRequestStatus(str(raw["status"])),
                application=raw.get("application"),
                target_runtime_id=raw.get("target_runtime_id"),
                runtime_id=raw.get("runtime_id"),
                replaced_runtime_id=raw.get("replaced_runtime_id"),
                error=raw.get("error"),
                workspace_id=raw.get("workspace_id"),
                updated_at=float(raw.get("updated_at", 0.0)),
            )
            if result.request_id != request_id:
                raise ValueError("request-result filename does not match request_id")
            return result
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeOperationError(
                f"cannot read application request result {path}: {exc}"
            ) from exc

    def save_result(self, result: ApplicationRequestResult) -> None:
        ensure_directory(self.result_dir)
        path = self.result_dir / f"{result.request_id}.json"
        payload = asdict(result)
        payload["schema"] = REQUEST_SCHEMA
        payload["operation"] = result.operation.value
        payload["status"] = result.status.value
        payload["updated_at"] = result.updated_at or time.time()
        self._replace(path, payload)

    @staticmethod
    def remove(path: Path) -> None:
        remove_file(path)

    @staticmethod
    def _write_new(path: Path, payload: dict) -> None:
        # Public submission holds ProjectLock through identity checks and save.
        replace_json(path, payload)

    @staticmethod
    def _replace(path: Path, payload: dict) -> None:
        replace_json(path, payload)
