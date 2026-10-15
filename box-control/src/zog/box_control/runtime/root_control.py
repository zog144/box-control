from __future__ import annotations

import socket
from contextvars import ContextVar
import time
from contextlib import contextmanager
from pathlib import Path

from ..errors import RuntimeOperationError, PersistenceError, RootControlReplyTimeout
from .systemd import SystemdServiceDefinition, SystemdUnitObservation
from ..root_protocol import decode, encode

DEFAULT_ROOT_CONTROL_SOCKET = Path("/run/zog/root-control.sock")


class RootControlSystemdTransport:
    """Unix-socket client for root-control's narrow systemd mechanism API."""

    def __init__(self, socket_path: Path = DEFAULT_ROOT_CONTROL_SOCKET, *, timeout_seconds=45.0):
        self.socket_path = Path(socket_path)
        self.timeout_seconds = timeout_seconds
        self._deadline = ContextVar("root_control_deadline", default=None)

    @property
    def operation_deadline(self):
        return self._deadline.get()

    @operation_deadline.setter
    def operation_deadline(self, value):
        self._deadline.set(value)

    def application_processes(self, *, project_root, runtime_id, program, limit=50):
        with self.operation_budget(10):
            return self._request("application_processes", project_root=str(project_root),
                                 runtime_id=runtime_id, program=program, limit=limit)

    def application_runtime_credentials(self, *, project_root, runtime_id, limit=50):
        with self.operation_budget(10):
            return self._request(
                "application_runtime_credentials",
                project_root=str(project_root), runtime_id=runtime_id, limit=limit,
            )

    def issuer_observe(self, *, project_root, request_id):
        with self.operation_budget(10):
            return self._request(
                "issuer_observe", project_root=str(project_root), request_id=request_id,
            )

    def application_runtime_mounts(self, *, project_root, runtime_id):
        with self.operation_budget(10):
            return self._request(
                "application_runtime_mounts",
                project_root=str(project_root),
                runtime_id=runtime_id,
            )

    def build_job_processes(self, *, project_root, job_id, limit=50):
        with self.operation_budget(10):
            return self._request("build_job_processes", project_root=str(project_root),
                                 job_id=job_id, limit=limit)

    def build_job_logs(self, *, project_root, job_id, cursor=None, limit=50):
        with self.operation_budget(10):
            return self._request("build_job_logs", project_root=str(project_root),
                                 job_id=job_id, cursor=cursor, limit=limit)

    def application_logs(self, *, project_root, runtime_id, program, cursor=None, limit=50):
        with self.operation_budget(10):
            return self._request("application_logs", project_root=str(project_root),
                                 runtime_id=runtime_id, program=program, cursor=cursor, limit=limit)

    @contextmanager
    def operation_budget(self, timeout_seconds):
        previous = self.operation_deadline
        deadline = time.monotonic() + timeout_seconds
        self.operation_deadline = min(previous, deadline) if previous is not None else deadline
        try:
            yield
        finally:
            self.operation_deadline = previous

    def _request(self, operation: str, **payload):
        timeout = self.timeout_seconds
        if self.operation_deadline is not None:
            timeout = min(timeout, self.operation_deadline - time.monotonic())
        if timeout <= 0:
            raise RuntimeOperationError("lifecycle operation deadline exceeded; outcome may be unresolved")
        request = {"operation": operation, **payload}
        started = time.monotonic()
        phase = "connect"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                deadline = time.monotonic() + timeout
                client.settimeout(timeout)
                client.connect(str(self.socket_path))
                client.settimeout(max(0.001, deadline - time.monotonic()))
                phase = "send"
                client.sendall(encode(request))
                phase = "reply"
                response = decode(client, deadline=deadline)
        except (OSError, ValueError) as exc:
            if isinstance(exc, TimeoutError) and phase == "reply":
                raise RootControlReplyTimeout(operation, time.monotonic() - started) from exc
            raise RuntimeOperationError(
                f"root-control {operation} {phase} failure at {self.socket_path}: {exc}; "
                + ("outcome may be unresolved" if phase != "connect" else "request not sent")
            ) from exc
        if not isinstance(response, dict) or not response.get("ok"):
            error = response.get("error", "invalid root-control response") if isinstance(response, dict) else "invalid root-control response"
            if isinstance(response, dict) and response.get("storage_failure"):
                raise PersistenceError(str(error))
            if isinstance(response, dict) and isinstance(response.get('code'), str):
                from ..workspaces import WorkspaceError
                raise WorkspaceError(response['code'], str(error))
            raise RuntimeOperationError(str(error))
        return response.get("result")

    def resolve_execution_identity(self, *, project_root, application):
        """Resolve a privileged administrator-issued identity; never provision one."""
        return self._request(
            'execution_identity_resolve', project_root=str(project_root),
            application=application,
        )

    def sync_application_storage(self, *, project_root, application, storage_id):
        return self._request('application_storage_sync', project_root=str(project_root),
                             application=application, storage_id=storage_id)

    def delete_application_storage(self, *, project_root, application, storage_id):
        return self._request('application_storage_delete', project_root=str(project_root),
                             application=application, storage_id=storage_id)

    def workspace_call(self, operation, *, project_root, **arguments):
        with self.operation_budget(10):
            return self._request('workspace_' + operation, project_root=str(project_root), **arguments)

    def build_registration_status(self, *, project_root, resource_id):
        with self.operation_budget(10):
            return self._request('build_registration_status', project_root=str(project_root), resource_id=resource_id)

    def build_call(self, operation, *, project_root, **kwargs):
        return self._request('build_' + operation, project_root=str(project_root), **kwargs)

    def version(self) -> int:
        return int(self._request("systemd_version"))

    def start_slice(self, *, project_root: Path, slice_name: str, description: str) -> None:
        self._request(
            "systemd_start_slice",
            project_root=str(project_root),
            slice_name=slice_name,
            description=description,
        )

    def start_service(
        self,
        *,
        project_root: Path,
        generation: str,
        definition: SystemdServiceDefinition,
    ) -> None:
        self._request(
            "systemd_start_service",
            project_root=str(project_root),
            generation=generation,
            definition=definition.to_transport_dict(),
        )

    def observe(self, *, project_root: Path, unit_name: str) -> SystemdUnitObservation:
        raw = self._request(
            "systemd_observe",
            project_root=str(project_root),
            unit_name=unit_name,
        )
        if not raw.get("exists", False):
            return SystemdUnitObservation(unit_name=unit_name, exists=False)
        return SystemdUnitObservation(
            unit_name=unit_name,
            exists=True,
            transient=bool(raw.get("transient", False)),
            fragment_path=str(raw.get("fragment_path", "")),
            drop_in_paths=tuple(str(item) for item in raw.get("drop_in_paths", ())),
            invocation_id=(str(raw["invocation_id"]) if raw.get("invocation_id") else None),
            active_state=str(raw.get("active_state", "inactive")),
            sub_state=str(raw.get("sub_state", "dead")),
            result=(str(raw["result"]) if raw.get("result") is not None else None),
            main_pid=(int(raw["main_pid"]) if raw.get("main_pid") else None),
            control_group=(str(raw["control_group"]) if raw.get("control_group") else None),
            properties=tuple((str(name), value) for name, value in raw.get("properties", ())),
        )

    def kill(self, *, project_root: Path, unit_name: str, signal_number: int) -> None:
        self._request(
            "systemd_kill",
            project_root=str(project_root),
            unit_name=unit_name,
            signal_number=int(signal_number),
        )

    def stop(self, *, project_root: Path, unit_name: str) -> None:
        self._request(
            "systemd_stop",
            project_root=str(project_root),
            unit_name=unit_name,
        )

    def reset_failed(self, *, project_root: Path, unit_name: str) -> None:
        self._request(
            "systemd_reset_failed",
            project_root=str(project_root),
            unit_name=unit_name,
        )

    def release(self, *, project_root: Path, unit_name: str) -> None:
        self._request("systemd_release", project_root=str(project_root), unit_name=unit_name)

    def release_runtime_root(self, *, project_root: Path, runtime_id: str) -> None:
        self._request(
            "systemd_release_runtime_root",
            project_root=str(project_root),
            runtime_id=runtime_id,
        )
