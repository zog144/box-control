"""Host-deploy facing client for the fixed read-only issuer observation socket."""
from __future__ import annotations

import socket
from pathlib import Path

from .issuer_observer import _sanitize
from .role_broker import SCHEMA, RoleBrokerError, _json_bytes
from .role_broker_client import _decode_reply


class IssuerObserverClient:
    def __init__(self, socket_path, *, timeout_seconds=10):
        path = Path(socket_path)
        if not path.is_absolute() or path == Path("/"):
            raise ValueError("absolute issuer observer socket path required")
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid issuer observer timeout")
        self.socket_path = path
        self.timeout_seconds = float(timeout_seconds)

    def observe_issuer_request(self, request_id):
        request = {
            "schema": SCHEMA, "operation": "observe-issuer", "request_id": request_id,
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(self.timeout_seconds)
            conn.connect(str(self.socket_path))
            conn.sendall(_json_bytes(request, 4096))
            response = _decode_reply(conn)
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise RoleBrokerError("trusted issuer evidence is unavailable", code="issuer-evidence-unavailable")
        if response.get("application") != "certify-manage":
            raise RoleBrokerError("issuer observer scope disagrees", code="scope-rejected")
        return _sanitize(response.get("result"), request_id)
