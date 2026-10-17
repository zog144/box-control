"""Source-level acceptance for read-only console application progress."""
import json
import socket
import struct
from types import SimpleNamespace

import pytest

from zog.box_control.application_progress import (
    SCHEMA, observe_application_progress, local_session_capabilities,
)
from zog.box_control.application_progress_broker import (
    ApplicationProgressBroker, WIRE, respond_one,
)
from zog.box_control.role_broker import _json_bytes, RoleBrokerError


JOB = "a" * 32
BOOT = "b" * 32
RUNTIME = "desktop-ABC123"


class Source:
    def __init__(self):
        self.calls = []
        self.job_state = "completed"
        self.outcome = "success"
        self.ref = SimpleNamespace(
            runtime_id=RUNTIME, application="firefox", request_id="signed-123",
            software_view="sha256:" + "d" * 64, boot_id=BOOT,
            state=SimpleNamespace(value="running"), cleanup_pending=True,
            error=None, programs=[SimpleNamespace(result=None)],
        )

    def boot_id_provider(self):
        return BOOT

    def list_build_jobs(self, *, after=None, limit=4):
        self.calls.append(("list", after, limit))
        if after == JOB:
            return {"jobs": [], "has_more": False}
        return {"jobs": [{"job_id": JOB, "state": self.job_state,
                          "outcome": self.outcome}], "has_more": False}

    def inspect_build_job(self, job_id):
        self.calls.append(("inspect", job_id))
        return {"schema": 1, "entity": "job:" + JOB, "job_id": JOB,
                "request_id": "signed-build-123", "state": self.job_state,
                "outcome": self.outcome, "process_cleanup_complete": True,
                "request": {"command": ["--secret", "DO-NOT-EXPOSE"],
                            "environment": {"TOKEN": "DO-NOT-EXPOSE"}}}

    def application_runtimes(self):
        self.calls.append(("runtimes",))
        return {RUNTIME: self.ref}

    def observe_application_runtime(self, runtime_id):
        self.calls.append(("observe", runtime_id))
        return {"runtime_id": runtime_id, "status": "observed",
                "programs": [{"program": "web", "status": "observed",
                              "observation": {"active_state": "active"}}]}


def test_progress_is_bounded_read_only_and_does_not_imply_publication():
    source = Source()
    raw = observe_application_progress(source, limit=1)
    assert raw["schema"] == SCHEMA and raw["host_id"] is None
    assert raw["installation_id"] is None
    assert len(raw["items"]) == 2  # one build job and one managed runtime
    build, runtime = raw["items"]
    assert build["application"] is None  # image-build owns package association
    assert build["state"] == "succeeded" and build["phase"] == "building"
    assert build["selected_software_id"] is None
    assert build["progress"] is None
    assert runtime["state"] == "running" and runtime["phase"] == "launching"
    assert runtime["selected_software_id"] == source.ref.software_view
    assert all(row["phase"] != "publishing" for row in raw["items"])
    assert "DO-NOT-EXPOSE" not in json.dumps(raw)
    assert set(source.calls) == {("list", None, 1), ("inspect", JOB),
                                 ("runtimes",), ("observe", RUNTIME)}


def test_unavailable_or_conflicting_build_evidence_is_not_success():
    source = Source()
    source.inspect_build_job = lambda job: (_ for _ in ()).throw(FileNotFoundError())
    row = observe_application_progress(source)["items"][0]
    assert row["state"] == "unknown"
    assert row["message_code"] == "build-evidence-unavailable"


@pytest.mark.parametrize("state,outcome,expected", [
    ("running", None, "unknown"),
    ("unknown", "unknown", "uncertain"),
    ("cleanup", "success", "uncertain"),
    ("completed", "cancelled", "cancelled"),
    ("completed", "timeout", "failed"),
    ("completed", "success", "succeeded"),
])
def test_build_phase_state_reflects_saved_outcome(state, outcome, expected):
    source = Source()
    source.job_state, source.outcome = state, outcome
    assert observe_application_progress(source)["items"][0]["state"] == expected


def test_prior_boot_and_unverifiable_running_state_are_unknown():
    source = Source()
    source.ref.boot_id = "f" * 32
    raw = observe_application_progress(source)
    assert raw["items"][1]["state"] == "unknown"
    assert ("observe", RUNTIME) not in source.calls


def test_missing_runtime_observation_fails_closed():
    source = Source()
    source.observe_application_runtime = lambda _id: {"status": "partial"}
    assert observe_application_progress(source)["items"][1]["state"] == "unknown"


