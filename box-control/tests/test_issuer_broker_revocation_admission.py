"""Selected issuer revocation between client check and server admission.

Source-only: never connects to the protected socket, executes Certbot, or
publishes to /etc/STATE. The root-only fixture uses a temporary directory.
"""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from zog.box_control import issuer_broker_authority as authority
from zog.box_control.issuer_lifecycle_broker import IssuerLifecycleBroker
from zog.box_control.model import ApplicationStartPolicy
from zog.box_control.role_broker import RoleBrokerError, SCHEMA


def projection():
    return {
        "schema": authority.SCHEMA, "host_id": "1" * 36,
        "installation_id": "2" * 36, "state_volume_id": "3" * 36,
        "certificate_id": "station-web", "hostnames": ["example.test"],
        "policy_sha256": "a" * 64,
        "host_policy_digest": "sha256:" + "b" * 64,
        "issuer_lifecycle_socket": "/run/zog/certify-lifecycle.sock",
        "controller_uid": 23001, "caller_uid": 23002,
        "issuer_uid": 24001, "issuer_gid": 24002,
        "storage_id": "c" * 32, "grant_id": "d" * 32,
        "box_control_revision": "e" * 40,
        "consumer_binding_sha256": "sha256:" + "f" * 64,
    }


def make_broker(monkeypatch, checker):
    from zog.box_control import specification
    monkeypatch.setattr(
        specification, "discover_applications",
        lambda path: {"certify-manage": SimpleNamespace(
            start_policy=ApplicationStartPolicy.EXTERNALLY_CONTROLLED,
            persistent=True, multiple_instances=False,
            execution_identity="application", software_view="sha256:" + "b"*64,
            programs=tuple(SimpleNamespace(name=name)
                           for name in ("renew", "inspect", "stage")),
        )},
    )

    class Control:
        def __init__(self):
            self.project = SimpleNamespace(application_dir="/inert")
            self.calls = []
        def application_preparation(self, application):
            self.calls.append(("preparation", application))
            return {"state": "ready", "storage_id": "c" * 32}
        def application_request_result(self, request_id):
            self.calls.append(("result", request_id))
            return None
        def issue_application_request_id(self):
            self.calls.append(("issue",))
            return "AUTHORITY-REQ-1"
        def launch_application(self, application, *, request_id, program_name):
            self.calls.append(("launch", program_name))
            return SimpleNamespace(application=application, request_id=request_id,
                runtime_id="RUNTIME1", programs=(SimpleNamespace(program=program_name),))
    ctl = Control()
    return IssuerLifecycleBroker(
        ctl, authorized_uid=23002, software_view="sha256:"+"b"*64,
        authority_guard=checker,
    ), ctl


@pytest.mark.parametrize("operation,fields", [
    ("start", {"request_id": "AUTHORITY-REQ-1", "program": "renew"}),
    ("start", {"request_id": "AUTHORITY-REQ-1", "program": "inspect"}),
    ("start", {"request_id": "AUTHORITY-REQ-1", "program": "stage"}),
    ("issue", {}),
    ("available", {}),
    ("preparation", {}),
    ("result", {"request_id": "AUTHORITY-REQ-1"}),
    ("cancel", {"request_id": "AUTHORITY-REQ-1"}),
    ("observe", {"runtime_id": "RUNTIME1"}),
    ("inspect-result", {"request_id": "AUTHORITY-REQ-1"}),
])
def test_client_passes_then_root_revoke_before_broker_admission_denied(
        monkeypatch, operation, fields):
    # Simulate host-deploy's earlier successful verification, then a root
    # revocation before the independently authenticated socket is dispatched.
    state = {"revoked": False, "checks": 0}
    def guarded(*, authorized_uid):
        assert authorized_uid == 23002
        state["checks"] += 1
        return not state["revoked"]
    broker, ctl = make_broker(monkeypatch, guarded)
    assert guarded(authorized_uid=23002) is True
    state["revoked"] = True
    with pytest.raises(RoleBrokerError) as error:
        broker.handle({"schema": SCHEMA, "operation": operation, **fields})
    assert error.value.code == "authority-revoked"
    assert ctl.calls == []


