"""Fixed host-deploy client for one application and renew/inspect/stage only."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from .role_broker import RoleBrokerError
from .role_broker_client import RoleBrokerClient


class IssuerLifecycleClient(RoleBrokerClient):
    def __init__(self, socket_path: str | Path, *, timeout_seconds=10):
        super().__init__(
            socket_path, application="certify-manage",
            timeout_seconds=timeout_seconds,
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
        result = self._call("result", request_id=request_id)
        if result.get("program") not in {"renew", "stage", None}:
            raise RoleBrokerError("issuer result selection mismatch", code="scope-rejected")
        return result

    def cancel_application_launch(self, application, *, request_id):
        if application != self.application:
            raise RoleBrokerError("issuer application scope mismatch", code="scope-rejected")
        return self._call("cancel", request_id=request_id)

    def observe_application_runtime(self, runtime_id):
        return self._call("observe", runtime_id=runtime_id)

    def inspect_issuer_result(self, request_id):
        """Read root-control-attested one-shot inspection for an exact request."""
        return self._call("inspect-result", request_id=request_id)
