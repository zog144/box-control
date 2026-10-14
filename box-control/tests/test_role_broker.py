from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import socket
import struct
from types import SimpleNamespace

import pytest

from zog.box_control.model import ApplicationStartPolicy
from zog.box_control.role_broker import (
    RoleBroker, RoleBrokerError, SCHEMA, _decode, _json_bytes, respond_one,
)
from zog.box_control.role_broker_client import RoleBrokerClient


class FakeController:
    def __init__(self, project):
        self.project = SimpleNamespace(path=project, application_dir=project / "application")
        self.ready = True
        self.runtimes = {"RUNTIME1": SimpleNamespace(
            application="archive-mirror", purpose="application", runtime_id="RUNTIME1",
        ), "OTHER1": SimpleNamespace(
            application="station-access", purpose="application", runtime_id="OTHER1",
        )}
        self.starts = []
        self.stops = []
        self.issued = 0
        self.cancel_status = "accepted"

    def issue_application_request_id(self):
        self.issued += 1
        return "ID%d" % self.issued

    def application_preparation(self, application):
        assert application == "archive-mirror"
        return {"state": "ready" if self.ready else "uncertain",
                "storage_id": "a" * 32, "revision": "v1"}

    def launch_application(self, application, *, request_id):
        self.starts.append((application, request_id))
        return self.runtimes["RUNTIME1"]

    def application_runtime(self, runtime_id):
        return self.runtimes.get(runtime_id)

    def cancel_application_launch(self, application, *, request_id):
        return {"request_id": request_id, "application": application,
                "status": self.cancel_status, "runtime_id": "RUNTIME1",
                "operation_id": "operation1"}

    def terminate_application_runtime(self, runtime_id, *, request_id):
        self.stops.append((runtime_id, request_id))
        return self.runtimes[runtime_id]

    def observe_application_runtime(self, runtime_id):
        return {"runtime_id": runtime_id, "status": "observed",
                "programs": [{"program": "server", "status": "observed",
                              "observation": {"active_state": "active"}},
                             {"program": "refresh-scheduler", "status": "observed",
                              "observation": {"active_state": "active"}}]}

    def application_request_result(self, request_id):
        return SimpleNamespace(
            operation="launch", application="archive-mirror",
            status=SimpleNamespace(value="satisfied"), runtime_id="RUNTIME1",
        )


@pytest.fixture
def scope(tmp_path, monkeypatch):
    controller = FakeController(tmp_path)
    spec = SimpleNamespace(start_policy=ApplicationStartPolicy.EXTERNALLY_CONTROLLED,
                           persistent=True, multiple_instances=False,
                           execution_identity="application")
    monkeypatch.setattr("zog.box_control.specification.discover_applications",
                        lambda _: {"archive-mirror": spec})
    return controller, RoleBroker(controller, "archive-mirror", authorized_uid=992), spec


def request(op, **arguments):
    return {"schema": SCHEMA, "operation": op, **arguments}


def test_application_scoped_repeated_requests_and_exact_runtime(scope):
    control, broker, _spec = scope
    assert broker.handle(request("available")) == {"available": True}
    assert broker.handle(request("preparation"))["state"] == "ready"
    request_id = broker.handle(request("issue"))["request_id"]
    assert broker.handle(request("start", request_id=request_id))["runtime_id"] == "RUNTIME1"
    assert broker.handle(request("start", request_id=request_id))["runtime_id"] == "RUNTIME1"
    assert control.starts == [("archive-mirror", request_id)] * 2
    # Production BoxControl, not this broker, owns the durable idempotency
    # guard. Both executions preserve the exact same request identity.
    assert broker.handle(request("result", request_id=request_id))["status"] == "satisfied"
    assert broker.handle(request("cancel", request_id=request_id))["status"] == "accepted"
    assert broker.handle(request("observe", runtime_id="RUNTIME1"))["status"] == "observed"
    assert broker.handle(request("terminate", runtime_id="RUNTIME1",
                                 request_id="STOP-1"))["runtime_id"] == "RUNTIME1"
    assert control.stops == [("RUNTIME1", "STOP-1")]


def test_admission_rejects_missing_preparation_and_start_contract(scope):
    control, broker, spec = scope
    control.ready = False
    with pytest.raises(RoleBrokerError, match="not ready"):
        broker.handle(request("start", request_id="LAUNCH1"))
    assert not control.starts
    control.ready = True
    spec.start_policy = ApplicationStartPolicy.KEEP_RUNNING
    with pytest.raises(RoleBrokerError, match="unsupported"):
        broker.handle(request("start", request_id="LAUNCH1"))
    spec.start_policy = ApplicationStartPolicy.EXTERNALLY_CONTROLLED
    spec.execution_identity = None
    with pytest.raises(RoleBrokerError, match="unsupported"):
        broker.handle(request("start", request_id="LAUNCH1"))


