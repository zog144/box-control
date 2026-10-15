"""Authenticated read-only issuer broker exposes a fixed, sanitized request view."""
import json
import socket
import struct
from types import SimpleNamespace

import pytest

from zog.box_control.issuer_observer import IssuerObserver, _sanitize, respond_one
from zog.box_control.issuer_observer_client import IssuerObserverClient
from zog.box_control.role_broker import SCHEMA, RoleBrokerError, _json_bytes


def sample(request_id="REQ1"):
    return {
        "schema": "zog-trusted-issuer-observation-v1",
        "request_id": request_id,
        "runtime_id": "ABCD23",
        "program": "renew",
        "storage_id": "a" * 32,
        "grant_id": "b" * 32,
        "issuer_uid": 200001,
        "issuer_gid": 200001,
        "policy_sha256": "c" * 64,
        "host_policy_digest": "sha256:" + "d" * 64,
        "certificate_id": "example",
        "certificate_fingerprint_sha256": "e" * 64,
        "journal_updated_at": "2026-10-15T12:00:00+00:00",
        "outcome": "renewed",
        "status": "journal-ready",
        "tls_ready": False,
    }


def test_observer_only_calls_trusted_controller_evidence_method():
    called = []
    observer = IssuerObserver(SimpleNamespace(observe_issuer_request=lambda rid:
        called.append(rid) or sample(rid)), authorized_uid=9901)
    result = observer.handle({
        "schema": SCHEMA, "operation": "observe-issuer", "request_id": "REQ1",
    })
    assert called == ["REQ1"]
    assert result == sample()


@pytest.mark.parametrize("bad", [
    {"schema": SCHEMA, "operation": "start", "request_id": "REQ1"},
    {"schema": SCHEMA, "operation": "observe-issuer", "request_id": "REQ1", "program": "issue"},
    {"schema": SCHEMA, "operation": "observe-issuer", "request_id": "../etc/passwd"},
])
def test_observer_refuses_mutation_selection_and_arbitrary_paths(bad):
    observer = IssuerObserver(SimpleNamespace(observe_issuer_request=lambda rid:
        pytest.fail("must not inspect")), authorized_uid=9901)
    with pytest.raises(RoleBrokerError):
        observer.handle(bad)


@pytest.mark.parametrize("field,value", [
    ("request_id", "other"),
    ("status", "verified-and-serving"),
    ("tls_ready", True),
    ("certificate_fingerprint_sha256", "/" * 100),
    ("issuer_uid", 0),
])
def test_observer_does_not_echo_untrusted_privileged_evidence(field, value):
    raw = dict(sample(), **{field: value})
    with pytest.raises(RoleBrokerError):
        _sanitize(raw, "REQ1")


def test_peer_unauthorized_cannot_read_private_issuer_data(monkeypatch):
    import zog.box_control.issuer_observer as module
    monkeypatch.setattr(module, "_peer_uid", lambda _sock: 9000)
    observer = IssuerObserver(SimpleNamespace(observe_issuer_request=lambda rid:
        pytest.fail("untrusted peer reached source")), authorized_uid=9901)
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(_json_bytes({
            "schema": SCHEMA, "operation": "observe-issuer", "request_id": "REQ1"
        }, 4096))
        respond_one(observer, receiver)
        sender.settimeout(1)
        assert sender.recv(1) == b""
    finally:
        sender.close()


def test_peer_authenticated_receives_only_sanitized_secret_free_data(monkeypatch):
    import zog.box_control.issuer_observer as module
    monkeypatch.setattr(module, "_peer_uid", lambda _sock: 9901)
    observer = IssuerObserver(SimpleNamespace(observe_issuer_request=lambda rid:
        sample(rid)), authorized_uid=9901)
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(_json_bytes({
            "schema": SCHEMA, "operation": "observe-issuer", "request_id": "REQ1"
        }, 4096))
        respond_one(observer, receiver)
        length = struct.unpack("!I", sender.recv(4))[0]
        raw = sender.recv(length)
        payload = json.loads(raw)
        assert payload["ok"] is True
        assert payload["result"]["status"] == "journal-ready"
        assert "private_key" not in raw.decode()
        assert payload["result"]["tls_ready"] is False
    finally:
        sender.close()
