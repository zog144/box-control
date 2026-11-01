"""Exact-head host-deploy/box-control authority race: source-only candidate.

Intentionally not discovered by the accepted default suite. CI checks out the
exact PR #3 host-deploy source and explicitly invokes this module. Socket
transport and systemd activation are simulated; both real project classes
perform their own independent admission checks.
"""
import pytest
from zog.host_deploy.certificate_issuer_lifecycle import SelectedIssuerController
from zog.host_deploy.certificate_renewal import RenewalBlocked
from zog.box_control.role_broker import SCHEMA
from test_issuer_broker_revocation_admission import make_broker


@pytest.mark.parametrize("program", ("renew", "inspect", "stage"))
def test_client_verifies_then_broker_refuses_revocation_before_launch(
        monkeypatch, program):
    state = {"revoked": False, "client_checks": 0, "server_checks": 0}

    def client_check():
        state["client_checks"] += 1
        return not state["revoked"]

    def server_check(*, authorized_uid):
        assert authorized_uid == 23002
        state["server_checks"] += 1
        return not state["revoked"]

    broker, ctl = make_broker(monkeypatch, server_check)

    class RealSelectedWireFixture:
        def __init__(self, path, *, controller_uid):
            assert path == "/run/zog/certify-lifecycle.sock"
            assert controller_uid == 23001

        def launch_application(self, application, *, request_id, program_name):
            # A root-owner revocation lands after SelectedIssuerController
            # checked its protected projection but before the fixed broker
            # admits this exact request.
            state["revoked"] = True
            return broker.handle({
                "schema": SCHEMA, "operation": "start",
                "program": program_name, "request_id": request_id,
            })

    client = SelectedIssuerController(
        "/run/zog/certify-lifecycle.sock",
        controller_uid=23001,
        client_factory=RealSelectedWireFixture,
        authority_guard=client_check,
    )
    with pytest.raises(RenewalBlocked, match="issuer-lifecycle-launch-uncertain"):
        client.request_application_launch(
            "certify-manage", program_name=program,
            request_id="AUTHORITY-REQ-1",
        )
    assert state["client_checks"] == 2  # construction + immediate RPC guard
    assert state["server_checks"] == 1
    assert ctl.calls == []  # no preparation, launch, journal or cancellation
