"""Single-application, peer-credential-checked local lifecycle broker.

This is a narrow RPC facade, NOT a second supervisor. It only executes
explicit role-owner requests using box-control's durable lifecycle APIs.
The broker runs unprivileged as the production project owner. A separate,
root-owned systemd socket unit grants access to one specific client UID.
No application specification, shell command, PID, arbitrary path, image,
grant, or workspace identity is accepted in request payloads.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
import json
import os
from pathlib import Path
import pwd
import re
import socket
import struct
import time

from .api import BoxControl
from .model import ApplicationStartPolicy
from .project import Project
from .runtime.root_control import RootControlSystemdTransport

SCHEMA = "zog-application-role-broker-v1"
_REQUEST = re.compile(r"[A-Za-z0-9-]{1,200}\Z")
_RUNTIME = re.compile(r"[A-Za-z0-9-]{1,40}\Z")
_APPLICATION = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REQUEST = 4096
_MAX_REPLY = 128 * 1024


class RoleBrokerError(ValueError):
    def __init__(self, message, *, code="blocked"):
        super().__init__(message)
        self.code = code


class FixedPublishedImage:
    """Select only the reviewed immutable published generation, never active."""

    def __init__(self, generation):
        if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
            raise RoleBrokerError("exact published image generation required", code="image-selection-invalid")
        self.generation = generation

    def ensure(self, project):
        from .images import published_selection
        return published_selection(project.path, self.generation)

    def preview(self, project):
        # Existing preflight can verify immutable selected image/mounts without
        # looking at the mutable image-build active symlink or building anything.
        return self.ensure(project)


def _text(value, pattern, name):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RoleBrokerError("invalid " + name)
    return value


def _json_bytes(message, maximum):
    data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > maximum:
        raise RoleBrokerError("broker response or request exceeds size limit")
    return struct.pack("!I", len(data)) + data


def _read_exact(conn, count):
    parts = []
    while count:
        data = conn.recv(count)
        if not data:
            raise RoleBrokerError("incomplete broker message")
        count -= len(data)
        parts.append(data)
    return b"".join(parts)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RoleBrokerError("duplicate broker request key")
        result[key] = value
    return result


def _decode(conn):
    length = struct.unpack("!I", _read_exact(conn, 4))[0]
    if not 0 < length <= _MAX_REQUEST:
        raise RoleBrokerError("invalid broker request length")
    try:
        body = json.loads(_read_exact(conn, length).decode("utf-8"),
                          object_pairs_hook=_unique_pairs)
    except RoleBrokerError:
        raise
    except (ValueError, UnicodeError) as exc:
        raise RoleBrokerError("invalid broker request") from exc
    if not isinstance(body, dict):
        raise RoleBrokerError("broker request must be an object")
    return body


class RoleBroker:
    """Immutable application scope, no role-election or background evaluation."""

    def __init__(self, control, application, *, authorized_uid,
                 expected_software_view=None):
        _text(application, _APPLICATION, "application")
        if (expected_software_view is not None and
                (not isinstance(expected_software_view, str)
                 or not _DIGEST.fullmatch(expected_software_view))):
            raise RoleBrokerError("exact published software view required")
        if type(authorized_uid) is not int or not 1 <= authorized_uid < 2**32:
            raise RoleBrokerError("authorized client UID must be a non-root integer")
        self.control = control
        self.application = application
        self.authorized_uid = authorized_uid
        self.expected_software_view = expected_software_view

    def _application(self):
        from .specification import discover_applications
        spec = discover_applications(self.control.project.application_dir).get(self.application)
        if spec is None:
            raise RoleBrokerError("authorized application definition is unavailable", code="application-unavailable")
        if (spec.start_policy != ApplicationStartPolicy.EXTERNALLY_CONTROLLED
                or not spec.persistent or spec.multiple_instances
                or spec.execution_identity != "application"
                or (self.expected_software_view is not None
                    and spec.software_view != self.expected_software_view)):
            raise RoleBrokerError("authorized role application contract is unsupported", code="definition-mismatch")
        return spec

    def _runtime(self, runtime_id):
        runtime_id = _text(runtime_id, _RUNTIME, "runtime identity")
        reference = self.control.application_runtime(runtime_id)
        if (reference is None or reference.application != self.application
                or reference.purpose != "application"):
            raise RoleBrokerError("runtime is not owned by authorized application", code="scope-rejected")
        return runtime_id

    def _request_result(self, request_id):
        outcome = self.control.application_request_result(request_id)
        if outcome is None:
            return {"status": "unknown", "request_id": request_id}
        if (getattr(outcome.operation, "value", outcome.operation) != "launch"
                or outcome.application != self.application):
            raise RoleBrokerError("request belongs to another application or operation")
        return {
            "status": outcome.status.value, "request_id": request_id,
            "runtime_id": outcome.runtime_id, "error": None,
        }

    def handle(self, request):
        if request.get("schema") != SCHEMA:
            raise RoleBrokerError("unsupported role broker schema")
        method = request.get("operation")
        shape = {
            "available": set(),
            "preparation": set(),
            "preflight": set(),
            "issue": set(),
            "start": {"request_id"},
            "cancel": {"request_id"},
            "result": {"request_id"},
            "terminate": {"runtime_id", "request_id"},
            "observe": {"runtime_id"},
        }
        if method not in shape:
            raise RoleBrokerError("operation is not authorized")
        if set(request) != {"schema", "operation"} | shape[method]:
            raise RoleBrokerError("unexpected broker request fields")
        if "request_id" in request:
            _text(request["request_id"], _REQUEST, "request identity")
        if "runtime_id" in request:
            _text(request["runtime_id"], _RUNTIME, "runtime identity")
        if method == "available":
            try:
                self._application()
            except RoleBrokerError:
                return {"available": False}
            return {"available": True}
        # Only admission/start depends on a current definition. Withdrawals
        # and inspections must remain possible if an operator removes or
        # replaces the application specification after a runtime started.
        if method in {"issue", "preparation", "preflight", "start"}:
            self._application()
        if method == "preparation":
            raw = self.control.application_preparation(self.application)
            if raw is None:
                return {"state": "unavailable"}
            return {"state": raw.get("state", "unavailable"),
                    "revision": raw.get("revision"),
                    "storage_id": raw.get("storage_id")}
        if method == "preflight":
            assessment = self.control.preflight_application_launch(self.application)
            if not isinstance(assessment, dict) or not isinstance(assessment.get("checks"), list):
                raise RoleBrokerError("invalid controller preflight result")
            if len(assessment["checks"]) > 64:
                raise RoleBrokerError("excessive preflight checks")
            checks = []
            for check in assessment["checks"]:
                if (not isinstance(check, dict)
                        or check.get("status") not in {"ready", "blocked", "unable-to-verify"}
                        or not isinstance(check.get("code"), str)
                        or not _APPLICATION.fullmatch(check["code"])):
                    raise RoleBrokerError("invalid preflight evidence")
                checks.append({"code": check["code"], "status": check["status"]})
            status = assessment.get("status")
            if status not in {"blocked", "unable-to-verify", "ready-to-attempt"}:
                raise RoleBrokerError("invalid preflight status")
            return {"status": status, "advisory": True, "checks": checks}
        if method == "issue":
            return {"request_id": self.control.issue_application_request_id()}
        if method == "start":
            raw = self.control.application_preparation(self.application)
            if not isinstance(raw, dict) or raw.get("state") != "ready":
                raise RoleBrokerError("application preparation is not ready", code="preparation-not-ready")
            reference = self.control.launch_application(
                self.application, request_id=request["request_id"]
            )
            if reference.application != self.application:
                raise RoleBrokerError("controller returned another application identity")
            return {"request_id": request["request_id"],
                    "runtime_id": reference.runtime_id}
        if method == "cancel":
            return self.control.cancel_application_launch(
                self.application, request_id=request["request_id"]
            )
        if method == "result":
            return self._request_result(request["request_id"])
        runtime_id = self._runtime(request["runtime_id"])
        if method == "observe":
            return _safe_observation(
                self.control.observe_application_runtime(runtime_id), runtime_id
            )
        if method == "terminate":
            reference = self.control.terminate_application_runtime(
                runtime_id, request_id=request["request_id"]
            )
            return {"request_id": request["request_id"],
                    "runtime_id": reference.runtime_id}
        raise AssertionError("unreachable")


def _safe_observation(raw, runtime_id):
    """Never forward systemd Environment, commands, BindPaths or host paths."""
    if not isinstance(raw, dict) or raw.get("runtime_id") != runtime_id:
        raise RoleBrokerError("controller returned mismatched runtime observation")
    allowed = {"observed", "partial", "unavailable", "identity-unavailable",
               "integrity-fault", "previous-boot", "absent"}
    status = raw.get("status")
    members = raw.get("programs")
    if status not in allowed or not isinstance(members, list) or len(members) > 32:
        raise RoleBrokerError("invalid runtime observation")
    safe = []
    names = set()
    for item in members:
        if not isinstance(item, dict):
            raise RoleBrokerError("invalid program observation")
        name, state = item.get("program"), item.get("status")
        if (not isinstance(name, str) or not _APPLICATION.fullmatch(name)
                or name in names or state not in allowed):
            raise RoleBrokerError("invalid program observation")
        names.add(name)
        obs = item.get("observation")
        public_obs = None
        if state == "observed":
            if not isinstance(obs, dict) or obs.get("active_state") not in {
                    "active", "inactive", "failed", "activating", "deactivating",
                    "reloading", "maintenance"}:
                raise RoleBrokerError("invalid service state observation")
            public_obs = {
                "active_state": obs["active_state"],
                "sub_state": str(obs.get("sub_state", ""))[:40],
                "result": str(obs.get("result", ""))[:60],
                "invocation_id": str(obs.get("invocation_id", ""))[:64],
            }
        safe.append({
            "program": name, "status": state,
            "recorded_invocation_id": item.get("recorded_invocation_id"),
            "observation": public_obs,
        })
    return {
        "runtime_id": runtime_id, "status": status,
        "persisted_state": raw.get("persisted_state"),
        "cleanup_pending": raw.get("cleanup_pending"),
        "programs": safe,
    }


def _peer_uid(conn):
    if not hasattr(socket, "SO_PEERCRED"):
        raise RoleBrokerError("Linux SO_PEERCRED is required")
    pid, uid, _gid = struct.unpack(
        "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    if pid <= 0:
        raise RoleBrokerError("peer has no valid process identity")
    return uid


def respond_one(broker, conn):
    """One bounded request, then close. No RPC provides arbitrary execution."""
    with conn:
        conn.settimeout(8)
        if _peer_uid(conn) != broker.authorized_uid:
            # Do not parse unauthenticated data or reveal project state.
            return
        try:
            result = broker.handle(_decode(conn))
            data = _json_bytes({"ok": True, "application": broker.application, "result": result}, _MAX_REPLY)
        except Exception as exc:
            # Emit a stable nonsecret failure code; never expose paths, env,
            # tokens, saved commands or the underlying exception message.
            from .errors import PersistenceError, RecoveryRequired, ProjectBusy
            from .images import ImageProviderError
            if isinstance(exc, RoleBrokerError):
                code = exc.code
            elif isinstance(exc, PersistenceError):
                code = "storage-fault"
            elif isinstance(exc, RecoveryRequired):
                code = "recovery-required"
            elif isinstance(exc, ProjectBusy):
                code = "controller-busy"
            elif isinstance(exc, ImageProviderError):
                code = "image-unavailable"
            else:
                code = "controller-blocked"
            data = _json_bytes({"ok": False, "error": "blocked", "code": code}, _MAX_REPLY)
        conn.sendall(data)


def _activated_socket():
    if (os.environ.get("LISTEN_PID") != str(os.getpid())
            or os.environ.get("LISTEN_FDS") != "1"):
        raise RoleBrokerError("exactly one systemd-activated Unix socket is required")
    listener = socket.socket(fileno=3)
    if (listener.family != socket.AF_UNIX or listener.type & 0xf != socket.SOCK_STREAM
            or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
        raise RoleBrokerError("role broker requires an activated listening AF_UNIX stream socket")
    return listener


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded application role lifecycle broker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--application", required=True)
    parser.add_argument("--authorized-uid", type=int, required=True)
    parser.add_argument("--generation", required=True, help="exact published image SHA256")
    parser.add_argument("--software-view", required=True, help="exact immutable view sha256:<digest>")
    parser.add_argument("--root-control-socket", default=None)
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        raise RoleBrokerError("role broker must not run as root")
    project_path = Path(args.project)
    if not project_path.is_absolute() or project_path == Path("/"):
        raise RoleBrokerError("absolute production project path required")
    project = Project(project_path)
    project.require_state_allowed()
    # Refuse direct execution under the unprivileged role owner's UID.
    if args.authorized_uid == os.geteuid():
        raise RoleBrokerError("role client must not share project owner identity")
    transport = (RootControlSystemdTransport(Path(args.root_control_socket))
                 if args.root_control_socket else RootControlSystemdTransport())
    broker = RoleBroker(
        BoxControl(project, image_provider=FixedPublishedImage(args.generation),
                   systemd_transport=transport),
        args.application, authorized_uid=args.authorized_uid,
        expected_software_view=args.software_view,
    )
    broker._application()
    def _serve_slot(conn, slots):
        try:
            respond_one(broker, conn)
        finally:
            slots.release()

    # Requests may arrive while an earlier controller mutation is in progress.
    # Do not block read-only observation or withdrawal behind socket handling.
    # The authoritative ProjectLock still serializes all lifecycle mutations.
    with _activated_socket() as listener, ThreadPoolExecutor(max_workers=4) as pool:
        slots = BoundedSemaphore(8)
        while True:
            conn, _address = listener.accept()
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            try:
                pool.submit(_serve_slot, conn, slots)
            except BaseException:
                conn.close()
                slots.release()
                raise


if __name__ == "__main__":
    main()