def test_withdrawal_survives_removed_application_declaration(scope, monkeypatch):
    control, broker, _spec = scope
    monkeypatch.setattr("zog.box_control.specification.discover_applications",
                        lambda _: {})
    assert broker.handle(request("available")) == {"available": False}
    # Removing the spec must never obstruct a recorded runtime's cancellation.
    assert broker.handle(request("cancel", request_id="LAUNCH1"))["status"] == "accepted"
    assert broker.handle(request("terminate", request_id="STOP1",
                                 runtime_id="RUNTIME1"))["runtime_id"] == "RUNTIME1"
    with pytest.raises(RoleBrokerError):
        broker.handle(request("start", request_id="LAUNCH1"))


@pytest.mark.parametrize("operation,payload", [
    ("start", {"request_id": "X", "application": "station-access"}),
    ("start", {"request_id": "X", "command": ["/bin/sh"]}),
    ("terminate", {"runtime_id": "OTHER1", "request_id": "STOP1"}),
    ("observe", {"runtime_id": "OTHER1"}),
    ("cancel", {"request_id": "bad/../request"}),
    ("issue", {"uid": 0}),
    ("available", {"project": "/root/other"}),
])
def test_arbitrary_project_process_other_app_or_shell_is_not_authorized(scope, operation, payload):
    control, broker, _spec = scope
    with pytest.raises(RoleBrokerError):
        broker.handle(request(operation, **payload))
    assert not control.starts and not control.stops


def test_result_of_other_application_is_not_exposed(scope):
    control, broker, _spec = scope
    control.application_request_result = lambda _: SimpleNamespace(
        operation="launch", application="station-access",
        status=SimpleNamespace(value="satisfied"), runtime_id="OTHER1")
    with pytest.raises(RoleBrokerError, match="another application"):
        broker.handle(request("result", request_id="LAUNCH1"))


def test_protocol_rejects_duplicate_keys_and_oversized_payload():
    a, b = socket.socketpair()
    try:
        payload = b'{"schema":"x","schema":"x","operation":"issue"}'
        a.sendall(struct.pack("!I", len(payload)) + payload)
        with pytest.raises(RoleBrokerError, match="duplicate"):
            _decode(b)
    finally:
        a.close()
        b.close()
    with pytest.raises(RoleBrokerError, match="limit"):
        _json_bytes({"x": "a" * 5000}, 4096)


def test_peer_credentials_are_checked_before_request_parse(scope, monkeypatch):
    _control, broker, _spec = scope
    first, second = socket.socketpair()
    try:
        monkeypatch.setattr("zog.box_control.role_broker._peer_uid", lambda conn: 501)
        # An unauthorized client does not get to submit an issue request.
        respond_one(broker, second)
        assert first.recv(1) == b""
    finally:
        first.close()


def test_authenticated_one_shot_request_and_no_mutation_on_bad_input(scope, monkeypatch):
    control, broker, _spec = scope
    monkeypatch.setattr("zog.box_control.role_broker._peer_uid", lambda conn: 992)
    a, b = socket.socketpair()
    try:
        a.sendall(_json_bytes(request("issue"), 4096))
        respond_one(broker, b)
        raw_size = struct.unpack("!I", a.recv(4))[0]
        body = json.loads(a.recv(raw_size))
        assert body["ok"] and body["application"] == "archive-mirror"
        assert body["result"]["request_id"] == "ID1"
    finally:
        a.close()
    a, b = socket.socketpair()
    try:
        a.sendall(_json_bytes(request("issue", unit="systemd-foreign.service"), 4096))
        respond_one(broker, b)
        raw_size = struct.unpack("!I", a.recv(4))[0]
        assert json.loads(a.recv(raw_size)) == {
            "ok": False, "error": "blocked", "code": "blocked"
        }
        assert control.issued == 1
    finally:
        a.close()


def test_client_requires_an_immutable_application_scope(tmp_path):
    with pytest.raises(ValueError):
        RoleBrokerClient(tmp_path / "rpc.sock", application="../../etc")
    client = RoleBrokerClient(tmp_path / "rpc.sock", application="archive-mirror")
    assert client.application == "archive-mirror"
    with pytest.raises(RoleBrokerError, match="scope"):
        client.launch_application("station-access", request_id="REQ1")
    with pytest.raises(RoleBrokerError, match="scope"):
        client.cancel_application_launch("station-access", request_id="REQ1")



def test_reviewed_immutable_generation_is_selected_without_mutable_active_link(tmp_path, monkeypatch):
    from zog.box_control.role_broker import FixedPublishedImage
    import zog.box_control.images as images
    calls = []
    def selection(project, generation):
        calls.append((project, generation))
        return SimpleNamespace(generation=generation, reused=True)
    monkeypatch.setattr(images, "published_selection", selection)
    chosen = "a" * 64
    provider = FixedPublishedImage(chosen)
    project = SimpleNamespace(path=tmp_path)
    assert provider.ensure(project).generation == chosen
    assert provider.preview(project).generation == chosen
    assert calls == [(tmp_path, chosen), (tmp_path, chosen)]
    with pytest.raises(RoleBrokerError, match="exact published image"):
        FixedPublishedImage("active")
    with pytest.raises(RoleBrokerError, match="exact published image"):
        FixedPublishedImage("../image")


