"""Fixed host-deploy client for one application and renew/inspect/stage only."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from .role_broker import RoleBrokerError
from .role_broker_client import RoleBrokerClient


class IssuerLifecycleClient(RoleBrokerClient):
    def __init__(self, socket_path: str | Path, *, timeout_seconds=10,
                 controller_uid=None):
        super().__init__(
            socket_path, application="certify-manage",
            timeout_seconds=timeout_seconds, expected_server_uid=controller_uid,
        )

    def launch_application(self, application, *, request_id, program_name):
        if application != self.application or program_name not in {"renew", "stage", "inspect"}:
            raise RoleBrokerError("issuer lifecycle selection is not authorized",
                                  code="scope-rejected")
        result = self._call("start", request_id=request_id, program=program_name)
        if result.get("program") != program_name:
            raise RoleBrokerError("issuer selection mismatch", code="scope-rejected")
        return SimpleNamespace(
            application=self.application, runtime_id=result["runtime_id"],
            selected_program=program_name,
        )

    def application_request_result(self, request_id):
        # The durable issuer request identity is selected by the caller's
        # protected operation journal, not by a returned broker field.
        from .role_broker import _REQUEST
        if type(request_id) is not str or _REQUEST.fullmatch(request_id) is None:
            raise RoleBrokerError("invalid issuer request identity", code="scope-rejected")
        result = self._call("result", request_id=request_id)
        program = result.get("program") if type(result) is dict else None
        if (type(result) is not dict or result.get("request_id") != request_id
                or (program is not None
                    and (type(program) is not str
                         or program not in {"renew", "inspect", "stage"}))):
            raise RoleBrokerError("issuer result identity or selection mismatch",
                                  code="scope-rejected")
        return result

    def cancel_application_launch(self, application, *, request_id):
        if application != self.application:
            raise RoleBrokerError("issuer application scope mismatch", code="scope-rejected")
        return self._call("cancel", request_id=request_id)

    def observe_application_runtime(self, runtime_id):
        return self._call("observe", runtime_id=runtime_id)

    def inspect_issuer_result(self, request_id):
        """Read an exact inspection only from independently pinned controller UID."""
        if self.expected_server_uid is None:
            raise RoleBrokerError("authenticated controller owner UID required for inspect",
                                  code="scope-rejected")
        from .issuer_inspection_evidence import sanitize_issuer_inspection
        return sanitize_issuer_inspection(
            self._call("inspect-result", request_id=request_id), request_id,
        )
