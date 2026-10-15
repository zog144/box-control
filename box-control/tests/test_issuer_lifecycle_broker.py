"""Certificate renew/stage lifecycle is authenticated and selection-pinned."""
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


@pytest.mark.parametrize("program", ("renew", "stage"))
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


@pytest.mark.parametrize("program", ("issue", "inspect", "recover", "shell", "", None))
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
    for program in ("issue", "inspect", "recover"):
        with pytest.raises(RoleBrokerError):
            client.launch_application(
                "certify-manage", request_id="SIGNED-ID-123",
                program_name=program,
            )