def test_pinned_software_view_prevents_unreviewed_app_change(scope):
    control, _broker, spec = scope
    desired = "sha256:" + "f" * 64
    spec.software_view = desired
    broker = RoleBroker(control, "archive-mirror", authorized_uid=992,
                        expected_software_view=desired)
    assert broker.handle(request("available")) == {"available": True}
    spec.software_view = "sha256:" + "e" * 64
    assert broker.handle(request("available")) == {"available": False}
    with pytest.raises(RoleBrokerError, match="unsupported"):
        broker.handle(request("start", request_id="REQ1"))
    assert not control.starts
    with pytest.raises(RoleBrokerError, match="exact published software view"):
        RoleBroker(control, "archive-mirror", authorized_uid=992,
                   expected_software_view="untrusted")



def test_observation_redacts_environment_host_bindings_and_commands(scope):
    control, broker, _spec = scope
    secret = "SECRET-RPC-PRIVATE-KEY"
    def observe(runtime_id):
        return {
            "runtime_id": runtime_id, "status": "observed",
            "persisted_state": "running", "cleanup_pending": False,
            "programs": [
                {"program": "server", "status": "observed",
                 "recorded_invocation_id": "a" * 32,
                 "observation": {
                     "active_state": "active", "sub_state": "running",
                     "result": "success", "invocation_id": "a" * 32,
                     "properties": [
                         ["Environment", ["TOKEN=" + secret]],
                         ["BindPaths", [["/root/sensitive", "/data"]]],
                         ["ExecStart", ["/bin/command", "--password", secret]],
                     ],
                 }}
            ],
        }
    control.observe_application_runtime = observe
    result = broker.handle(request("observe", runtime_id="RUNTIME1"))
    payload = json.dumps(result)
    assert result["status"] == "observed"
    assert result["programs"][0]["observation"]["active_state"] == "active"
    assert secret not in payload
    assert "/root/sensitive" not in payload
    assert "ExecStart" not in payload and "Environment" not in payload
    assert "BindPaths" not in payload


def test_preparation_failure_returns_safe_blocked_code(scope, monkeypatch):
    from zog.box_control.role_broker import respond_one
    control, broker, _spec = scope
    control.ready = False
    monkeypatch.setattr("zog.box_control.role_broker._peer_uid", lambda conn: 992)
    client, server = socket.socketpair()
    try:
        client.sendall(_json_bytes(request("start", request_id="R1"), 4096))
        respond_one(broker, server)
        length = struct.unpack("!I", client.recv(4))[0]
        result = json.loads(client.recv(length))
        assert result == {
            "ok": False, "error": "blocked", "code": "preparation-not-ready"
        }
        assert not control.starts
    finally:
        client.close()



def test_preflight_exposes_blocked_check_codes_without_messages_or_secrets(scope):
    control, broker, _spec = scope
    def advisory(application):
        assert application == "archive-mirror"
        return {
            "status": "blocked", "checks": [
                {"code": "mount-policy", "status": "blocked",
                 "message": "/root/secret/mount/actual-private-credential"},
                {"code": "application-preparation", "status": "ready",
                 "message": "secret configuration detail"},
            ],
        }
    control.preflight_application_launch = advisory
    result = broker.handle(request("preflight"))
    assert result == {
        "status": "blocked", "advisory": True, "checks": [
            {"code": "mount-policy", "status": "blocked"},
            {"code": "application-preparation", "status": "ready"},
        ],
    }
    assert "secret" not in json.dumps(result)
    assert not control.starts


def test_preflight_rejects_malformed_non_authoritative_status(scope):
    control, broker, _spec = scope
    control.preflight_application_launch = lambda application: {
        "status": "ready", "checks": []
    }
    with pytest.raises(RoleBrokerError, match="preflight status"):
        broker.handle(request("preflight"))



def test_long_launch_does_not_block_independent_read_only_role_observation(scope, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    control, broker, _spec = scope
    monkeypatch.setattr("zog.box_control.role_broker._peer_uid", lambda conn: 992)
    entered, release = Event(), Event()
    original = control.launch_application
    def slow_start(application, *, request_id):
        entered.set()
        assert release.wait(5), "test launch was not released"
        return original(application, request_id=request_id)
    control.launch_application = slow_start

    def call(request_data):
        client, server = socket.socketpair()
        try:
            client.sendall(_json_bytes(request_data, 4096))
            respond_one(broker, server)
            length = struct.unpack("!I", client.recv(4))[0]
            return json.loads(client.recv(length))
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(call, request("start", request_id="START1"))
        assert entered.wait(5), "test launch was not entered"
        try:
            observed = pool.submit(call, request("observe", runtime_id="RUNTIME1"))
            result = observed.result(timeout=5)
            assert result["ok"] is True
            assert result["result"]["status"] == "observed"
        finally:
            release.set()
        assert future.result(timeout=5)["ok"] is True