def test_pagination_and_input_bounds():
    source = Source()
    p = observe_application_progress(source, after_build=JOB, after_runtime=RUNTIME)
    assert not p["items"]
    assert p["page"]["next_build"] == JOB
    assert p["page"]["next_runtime"] == RUNTIME
    for kwargs in ({"limit": True}, {"limit": 50}, {"after_build": "../state"},
                   {"after_runtime": "/etc/passwd"}):
        with pytest.raises(ValueError):
            observe_application_progress(source, **kwargs)


def test_local_session_is_explicitly_unsupported_not_remote_workspace():
    data = local_session_capabilities()
    assert data["supported"] is False
    assert data["workspace_v1_is_local_seat"] is False
    assert data["login_role"] == "not-authorized"
    assert "seat" not in data or data["status"] == "not-implemented"


def test_broker_has_only_snapshot_and_capability_operations():
    broker = ApplicationProgressBroker(Source(), authorized_uid=9901)
    frame = {"schema": WIRE, "operation": "snapshot", "limit": 1,
             "after_build": None, "after_runtime": None}
    assert broker.handle(frame)["schema"] == SCHEMA
    assert broker.handle({"schema": WIRE, "operation": "local-session"})["supported"] is False
    for hostile in (
        {"schema": WIRE, "operation": "start", "program": "greetd"},
        {**frame, "project": "/etc/zog"},
        {**frame, "command": "root"},
        {**frame, "limit": 40},
    ):
        with pytest.raises((RoleBrokerError, ValueError)):
            broker.handle(hostile)


def test_wrong_socket_uid_is_rejected_before_parsing(monkeypatch):
    from zog.box_control import application_progress_broker as module
    monkeypatch.setattr(module, "_peer_uid", lambda _sock: 9000)
    source = Source()
    broker = ApplicationProgressBroker(source, authorized_uid=9901)
    client, server = socket.socketpair()
    try:
        client.sendall(b"garbage")
        respond_one(broker, server)
        client.settimeout(1)
        try:
            assert client.recv(1) == b""
        except ConnectionResetError:
            pass
        assert source.calls == []
    finally:
        client.close()


def test_right_peer_receives_only_bounded_redacted_snapshot(monkeypatch):
    from zog.box_control import application_progress_broker as module
    monkeypatch.setattr(module, "_peer_uid", lambda _sock: 9901)
    broker = ApplicationProgressBroker(Source(), authorized_uid=9901)
    client, server = socket.socketpair()
    try:
        client.sendall(_json_bytes({"schema": WIRE, "operation": "snapshot",
                                    "after_build": None, "after_runtime": None,
                                    "limit": 1}, 4096))
        respond_one(broker, server)
        size = struct.unpack("!I", client.recv(4))[0]
        body = b""
        while len(body) < size:
            body += client.recv(size-len(body))
        assert size < 32768
        assert json.loads(body)["result"]["schema"] == SCHEMA
        assert b"DO-NOT-EXPOSE" not in body
    finally:
        client.close()


def test_consumer_requires_matching_controller_peer_and_sanitized_page(monkeypatch):
    from zog.box_control.application_progress_client import (
        ApplicationProgressClient, ProgressClientError,
    )
    client = ApplicationProgressClient(
        "/run/zog/application-progress.sock", controller_uid=9902,
    )
    source = Source()
    page = observe_application_progress(source, limit=1)
    monkeypatch.setattr(client, "_request", lambda _frame: page)
    assert client.snapshot(limit=1)["items"] == page["items"]
    for modify in (
        lambda p: p.update(boot_id="../secrets"),
        lambda p: p["items"][0].update(evidence_ref="/home/private"),
        lambda p: p["items"][1].update(application="<script>"),
        lambda p: p["items"][0].update(state="published"),
        lambda p: p.update(snapshot_id="../forged"),
        lambda p: p["items"][1].update(selected_software_id="/usr/bin/python"),
    ):
        import copy
        broken = copy.deepcopy(page)
        modify(broken)
        monkeypatch.setattr(client, "_request", lambda _frame, v=broken: v)
        with pytest.raises(ProgressClientError):
            client.snapshot(limit=1)


def test_consumer_rejects_incorrect_login_capability(monkeypatch):
    from zog.box_control.application_progress_client import (
        ApplicationProgressClient, ProgressClientError,
    )
    client = ApplicationProgressClient(
        "/run/zog/application-progress.sock", controller_uid=9902,
    )
    monkeypatch.setattr(client, "_request", lambda _frame: local_session_capabilities())
    assert client.local_session()["supported"] is False
    monkeypatch.setattr(client, "_request", lambda _frame: {
        **local_session_capabilities(), "supported": True,
    })
    with pytest.raises(ProgressClientError):
        client.local_session()


