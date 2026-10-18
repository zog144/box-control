"""Certificate renew/inspect/stage lifecycle is authenticated and selection-pinned."""
from types import SimpleNamespace

import pytest

from zog.box_control.issuer_lifecycle_broker import IssuerLifecycleBroker
from zog.box_control.issuer_lifecycle_client import IssuerLifecycleClient
from zog.box_control.role_broker import RoleBrokerError, SCHEMA
from zog.box_control.model import ApplicationStartPolicy


class Control:
    def __init__(self):
        self.project = SimpleNamespace(application_dir="/inert")
        self.started = []
        self.request_status = {}
        self.preparation = {"state": "ready", "storage_id": "a" * 32}

    def application_preparation(self, application):
        assert application == "certify-manage"
        return self.preparation

    def issue_application_request_id(self):
        return "SIGNED-ID-123"

    def application_request_result(self, request_id):
        return self.request_status.get(request_id)

    def launch_application(self, application, *, request_id, program_name):
        self.started.append((application, request_id, program_name))
        return SimpleNamespace(
            application=application, runtime_id="RUNTIME1",
            programs=(SimpleNamespace(program=program_name),),
        )


def spec():
    return SimpleNamespace(
        start_policy=ApplicationStartPolicy.EXTERNALLY_CONTROLLED,
        persistent=True, multiple_instances=False,
        execution_identity="application", software_view="sha256:" + "b" * 64,
        programs=(SimpleNamespace(name="issue"), SimpleNamespace(name="renew"),
                  SimpleNamespace(name="stage"), SimpleNamespace(name="inspect")),
    )


def broker(monkeypatch):
    from zog.box_control import specification
    monkeypatch.setattr(
        specification, "discover_applications",
        lambda directory: {"certify-manage": spec()},
    )
    ctl = Control()
    return IssuerLifecycleBroker(
        ctl, authorized_uid=99001, software_view="sha256:" + "b" * 64,
    ), ctl


def request(method, **kwargs):
    return {"schema": SCHEMA, "operation": method, **kwargs}


@pytest.mark.parametrize("program", ("renew", "stage", "inspect"))
def test_only_exact_selected_issuer_operation_can_be_started(monkeypatch, program):
    b, ctl = broker(monkeypatch)
    result = b.handle(request(
        "start", request_id="SIGNED-ID-123", program=program,
    ))
    assert result == {
        "request_id": "SIGNED-ID-123",
        "runtime_id": "RUNTIME1", "program": program,
    }
    assert ctl.started == [("certify-manage", "SIGNED-ID-123", program)]


@pytest.mark.parametrize("program", ("issue", "recover", "shell", "", None))
def test_other_programs_cannot_be_triggered(monkeypatch, program):
    b, ctl = broker(monkeypatch)
    with pytest.raises(RoleBrokerError):
        b.handle(request("start", request_id="SIGNED-ID-123", program=program))
    assert ctl.started == []


@pytest.mark.parametrize("operation,fields", [
    ("start", {"request_id": "SIGNED-ID-123", "program": "stage",
               "command": "/bin/sh"}),
    ("start", {"request_id": "SIGNED-ID-123", "program": "renew",
               "policy": "/tmp/bad"}),
    ("evaluate", {}),
    ("terminate", {"runtime_id": "RUNTIME1", "request_id": "OTHER"}),
    ("start", {"request_id": "../secret", "program": "renew"}),
])
def test_broker_does_not_accept_paths_or_generic_controller_commands(monkeypatch, operation, fields):
    b, ctl = broker(monkeypatch)
    with pytest.raises(RoleBrokerError):
        b.handle(request(operation, **fields))
    assert ctl.started == []


def test_existing_request_for_another_program_not_reinterpreted(monkeypatch):
    b, ctl = broker(monkeypatch)
    ctl.request_status["SIGNED-ID-123"] = SimpleNamespace(
        application="certify-manage", program_name="renew",
        operation=SimpleNamespace(value="launch"),
        status=SimpleNamespace(value="satisfied"), runtime_id="OLD",
    )
    with pytest.raises(RoleBrokerError, match="bound"):
        b.handle(request("start", request_id="SIGNED-ID-123", program="stage"))
    assert ctl.started == []


def test_nonready_preparation_blocks_before_launch(monkeypatch):
    b, ctl = broker(monkeypatch)
    ctl.preparation = {"state": "uncertain"}
    with pytest.raises(RoleBrokerError) as exc:
        b.handle(request("start", request_id="SIGNED-ID-123", program="renew"))
    assert exc.value.code == "preparation-not-ready"
    assert ctl.started == []


def test_result_redacts_exception_text_and_binds_program(monkeypatch):
    b, ctl = broker(monkeypatch)
    ctl.request_status["SIGNED-ID-123"] = SimpleNamespace(
        application="certify-manage", program_name="stage",
        operation=SimpleNamespace(value="launch"),
        status=SimpleNamespace(value="satisfied"), runtime_id="RUNTIME1",
        error="KEY=topsecret"
    )
    assert b.handle(request("result", request_id="SIGNED-ID-123")) == {
        "request_id": "SIGNED-ID-123", "status": "satisfied",
        "program": "stage", "runtime_id": "RUNTIME1",
    }


