"""Read-only, peer-authenticated certificate issuer observation service.

Own socket, fixed project and application, no mutation or arbitrary paths.
Runs under the trusted non-root controller project owner. Only its existing
root-control socket privilege can inspect private application-owned evidence.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import socket
from threading import BoundedSemaphore

from .api import BoxControl
from .project import Project
from .role_broker import (
    SCHEMA, _activated_socket, _decode, _json_bytes, _peer_uid, RoleBrokerError,
)
from .runtime.root_control import RootControlSystemdTransport

_APPLICATION = "certify-manage"
_FIELDS = {
    "schema", "request_id", "runtime_id", "program", "storage_id",
    "grant_id", "issuer_uid", "issuer_gid", "policy_sha256",
    "host_policy_digest", "certificate_id", "certificate_fingerprint_sha256",
    "journal_updated_at", "outcome", "status", "tls_ready",
}
_STATES = {"ongoing", "stale", "uncertain", "journal-ready"}
_PROGRAMS = {"issue", "renew"}


def _sanitize(raw, request_id):
    if (not isinstance(raw, dict) or set(raw) != _FIELDS
            or raw.get("schema") != "zog-trusted-issuer-observation-v1"
            or raw.get("request_id") != request_id
            or raw.get("status") not in _STATES
            or raw.get("program") not in _PROGRAMS
            or raw.get("tls_ready") is not False):
        raise RoleBrokerError("invalid privileged issuer evidence", code="evidence-unavailable")
    import re
    if (not isinstance(raw.get("issuer_uid"), int)
            or raw["issuer_uid"] < 1000
            or not isinstance(raw.get("issuer_gid"), int)
            or raw["issuer_gid"] < 1000
            or not all(isinstance(raw.get(name), str) and re.fullmatch(pattern, raw[name])
                       for name, pattern in (
                           ("storage_id", "[a-f0-9]{32}"),
                           ("grant_id", "[a-f0-9]{32}"),
                           ("policy_sha256", "[a-f0-9]{64}"),
                           ("host_policy_digest", "sha256:[a-f0-9]{64}"),
                           ("certificate_id", "[a-z][a-z0-9-]{0,62}"),
                       ))):
        raise RoleBrokerError("issuer evidence identity invalid", code="evidence-unavailable")
    for name in ("certificate_fingerprint_sha256", "journal_updated_at", "outcome"):
        val = raw[name]
        if val is not None and (not isinstance(val, str) or len(val) > 90):
            raise RoleBrokerError("unbounded issuer evidence", code="evidence-unavailable")
    return {key: raw[key] for key in sorted(_FIELDS)}


class IssuerObserver:
    application = _APPLICATION

    def __init__(self, control, *, authorized_uid):
        if type(authorized_uid) is not int or not 1 <= authorized_uid < 2**31:
            raise RoleBrokerError("invalid observer peer UID")
        self.control = control
        self.authorized_uid = authorized_uid

    def handle(self, request):
        from .role_broker import _REQUEST as REQUEST
        if (not isinstance(request, dict)
                or set(request) != {"schema", "operation", "request_id"}
                or request.get("schema") != SCHEMA
                or request.get("operation") != "observe-issuer"
                or not isinstance(request["request_id"], str)
                or not REQUEST.fullmatch(request["request_id"])):
            raise RoleBrokerError("unsupported issuer observer request")
        identity = request["request_id"]
        return _sanitize(self.control.observe_issuer_request(identity), identity)


def respond_one(observer, conn):
    with conn:
        conn.settimeout(8)
        if _peer_uid(conn) != observer.authorized_uid:
            return
        try:
            value = observer.handle(_decode(conn))
            data = _json_bytes(
                {"ok": True, "application": _APPLICATION, "result": value}, 4096
            )
        except Exception:
            data = _json_bytes(
                {"ok": False, "error": "blocked", "code": "issuer-evidence-unavailable"},
                4096,
            )
        conn.sendall(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Private read-only issuer observer broker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--authorized-uid", type=int, required=True)
    parser.add_argument("--root-control-socket", default=None)
    args = parser.parse_args(argv)
    if os.geteuid() == 0 or os.geteuid() == args.authorized_uid:
        raise RoleBrokerError("issuer observer requires a distinct non-root controller owner")
    path = Path(args.project)
    if not path.is_absolute() or path == Path("/") or str(path) != str(path.resolve()):
        raise RoleBrokerError("invalid pinned controller project")
    project = Project(path)
    project.require_state_allowed()
    transport = (RootControlSystemdTransport(Path(args.root_control_socket))
                 if args.root_control_socket else RootControlSystemdTransport())
    observer = IssuerObserver(BoxControl(project, systemd_transport=transport),
                              authorized_uid=args.authorized_uid)
    def worker(conn, slots):
        try:
            respond_one(observer, conn)
        finally:
            slots.release()
    with _activated_socket() as listener, ThreadPoolExecutor(max_workers=2) as pool:
        slots = BoundedSemaphore(4)
        while True:
            conn, _address = listener.accept()
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            try:
                pool.submit(worker, conn, slots)
            except BaseException:
                conn.close()
                slots.release()
                raise


if __name__ == "__main__":
    main()
