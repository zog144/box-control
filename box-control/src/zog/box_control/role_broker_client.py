"""Narrow host-discover-facing client for a root-owned systemd socket unit.

The request wire exposes only one application-specific role lifecycle, not
arbitrary project, root-control, systemd or shell operations.
"""
from __future__ import annotations

import socket
import struct
import time
from pathlib import Path
from types import SimpleNamespace

from .role_broker import SCHEMA, RoleBrokerError, _decode, _json_bytes


class RoleBrokerClient:
    def __init__(self, socket_path: str | Path, *, application: str, timeout_seconds=10):
        path = Path(socket_path)
        if not path.is_absolute() or path == Path("/"):
            raise ValueError("absolute broker Unix socket path required")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid broker timeout")
        from .role_broker import _APPLICATION
        if not isinstance(application, str) or not _APPLICATION.fullmatch(application):
            raise ValueError("explicit application binding required")
        self.application = application
        self.socket_path = path
        self.timeout_seconds = float(timeout_seconds)

    def _call(self, operation, **arguments):
        # No client controls application, project, unit, PID or arbitrary command.
        request = {"schema": SCHEMA, "operation": operation, **arguments}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout_seconds)
            conn.connect(str(self.socket_path))
            conn.sendall(_json_bytes(request, 4096))
            result = _decode_reply(conn)
        if not isinstance(result, dict):
            raise RoleBrokerError("invalid broker response")
        if result.get("ok") is False:
            code = result.get("code")
            if not isinstance(code, str) or len(code) > 48:
                code = "controller-blocked"
            raise RoleBrokerError("application role broker unavailable or blocked", code=code)
        if result.get("ok") is not True or result.get("application") != self.application:
            raise RoleBrokerError("broker application identity mismatch", code="scope-rejected")
        return result["result"]

    def application_available(self):
        return self._call("available")["available"]

    def application_preparation_status(self):
        return self._call("preparation")

    def application_launch_preflight(self):
        """Sanitized advisory controller checks; no implicit admission/start."""
        return self._call("preflight")

    def issue_application_request_id(self):
        return self._call("issue")["request_id"]

    def launch_application(self, application, *, request_id):
        # application is an echo for BoxBridge compatibility, never passed as
        # an RPC selector; an incorrect name is rejected after response.
        if application != self.application:
            raise RoleBrokerError("application scope disagrees with broker client")
        value = self._call("start", request_id=request_id)
        return SimpleNamespace(application=application, runtime_id=value["runtime_id"])

    def cancel_application_launch(self, application, *, request_id):
        if application != self.application:
            raise RoleBrokerError("application scope disagrees with broker client")
        result = self._call("cancel", request_id=request_id)
        if result.get("application") != application:
            raise RoleBrokerError("broker cancellation application disagrees")
        return result

    def terminate_application_runtime(self, runtime_id, *, request_id):
        return self._call("terminate", runtime_id=runtime_id, request_id=request_id)

    def observe_application_runtime(self, runtime_id):
        return self._call("observe", runtime_id=runtime_id)

    def application_request_result(self, request_id):
        return self._call("result", request_id=request_id)


def _decode_reply(conn):
    # Response can contain bounded program observations but never unbounded
    # content. Reject any extra framing; the socket closes after one request.
    length = struct.unpack("!I", _read_exact(conn, 4))[0]
    if length < 1 or length > 128 * 1024:
        raise RoleBrokerError("invalid broker response size")
    import json
    try:
        return json.loads(_read_exact(conn, length).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RoleBrokerError("invalid broker response") from exc


def _read_exact(conn, n):
    parts = []
    while n:
        chunk = conn.recv(n)
        if not chunk:
            raise RoleBrokerError("incomplete broker response")
        parts.append(chunk)
        n -= len(chunk)
    return b"".join(parts)