def test_host_deploy_client_will_not_bypass_local_program_policy():
    client = IssuerLifecycleClient("/run/zog/issuer-lifecycle.sock")
    for program in ("issue", "recover"):
        with pytest.raises(RoleBrokerError):
            client.launch_application(
                "certify-manage", request_id="SIGNED-ID-123",
                program_name=program,
            )

def inspection_evidence(request_id="SIGNED-ID-123"):
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    return {
        "schema": "zog-trusted-issuer-inspection-v1",
        "request_id": request_id, "runtime_id": "RUNTIME1",
        "program": "inspect", "boot_id": "b" * 32,
        "invocation_id": "c" * 32,
        "storage_id": "a" * 32, "grant_id": "d" * 32,
        "issuer_uid": 200001, "issuer_gid": 200001,
        "host_policy_digest": "sha256:" + "e" * 64,
        "status": "verified", "tls_ready": False,
        "inspection": {
            "schema": "zog-certify-inspection-result-v1",
            "certificate_id": "example",
            "hostnames": ["example.test"],
            "policy_sha256": "f" * 64,
            "health": "valid",
            "certificate_fingerprint_sha256": "1" * 64,
            "valid_from": (now-timedelta(days=1)).isoformat(),
            "expires_at": (now+timedelta(days=60)).isoformat(),
            "days_remaining": 60, "operation": "renew", "outcome": "renewed",
            "operation_updated_at": now.isoformat(), "issuer_verified": True,
            "observed_at": now.isoformat(), "tls_ready": False,
        },
    }


def test_inspection_result_requires_satisfied_exact_selected_inspect(monkeypatch):
    b, ctl = broker(monkeypatch)
    ctl.request_status["SIGNED-ID-123"] = SimpleNamespace(
        application="certify-manage", program_name="inspect",
        operation=SimpleNamespace(value="launch"),
        status=SimpleNamespace(value="satisfied"), runtime_id="RUNTIME1",
    )
    calls = []
    ctl.inspect_issuer_result = lambda req: calls.append(req) or inspection_evidence(req)
    assert b.handle(request("inspect-result", request_id="SIGNED-ID-123"))["status"] == "verified"
    assert calls == ["SIGNED-ID-123"]
    for other in ("renew", "stage", "issue"):
        ctl.request_status["SIGNED-ID-123"].program_name = other
        with pytest.raises(RoleBrokerError):
            b.handle(request("inspect-result", request_id="SIGNED-ID-123"))
    assert calls == ["SIGNED-ID-123"]


def test_inspection_result_rejects_missing_pending_and_hostile_arguments(monkeypatch):
    b, ctl = broker(monkeypatch)
    for payload in (
        request("inspect-result", request_id="SIGNED-ID-123"),
        request("inspect-result", request_id="SIGNED-ID-123", path="/var/log/journal"),
        request("inspect-result", request_id="SIGNED-ID-123", invocation_id="forged"),
    ):
        with pytest.raises(RoleBrokerError):
            b.handle(payload)
    ctl.request_status["SIGNED-ID-123"] = SimpleNamespace(
        application="certify-manage", program_name="inspect",
        operation=SimpleNamespace(value="launch"),
        status=SimpleNamespace(value="pending"), runtime_id="RUNTIME1",
    )
    with pytest.raises(RoleBrokerError):
        b.handle(request("inspect-result", request_id="SIGNED-ID-123"))


@pytest.mark.parametrize("mutation", [
    "wrong-request", "wrong-invocation", "unsanitized", "wrong-cert",
    "wrong-health", "wrong-uid", "status-forged", "wrong-tls",
])
def test_inspection_evidence_schema_fails_closed(monkeypatch, mutation):
    from zog.box_control.issuer_inspection_evidence import sanitize_issuer_inspection
    data = inspection_evidence()
    if mutation == "wrong-request": data["request_id"] = "OTHER"
    if mutation == "wrong-invocation": data["invocation_id"] = "../bad"
    if mutation == "unsanitized": data["private_key"] = "SECRET"
    if mutation == "wrong-cert": data["inspection"]["certificate_id"] = "INVALID"
    if mutation == "wrong-health": data["inspection"]["health"] = "unknown"
    if mutation == "wrong-uid": data["issuer_uid"] = True
    if mutation == "status-forged": data["inspection"]["issuer_verified"] = False
    if mutation == "wrong-tls": data["tls_ready"] = True
    with pytest.raises(RoleBrokerError):
        sanitize_issuer_inspection(data, "SIGNED-ID-123")


def test_host_deploy_inspection_client_is_bound_to_fixed_result_schema(monkeypatch):
    from zog.box_control.issuer_inspection_evidence import sanitize_issuer_inspection
    client = IssuerLifecycleClient("/run/zog/issuer-lifecycle.sock")
    monkeypatch.setattr(client, "_call", lambda op, **kwargs: inspection_evidence(kwargs["request_id"]))
    assert client.inspect_issuer_result("SIGNED-ID-123")["status"] == "verified"
    monkeypatch.setattr(client, "_call", lambda op, **kwargs: {"status": "verified"})
    with pytest.raises(RoleBrokerError):
        client.inspect_issuer_result("SIGNED-ID-123")
