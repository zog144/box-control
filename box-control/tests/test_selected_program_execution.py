"""Durable one-program launch selection must not become an all-program launch."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from zog.box_control.errors import ConfigurationError, LifecycleOperationPending, RuntimeOperationError
from zog.box_control.model import ApplicationSpec, ApplicationStartPolicy, ProgramSpec, ReconcileReport
from zog.box_control.project import Project
from zog.box_control.reconcile import Reconciler
from zog.box_control.requests import (
    ApplicationRequest, ApplicationRequestOperation, ApplicationRequestResult,
    ApplicationRequestStatus, ApplicationRequestStore,
)


def app():
    programs = tuple(ProgramSpec(name=name, command=("/bin/echo", name))
                     for name in ("issue", "renew", "inspect"))
    return ApplicationSpec(
        name="certify-manage", programs=programs,
        persistent=True, execution_identity="application",
        start_policy=ApplicationStartPolicy.EXTERNALLY_CONTROLLED,
        writable_mounts=("certificate-data",),
    )


def reconciler(tmp_path):
    p = Project(tmp_path / "project")
    p.state_dir.mkdir(parents=True, exist_ok=True)
    p.require_state_allowed = lambda: None
    r = Reconciler(
        p, image_provider=object(),
        systemd_runtime=SimpleNamespace(transport=object()),
        boot_id_provider=lambda: "boot-1",
    )
    return r


def test_request_selector_is_durable_and_cannot_be_reused_for_another_program(tmp_path):
    store = ApplicationRequestStore(tmp_path / "q", tmp_path / "r")
    request = ApplicationRequest("REQ1", ApplicationRequestOperation.LAUNCH,
                                 "certify-manage", created_at=1234.0,
                                 program_name="renew")
    saved_path = store.submit(request)
    assert store.load(saved_path) == request
    assert store.submit(request) == saved_path
    with pytest.raises(RuntimeOperationError, match="already used"):
        store.submit(replace(request, program_name="issue"))
    result = ApplicationRequestResult(
        "REQ1", ApplicationRequestOperation.LAUNCH, ApplicationRequestStatus.SATISFIED,
        application="certify-manage", runtime_id="ABC123", program_name="renew",
    )
    store.save_result(result)
    assert store.result("REQ1").program_name == "renew"
    assert store.submit(request) == store.result_dir / "REQ1.json"
    with pytest.raises(RuntimeOperationError, match="already used"):
        store.submit(replace(request, program_name="inspect"))


@pytest.mark.parametrize("chosen", ["issue", "renew", "inspect"])
def test_bound_single_program_keeps_original_storage_and_grant(tmp_path, monkeypatch, chosen):
    from zog.box_control.application_storage import ApplicationStorage
    from zog.box_control import application_software
    r = reconciler(tmp_path)
    application = app()
    grant = {"schema": "zog-execution-identity-v1", "uid": 200002, "gid": 200002,
             "project": str(r.project.path), "application": application.name}
    application = replace(application, execution_binding=grant, storage_id="a" * 32)
    monkeypatch.setattr(ApplicationStorage, "bind", lambda _self, value, _gen: value)
    monkeypatch.setattr(application_software, "resolve_binding", lambda _p, value, _s: value)
    observed = []
    monkeypatch.setattr(r.operations, "run", lambda **values: observed.append(values) or "accepted")
    closure = SimpleNamespace(resolution_for=lambda _: SimpleNamespace(runtime_fingerprint="fingerprint"))
    selection = SimpleNamespace(generation="generation", root=tmp_path)
    result = r._launch(
        application, selection=selection, closure=closure, references={},
        instance_id=application.name, request_id="ID123", replaces_runtime_id=None,
        boot_id="boot-1", selected_program=chosen,
    )
    assert result == "accepted"
    snapshot = observed[0]["application"]
    assert [p.name for p in snapshot.programs] == [chosen]
    assert snapshot.selected_program == chosen
    assert snapshot.execution_binding == grant
    assert snapshot.storage_id == "a" * 32
    assert observed[0]["selected_program"] == chosen
    assert observed[0]["request_id"] == "ID123"


def test_unselected_multi_program_certificate_launch_fails_closed(tmp_path, monkeypatch):
    from zog.box_control.application_storage import ApplicationStorage
    from zog.box_control import application_software
    r = reconciler(tmp_path)
    monkeypatch.setattr(ApplicationStorage, "bind", lambda _self, value, _gen: value)
    monkeypatch.setattr(application_software, "resolve_binding", lambda _p, value, _s: value)
    closure = SimpleNamespace(resolution_for=lambda _: SimpleNamespace(runtime_fingerprint="x"))
    with pytest.raises(ConfigurationError, match="exact program selection"):
        r._launch(app(), selection=SimpleNamespace(generation="g", root=tmp_path),
                  closure=closure, references={}, instance_id="certify-manage",
                  request_id="REQ1", replaces_runtime_id=None, boot_id="boot-1")


def test_unknown_program_does_not_execute(tmp_path, monkeypatch):
    from zog.box_control.application_storage import ApplicationStorage
    from zog.box_control import application_software
    r = reconciler(tmp_path)
    monkeypatch.setattr(ApplicationStorage, "bind", lambda _self, value, _gen: value)
    monkeypatch.setattr(application_software, "resolve_binding", lambda _p, value, _s: value)
    closure = SimpleNamespace(resolution_for=lambda _: SimpleNamespace(runtime_fingerprint="x"))
    with pytest.raises(ConfigurationError, match="does not declare"):
        r._launch(app(), selection=SimpleNamespace(generation="g", root=tmp_path),
                  closure=closure, references={}, instance_id="certify-manage",
                  request_id="REQ1", replaces_runtime_id=None, boot_id="boot-1",
                  selected_program="nonexistent")


def test_selected_launch_refuses_existing_or_uncertain_runtime(tmp_path, monkeypatch):
    r = reconciler(tmp_path)
    monkeypatch.setattr("zog.box_control.workspaces.Workspaces.resolve",
                        lambda _self, application, *_args, **_kwargs: application)
    live = SimpleNamespace(runtime_id="EXIST1", workspace_binding=None)
    monkeypatch.setattr(r, "_live_references", lambda references, **kwargs: [live])
    with pytest.raises(LifecycleOperationPending, match="conflicts"):
        r._launch_application_loaded(
            "certify-manage", applications={"certify-manage": app()},
            closure=None, references={}, selection=None, boot_id="boot-1",
            request_id="REQ1", program_name="renew",
        )


def test_selected_launch_requires_original_request_identity(tmp_path):
    r = reconciler(tmp_path)
    with pytest.raises(ConfigurationError, match="durable request ID"):
        r._launch_application_loaded(
            "certify-manage", applications={"certify-manage": app()},
            closure=None, references={}, selection=None, boot_id="boot-1",
            program_name="renew",
        )


def test_selector_rejects_wrong_app_policy(tmp_path, monkeypatch):
    r = reconciler(tmp_path)
    monkeypatch.setattr("zog.box_control.workspaces.Workspaces.resolve",
                        lambda _self, application, *_args, **_kwargs: application)
    unmanaged = replace(app(), execution_identity=None)
    with pytest.raises(ConfigurationError, match="managed singleton"):
        r._launch_application_loaded(
            "certify-manage", applications={"certify-manage": unmanaged},
            closure=None, references={}, selection=None, boot_id="boot-1",
            request_id="REQ1", program_name="renew",
        )



def test_queued_evaluator_executes_only_selected_program_and_publishes_selector(tmp_path, monkeypatch):
    r = reconciler(tmp_path)
    request = ApplicationRequest(
        "QUEUE1", ApplicationRequestOperation.LAUNCH, "certify-manage",
        created_at=1234.0, program_name="inspect",
    )
    r.requests.submit(request)
    captured = []
    def selected(_name, **arguments):
        captured.append(arguments)
        return SimpleNamespace(
            runtime_id="EXACT1", application="certify-manage",
            replaces_runtime_id=None,
        )
    monkeypatch.setattr(r, "_launch_application_loaded", selected)
    report = ReconcileReport(ok=True)
    r._process_requests(
        applications={"certify-manage": app()}, closure=None,
        references={}, selection=None, boot_id="boot-1", report=report,
    )
    assert report.ok
    assert len(captured) == 1
    assert captured[0]["program_name"] == "inspect"
    assert captured[0]["request_id"] == "QUEUE1"
    assert r.requests.result("QUEUE1").program_name == "inspect"
    assert r.requests.result("QUEUE1").status == ApplicationRequestStatus.SATISFIED
    assert not r.requests.paths()


def test_queued_selector_conflicting_with_saved_result_blocks_without_execution(tmp_path, monkeypatch):
    from zog.box_control.errors import RecoveryRequired
    r = reconciler(tmp_path)
    r.requests.submit(ApplicationRequest(
        "QUEUE1", ApplicationRequestOperation.LAUNCH, "certify-manage",
        created_at=1234.0, program_name="renew",
    ))
    r.requests.save_result(ApplicationRequestResult(
        "QUEUE1", ApplicationRequestOperation.LAUNCH, ApplicationRequestStatus.SATISFIED,
        application="certify-manage", runtime_id="OLD1",
        program_name="issue",
    ))
    monkeypatch.setattr(r, "_launch_application_loaded",
                        lambda *args, **kwargs: pytest.fail("must not execute"))
    with pytest.raises(RecoveryRequired, match="contradicts"):
        r._process_requests(
            applications={"certify-manage": app()}, closure=None,
            references={}, selection=None, boot_id="boot-1",
            report=ReconcileReport(ok=True),
        )
    assert r.requests.paths()