@pytest.mark.parametrize("program", ["renew", "inspect", "stage"])
def test_revoked_during_preparation_is_denied_before_any_mutation(monkeypatch, program):
    state = {"checks": 0, "revoked": False}
    def guard(*, authorized_uid):
        state["checks"] += 1
        return not state["revoked"]
    broker, ctl = make_broker(monkeypatch, guard)
    original = ctl.application_preparation
    def revoke_after_preparation(application):
        result = original(application)
        state["revoked"] = True
        return result
    ctl.application_preparation = revoke_after_preparation
    with pytest.raises(RoleBrokerError) as error:
        broker.handle({"schema": SCHEMA, "operation": "start",
                       "request_id": "AUTHORITY-REQ-1", "program": program})
    assert error.value.code == "authority-revoked"
    assert state["checks"] == 2
    assert not any(call[0] == "launch" for call in ctl.calls)


@pytest.mark.parametrize("mutation", [
    {"controller_uid": 23002}, {"caller_uid": 23001},
    {"controller_uid": True}, {"caller_uid": "23002"},
    {"box_control_revision": "../foreign"}, {"schema": "untrusted"},
    {"secret": "bad"},
])
def test_broker_admission_requires_installed_identity(monkeypatch, mutation):
    candidate = {**projection(), **mutation}
    monkeypatch.setattr(authority, "_root_projection", lambda: candidate)
    monkeypatch.setattr(authority.os, "geteuid", lambda: 23001)
    with pytest.raises(RoleBrokerError, match="unavailable or revoked"):
        authority.require_active_authority(authorized_uid=23002)


def test_active_fixed_projection_can_admit_at_broker_only(monkeypatch):
    monkeypatch.setattr(authority, "_root_projection", projection)
    monkeypatch.setattr(authority.os, "geteuid", lambda: 23001)
    assert authority.require_active_authority(authorized_uid=23002) is True
    with pytest.raises(RoleBrokerError):
        authority.require_active_authority(authorized_uid=9999)


@pytest.mark.skipif(os.geteuid() != 0, reason="ephemeral root-owned source-only fixture")
def test_root_file_revocation_pending_tampering_and_read_race(tmp_path, monkeypatch):
    root = tmp_path / "protected"
    root.mkdir(mode=0o700)
    pin = root / Path(authority.ROOT_PATH).name
    pin.write_text(json.dumps(projection(), sort_keys=True))
    pin.chmod(0o444)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        assert authority._read_authority_in_parent(fd) == projection()
        for marker in (authority.REVOCATION_NAME,
                       authority.PENDING_REVOCATION_NAME):
            tombstone = root / marker
            tombstone.write_bytes(b"unfinished-revocation")
            with pytest.raises(RoleBrokerError) as error:
                authority._read_authority_in_parent(fd)
            assert error.value.code == "authority-revoked"
            tombstone.unlink()
        # A marker created between the two protected checks is also fatal.
        original = authority._no_revocation
        checks = [0]
        def install_between_checks(parent):
            checks[0] += 1
            if checks[0] == 2:
                (root / authority.PENDING_REVOCATION_NAME).write_bytes(b"partial")
            return original(parent)
        with monkeypatch.context() as patcher:
            patcher.setattr(authority, "_no_revocation", install_between_checks)
            with pytest.raises(RoleBrokerError):
                authority._read_authority_in_parent(fd)
        (root / authority.PENDING_REVOCATION_NAME).unlink()
        pin.chmod(0o644)
        with pytest.raises(RoleBrokerError):
            authority._read_authority_in_parent(fd)
        pin.chmod(0o444)
        pin.unlink()
        pin.symlink_to("/etc/passwd")
        with pytest.raises(RoleBrokerError):
            authority._read_authority_in_parent(fd)
    finally:
        os.close(fd)
