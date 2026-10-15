"""Authenticated certificate lifecycle controller broker for host-deploy.

Dedicated socket-activated non-root controller-owner process. Does not expose
root-control or raw project access. One immutable issuer application with two
selected commands: renew or stage; no shell, policy, UID, path or unit selector.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import BoundedSemaphore

from .api import BoxControl
from .model import ApplicationStartPolicy
from .project import Project
from .role_broker import (
    RoleBrokerError, FixedPublishedImage, _DIGEST, _REQUEST, _RUNTIME,
    _text, _activated_socket, _decode, _json_bytes, _peer_uid,
    _safe_observation, SCHEMA,
)
from .runtime.root_control import RootControlSystemdTransport

_ALLOWED = frozenset({"renew", "stage"})
_APP = "certify-manage"


class IssuerLifecycleBroker:
    application = _APP

    def __init__(self, control, *, authorized_uid, software_view):
        if (type(authorized_uid) is not int or authorized_uid < 1
                or authorized_uid >= 2**31
                or not isinstance(software_view, str)
                or not _DIGEST.fullmatch(software_view)):
            raise RoleBrokerError("invalid fixed issuer lifecycle authority")
        self.control = control
        self.authorized_uid = authorized_uid
        self.software_view = software_view

    def _definition(self, program=None):
        from .specification import discover_applications
        spec = discover_applications(self.control.project.application_dir).get(_APP)
        if (spec is None or spec.start_policy != ApplicationStartPolicy.EXTERNALLY_CONTROLLED
                or not spec.persistent or spec.multiple_instances
                or spec.execution_identity != "application"
                or spec.software_view != self.software_view):
            raise RoleBrokerError("issuer application contract differs", code="definition-mismatch")
        if program is not None and program not in {p.name for p in spec.programs}:
            raise RoleBrokerError("selected issuer program unavailable", code="definition-mismatch")
        return spec

    def _result(self, request_id):
        outcome = self.control.application_request_result(request_id)
        if outcome is None:
            return {"request_id": request_id, "status": "unknown",
                    "program": None, "runtime_id": None}
        if (outcome.application != _APP or outcome.program_name not in _ALLOWED
                or getattr(outcome.operation, "value", outcome.operation) != "launch"):
            raise RoleBrokerError("saved issuer request is outside broker scope", code="scope-rejected")
        return {"request_id": request_id, "status": outcome.status.value,
                "program": outcome.program_name, "runtime_id": outcome.runtime_id}

    def _runtime(self, runtime_id):
        ref = self.control.application_runtime(runtime_id)
        if ref is None or ref.application != _APP or len(ref.programs) != 1:
            raise RoleBrokerError("issuer runtime scope disagrees", code="scope-rejected")
        if ref.programs[0].program not in _ALLOWED:
            raise RoleBrokerError("issuer runtime program is unauthorized", code="scope-rejected")
        return ref

    def handle(self, request):
        if not isinstance(request, dict) or request.get("schema") != SCHEMA:
            raise RoleBrokerError("invalid issuer lifecycle request")
        operation = request.get("operation")
        shapes = {
            "available": set(), "preparation": set(),
            "issue": set(), "start": {"request_id", "program"},
            "result": {"request_id"}, "cancel": {"request_id"},
            "observe": {"runtime_id"},
        }
        if operation not in shapes or set(request) != ({"schema", "operation"} | shapes[operation]):
            raise RoleBrokerError("unsupported issuer lifecycle request shape")
        if "request_id" in request:
            _text(request["request_id"], _REQUEST, "request identity")
        if "runtime_id" in request:
            _text(request["runtime_id"], _RUNTIME, "runtime identity")
        if operation == "available":
            try:
                self._definition()
            except RoleBrokerError:
                return {"available": False}
            return {"available": True}
        if operation == "preparation":
            self._definition()
            raw = self.control.application_preparation(_APP)
            if raw is None:
                return {"state": "unavailable"}
            return {"state": raw.get("state", "unavailable"),
                    "revision": raw.get("revision"),
                    "storage_id": raw.get("storage_id")}
        if operation == "issue":
            self._definition()
            return {"request_id": self.control.issue_application_request_id()}
        if operation == "start":
            program = request["program"]
            if type(program) is not str or program not in _ALLOWED:
                raise RoleBrokerError("only exact renew/stage allowed", code="scope-rejected")
            self._definition(program)
            state = self.control.application_preparation(_APP)
            if not isinstance(state, dict) or state.get("state") != "ready":
                raise RoleBrokerError("issuer preparation incomplete", code="preparation-not-ready")
            original = self._result(request["request_id"])
            if original["status"] != "unknown" and original["program"] != program:
                raise RoleBrokerError("request selector already bound", code="scope-rejected")
            runtime = self.control.launch_application(
                _APP, request_id=request["request_id"], program_name=program,
            )
            if (runtime.application != _APP or [p.program for p in runtime.programs] != [program]):
                raise RoleBrokerError("unexpected issuer runtime", code="scope-rejected")
            return {"request_id": request["request_id"], "program": program,
                    "runtime_id": runtime.runtime_id}
        if operation == "result":
            return self._result(request["request_id"])
        if operation == "cancel":
            # Do not let arbitrary request IDs cancel station/archive work.
            known = self._result(request["request_id"])
            if known["status"] == "unknown":
                raise RoleBrokerError("unrecognized selected issuer request", code="scope-rejected")
            raw = self.control.cancel_application_launch(
                _APP, request_id=request["request_id"],
            )
            return {"request_id": request["request_id"], "status": raw.get("status", "blocked")}
        ref = self._runtime(request["runtime_id"])
        return _safe_observation(
            self.control.observe_application_runtime(ref.runtime_id), ref.runtime_id,
        )


def respond_one(broker, conn):
    # Reuse the proven SO_PEERCRED-before-JSON and redacted error logic.
    from .role_broker import respond_one as serve
    return serve(broker, conn)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pinned certificate renew/stage controller broker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--authorized-uid", type=int, required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--software-view", required=True)
    parser.add_argument("--root-control-socket")
    args = parser.parse_args(argv)
    if os.geteuid() == 0 or os.geteuid() == args.authorized_uid:
        raise RoleBrokerError("issuer broker must be a distinct non-root project owner")
    project_path = Path(args.project)
    if (not project_path.is_absolute() or project_path == Path("/")
            or project_path != project_path.resolve()):
        raise RoleBrokerError("canonical absolute issuer project required")
    project = Project(project_path)
    project.require_state_allowed()
    transport = (RootControlSystemdTransport(Path(args.root_control_socket))
                 if args.root_control_socket else RootControlSystemdTransport())
    broker = IssuerLifecycleBroker(
        BoxControl(project, image_provider=FixedPublishedImage(args.generation),
                   systemd_transport=transport),
        authorized_uid=args.authorized_uid, software_view=args.software_view,
    )
    broker._definition()
    slots = BoundedSemaphore(6)

    def worker(conn):
        try:
            respond_one(broker, conn)
        finally:
            slots.release()

    with _activated_socket() as listener, ThreadPoolExecutor(max_workers=3) as pool:
        while True:
            conn, _ = listener.accept()
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            try:
                pool.submit(worker, conn)
            except BaseException:
                conn.close()
                slots.release()
                raise


if __name__ == "__main__":
    main()
