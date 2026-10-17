"""Fixed-identity, read-only progress broker for console/administration consumers.

No client-selected project, application, file, systemd unit, command or action.
The systemd socket is installed by host owner; this is source, not deployment.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import stat
from pathlib import Path
from threading import BoundedSemaphore

from .api import BoxControl
from .application_progress import (
    observe_application_progress, local_session_capabilities,
)
from .project import Project
from .role_broker import (
    _activated_socket, _decode, _json_bytes, _peer_uid, RoleBrokerError,
)

WIRE = "zog-application-progress-rpc-v1"
CONSOLE_SOCKET = Path("/run/host-welcome/controller-progress.sock")


class ApplicationProgressBroker:
    def __init__(self, control, *, authorized_uid):
        if type(authorized_uid) is not int or not 1 <= authorized_uid < 2**31:
            raise RoleBrokerError("invalid progress observer UID")
        self.control = control
        self.authorized_uid = authorized_uid

    def handle(self, request):
        if type(request) is not dict or request.get("schema") != WIRE:
            raise RoleBrokerError("invalid progress request schema")
        operation = request.get("operation")
        if operation == "snapshot":
            if set(request) != {"schema", "operation", "after_build", "after_runtime", "limit"}:
                raise RoleBrokerError("invalid progress snapshot request shape")
            return observe_application_progress(
                self.control, after_build=request["after_build"],
                after_runtime=request["after_runtime"], limit=request["limit"],
            )
        if operation == "local-session":
            if set(request) != {"schema", "operation"}:
                raise RoleBrokerError("invalid local-session request shape")
            return local_session_capabilities()
        raise RoleBrokerError("progress endpoint is strictly read-only")


def respond_one(broker, conn):
    with conn:
        conn.settimeout(30)
        if _peer_uid(conn) != broker.authorized_uid:
            # Authentication precedes request parsing and controller reads.
            return
        try:
            output = broker.handle(_decode(conn))
            data = _json_bytes({"ok": True, "result": output}, 32768)
        except Exception:
            # Never reflect private host paths, argv, saved journal or errors.
            data = _json_bytes(
                {"ok": False, "error": "progress-unavailable"}, 4096,
            )
        conn.sendall(data)


def verify_host_welcome_listener(listener, consumer_uid):
    """Constrain a console observer to the separately installed socket.

    This does not install a socket or authorize a client: host-install pins the
    root-owned unit, the broker's controller UID, and the consumer's grant.
    """
    if type(consumer_uid) is not int or not 1 <= consumer_uid < 2**31:
        raise RoleBrokerError("invalid host-welcome consumer UID")
    if listener.getsockname() != str(CONSOLE_SOCKET):
        raise RoleBrokerError("wrong host-welcome listener")
    parent = Path("/")
    for part in CONSOLE_SOCKET.parent.parts[1:]:
        parent /= part
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise RoleBrokerError("untrusted console listener ancestry")
    info = CONSOLE_SOCKET.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != consumer_uid
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise RoleBrokerError("untrusted console listener ownership")
    # Directory protection prevents a non-root path swap after inspection.
    # SO_PEERCRED still enforces the caller UID separately for each request.


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Zog controller progress")
    parser.add_argument("--project", required=True)
    parser.add_argument("--authorized-uid", required=True, type=int)
    parser.add_argument("--root-control-socket")
    parser.add_argument("--observer-role", choices=("station", "host-welcome"), default="station")
    args = parser.parse_args(argv)
    if os.geteuid() == 0 or os.geteuid() == args.authorized_uid:
        raise RoleBrokerError("distinct nonroot controller owner required")
    path = Path(args.project)
    if (not path.is_absolute() or path == Path("/")
            or path != path.resolve()):
        raise RoleBrokerError("fixed canonical controller project required")
    # Systemd's unit and the root-owned socket configuration pin both UIDs,
    # project and optional privileged read-only observation transport.
    project = Project(path)
    project.require_state_allowed()
    transport = None
    if args.root_control_socket:
        from .runtime.root_control import RootControlSystemdTransport
        transport = RootControlSystemdTransport(Path(args.root_control_socket))
    kwargs = {"systemd_transport": transport} if transport is not None else {}
    broker = ApplicationProgressBroker(
        BoxControl(project, **kwargs), authorized_uid=args.authorized_uid,
    )
    slots = BoundedSemaphore(4)

    def worker(conn):
        try:
            respond_one(broker, conn)
        finally:
            slots.release()

    with _activated_socket() as listener:
        if args.observer_role == "host-welcome":
            if (args.root_control_socket != "/run/zog/root-control.sock"
                    or args.authorized_uid == os.geteuid()):
                raise RoleBrokerError("unapproved console progress authority")
            verify_host_welcome_listener(listener, args.authorized_uid)
        with ThreadPoolExecutor(max_workers=2) as pool:
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