def test_consumer_rejects_wrong_server_uid_before_receiving_reply(monkeypatch):
    from zog.box_control import application_progress_client as module
    from zog.box_control.application_progress_client import (
        ApplicationProgressClient, ProgressClientError,
    )
    monkeypatch.setattr(module, "_peer_uid", lambda _sock: 9000)
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, path): pass
        def sendall(self, data): pytest.fail("untrusted peer must not receive request")
    monkeypatch.setattr(module.socket, "socket", lambda *_args: FakeSocket())
    client = ApplicationProgressClient(
        "/run/zog/application-progress.sock", controller_uid=9902,
    )
    with pytest.raises(ProgressClientError, match="peer-identity"):
        client.snapshot()

 

def test_host_welcome_listener_accepts_exact_root_owned_0600_socket(monkeypatch):
    import stat
    from pathlib import Path
    from types import SimpleNamespace
    from zog.box_control import application_progress_broker as module

    target = module.CONSOLE_SOCKET
    assert str(target) == "/run/host-welcome/controller-progress.sock"
    valid = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=12001)
    original_lstat = Path.lstat

    def safe_lstat(self):
        if self == target:
            return valid
        if self == Path("/run") or self == target.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", safe_lstat)
    sock = SimpleNamespace(getsockname=lambda: str(target))
    module.verify_host_welcome_listener(sock, 12001)

    from zog.box_control.role_broker import RoleBrokerError
    with pytest.raises(RoleBrokerError, match="wrong host-welcome listener"):
        module.verify_host_welcome_listener(
            SimpleNamespace(getsockname=lambda: "/run/zog-host-export/application-progress/application-progress.sock"),
            12001,
        )
    with pytest.raises(RoleBrokerError, match="invalid host-welcome consumer"):
        module.verify_host_welcome_listener(sock, True)


@pytest.mark.parametrize("attack", [
    "wrong-owner", "world-readable", "socket-symlink", "parent-writable",
    "parent-user-owned", "parent-symlink",
])
def test_host_welcome_listener_rejects_unsafe_ownership_or_ancestry(monkeypatch, attack):
    import stat
    from pathlib import Path
    from types import SimpleNamespace
    from zog.box_control import application_progress_broker as module
    from zog.box_control.role_broker import RoleBrokerError

    target = module.CONSOLE_SOCKET
    original_lstat = Path.lstat

    def forged_lstat(self):
        if self == target:
            return SimpleNamespace(
                st_mode=(stat.S_IFLNK | 0o600 if attack == "socket-symlink" else
                         stat.S_IFSOCK | (0o666 if attack == "world-readable" else 0o600)),
                st_uid=12002 if attack == "wrong-owner" else 12001,
            )
        if self == Path("/run") or self == target.parent:
            bad = self == target.parent
            return SimpleNamespace(
                st_mode=(stat.S_IFLNK | 0o755 if bad and attack == "parent-symlink"
                         else stat.S_IFDIR | (0o777 if bad and attack == "parent-writable" else 0o755)),
                st_uid=12002 if bad and attack == "parent-user-owned" else 0,
            )
        return original_lstat(self)

    monkeypatch.setattr(Path, "lstat", forged_lstat)
    sock = SimpleNamespace(getsockname=lambda: str(target))
    with pytest.raises(RoleBrokerError, match="untrusted console"):
        module.verify_host_welcome_listener(sock, 12001)


def test_host_welcome_mode_refuses_station_socket_and_unapproved_privilege(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from zog.box_control import application_progress_broker as module
    from zog.box_control.role_broker import RoleBrokerError
    monkeypatch.setattr(module.os, "geteuid", lambda: 12002)
    monkeypatch.setattr(module, "Project", lambda p: SimpleNamespace(
        require_state_allowed=lambda: None,
    ))
    monkeypatch.setattr(module, "BoxControl", lambda p, **kwargs: Source())
    monkeypatch.setattr(
        "zog.box_control.runtime.root_control.RootControlSystemdTransport",
        lambda p: SimpleNamespace(socket_path=p),
    )
    events = []

    @contextmanager
    def activated():
        events.append("activated")
        yield SimpleNamespace()

    monkeypatch.setattr(module, "_activated_socket", activated)
    monkeypatch.setattr(module, "verify_host_welcome_listener",
                        lambda sock, uid: events.append(("verified", uid)))
    args = ["--project", str(tmp_path), "--authorized-uid", "12001",
            "--observer-role", "host-welcome"]
    with pytest.raises(RoleBrokerError, match="unapproved console"):
        module.main(args)
    assert events == ["activated"]
    events.clear()
    with pytest.raises(RoleBrokerError, match="unapproved console"):
        module.main(args + ["--root-control-socket", "/run/station/socket"])
    assert events == ["activated"]
