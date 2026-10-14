import json
import pytest
from pathlib import Path
from types import SimpleNamespace

from zog.box_control.project import Project
from zog.box_control.reconcile import Reconciler
from zog.box_control.runtime.systemd import SystemdServiceRuntime, SystemdUnitObservation


class FakeImageProvider:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.calls = 0

    def ensure(self, project):
        self.calls += 1
        return SimpleNamespace(
            generation="generation-1",
            root=self.root,
            manifest={
                "schema": 2,
                "fingerprint": "generation-1",
                "packages": ["hello-package"],
                "distribution_packages": [],
                "package_details": [],
            },
            previous_manifest=None,
            reused=False,
        )


class FakeSystemdTransport:
    def __init__(self):
        self.units = {}
        self.started_slices = []
        self.started_services = []
        self.kills = []
        self.stops = []
        self._counter = 0

    def version(self):
        return 257

    def start_slice(self, *, project_root, slice_name, description):
        self.started_slices.append(slice_name)

    def start_service(self, *, project_root, generation, definition):
        self._counter += 1
        self.started_services.append(definition.unit_name)
        self.units[definition.unit_name] = SystemdUnitObservation(
            unit_name=definition.unit_name,
            exists=True,
            transient=True,
            invocation_id=f"invocation-{self._counter}",
            active_state="active",
            sub_state="running",
            result="success",
            main_pid=1000 + self._counter,
            control_group=f"/{definition.unit_name}",
            properties=definition.expected_properties(),
        )

    def observe(self, *, project_root, unit_name):
        return self.units.get(
            unit_name, SystemdUnitObservation(unit_name=unit_name, exists=False)
        )

    def kill(self, *, project_root, unit_name, signal_number):
        self.kills.append((unit_name, signal_number))
        if unit_name in self.units:
            old = self.units[unit_name]
            self.units[unit_name] = SystemdUnitObservation(
                unit_name=unit_name, exists=True, transient=True,
                invocation_id=old.invocation_id, active_state="inactive", sub_state="dead",
                result="success", properties=old.properties,
            )

    def stop(self, *, project_root, unit_name):
        self.stops.append(unit_name)
        self.units.pop(unit_name, None)

    def operation_budget(self, timeout_seconds):
        from contextlib import nullcontext
        return nullcontext()

    def release(self, *, project_root, unit_name):
        pass

    def reset_failed(self, *, project_root, unit_name):
        pass


def _write_keep_running(project):
    project.application_dir.mkdir(parents=True)
    definition = project.application_dir / "hello"
    definition.mkdir()
    (definition / "application.py").write_text(
        'application(name="hello", start_policy="keep-running", programs=(program(name="main", command=("/bin/true",)),))',
        encoding="utf-8",
    )


def test_reconcile_launches_keep_running_application_through_systemd(tmp_path):
    project = Project(tmp_path / "project")
    _write_keep_running(project)
    project.state_dir.mkdir(parents=True)
    project.require_state_allowed = lambda: None

    provider = FakeImageProvider(tmp_path / "image-root")
    transport = FakeSystemdTransport()
    report = Reconciler(
        project,
        image_provider=provider,
        systemd_runtime=SystemdServiceRuntime(project, transport),
        boot_id_provider=lambda: "boot-1",
    ).reconcile()

    assert report.ok, report.errors
    assert provider.calls == 1
    assert len(transport.started_slices) == 1
    assert len(transport.started_services) == 1
    assert any("started 1 program service" in item for item in report.messages)
    refs = json.loads(project.runtime_reference_file.read_text(encoding="utf-8"))
    record = next(iter(refs["runtimes"].values()))
    assert record["state"] == "running"
    assert record["instance_id"] == "hello"
    assert record["boot_id"] == "boot-1"
    assert record["programs"][0]["invocation_id"] == "invocation-1"

    state = json.loads(project.state_file.read_text(encoding="utf-8"))
    assert len(state["applications"]["hello"]["runtime_ids"]) == 1
    assert state["rootfs"]["packages"] == ["hello-package"]


def test_second_evaluation_observes_existing_runtime_without_relaunch(tmp_path):
    project = Project(tmp_path / "project")
    _write_keep_running(project)
    project.state_dir.mkdir(parents=True)
    project.require_state_allowed = lambda: None
    provider = FakeImageProvider(tmp_path / "image-root")
    transport = FakeSystemdTransport()
    runtime = SystemdServiceRuntime(project, transport)
    reconciler = Reconciler(
        project,
        image_provider=provider,
        systemd_runtime=runtime,
        boot_id_provider=lambda: "boot-1",
    )

    assert reconciler.reconcile().ok
    assert reconciler.reconcile().ok
    assert len(transport.started_services) == 1



def _queue_boot_request(project, request_id, *, created_at=1.0):
    from zog.box_control.requests import ApplicationRequest, ApplicationRequestOperation, ApplicationRequestStore
    store = ApplicationRequestStore(project.application_request_dir, project.application_request_result_dir)
    store.submit(ApplicationRequest(request_id=request_id,
        operation=ApplicationRequestOperation.LAUNCH, application="hello",
        created_at=created_at))
    return store


def test_queued_boot_launch_precedes_keep_running_policy_without_replacement(tmp_path):
    project = Project(tmp_path / "project")
    _write_keep_running(project)
    project.state_dir.mkdir(parents=True)
    project.require_state_allowed = lambda: None
    store = _queue_boot_request(project, "BOOT-LAUNCH1")
    transport = FakeSystemdTransport()
    runtime = SystemdServiceRuntime(project, transport)
    reconciler = Reconciler(project, image_provider=FakeImageProvider(tmp_path / "image-root"),
        systemd_runtime=runtime, boot_id_provider=lambda: "boot-1")
    first = reconciler.reconcile()
    assert first.ok, first.errors
    assert len(transport.started_services) == 1
    assert len(transport.stops) == 0
    assert any("takes precedence" in message for message in first.messages)
    result = store.result("BOOT-LAUNCH1")
    assert result.status.value == "satisfied"
    references = reconciler.references.load()
    assert references[result.runtime_id].request_id == "BOOT-LAUNCH1"
    second = reconciler.reconcile()
    assert second.ok, second.errors
    assert len(transport.started_services) == 1
    assert not store.paths()


def test_interrupted_boot_policy_start_satisfies_earlier_queued_intent(tmp_path):
    project = Project(tmp_path / "project")
    _write_keep_running(project)
    project.state_dir.mkdir(parents=True)
    project.require_state_allowed = lambda: None
    transport = FakeSystemdTransport()
    runtime = SystemdServiceRuntime(project, transport)
    reconciler = Reconciler(project, image_provider=FakeImageProvider(tmp_path / "image-root"),
        systemd_runtime=runtime, boot_id_provider=lambda: "boot-1")
    first = reconciler.reconcile()
    assert first.ok, first.errors
    first_runtime = next(iter(reconciler.references.load()))
    store = _queue_boot_request(project, "BOOT-QUEUED1", created_at=1.0)
    second = reconciler.reconcile()
    assert second.ok, second.errors
    assert len(transport.started_services) == 1
    assert transport.stops == []
    assert store.result("BOOT-QUEUED1").runtime_id == first_runtime
    assert any("matched committed boot-policy runtime" in x for x in second.messages)


def test_later_explicit_launch_preserves_replacement_semantics(tmp_path):
    import time
    project = Project(tmp_path / "project")
    _write_keep_running(project)
    project.state_dir.mkdir(parents=True)
    project.require_state_allowed = lambda: None
    transport = FakeSystemdTransport()
    reconciler = Reconciler(project, image_provider=FakeImageProvider(tmp_path / "image-root"),
        systemd_runtime=SystemdServiceRuntime(project, transport),
        boot_id_provider=lambda: "boot-1")
    assert reconciler.reconcile().ok
    _queue_boot_request(project, "LATER-EXPLICIT", created_at=time.time())
    from zog.box_control.errors import RecoveryRequired
    with pytest.raises(RecoveryRequired, match="waiting for cleanup"):
        reconciler.reconcile()
    # This fake transport cannot complete teardown. Nonetheless the saved
    # operation proves the later request is a replacement, not absorbed as a
    # boot request; the controller must not launch its successor prematurely.
    operation = next(record for record in reconciler.operations.store.records()
                     if record["request_id"] == "LATER-EXPLICIT")
    assert operation["stop_runtime_ids"]
    assert operation["phase"] == "stopping"
    assert len(transport.started_services) == 1



def _interrupted_finished_evaluation(tmp_path):
    from zog.box_control.durability import replace_json
    project = Project(tmp_path / "project")
    _write_keep_running(project)
    project.state_dir.mkdir(parents=True)
    project.require_state_allowed = lambda: None
    transport = FakeSystemdTransport()
    provider = FakeImageProvider(tmp_path / "image-root")
    reconciler = Reconciler(project, image_provider=provider,
        systemd_runtime=SystemdServiceRuntime(project, transport),
        boot_id_provider=lambda: "boot-1")
    report = reconciler.reconcile()
    assert report.ok, report.errors
    marker = project.state_dir / "mutation-incomplete.json"
    replace_json(marker, {"schema": 1, "status": "mutation-incomplete"})
    from zog.box_control.api import BoxControl
    return project, BoxControl(project, image_provider=provider,
        systemd_transport=transport, boot_id_provider=lambda: "boot-1"), reconciler, transport


def test_interrupted_schema_one_session_requires_explicit_audited_recovery(tmp_path):
    project, control, reconciler, transport = _interrupted_finished_evaluation(tmp_path)
    from zog.box_control.errors import RecoveryRequired
    from zog.box_control.durability import mutation_guard
    marker = project.state_dir / "mutation-incomplete.json"
    assert control.recovery_status().mutation_status == "recovery-required"
    with pytest.raises(RecoveryRequired):
        with mutation_guard(project):
            raise AssertionError("must stay blocked")
    result = control.recover_interrupted_session()
    assert result["safety"] == "persisted-record-consistency-only"
    assert result["kernel_readiness"] == "not-established"
    assert not result["resumed"]
    assert result["operation_ids"]
    assert not marker.exists()
    audit = project.state_dir / "session-recovery" / (result["recovery_id"] + ".json")
    assert json.loads(audit.read_text())["kind"] == "reviewed-interrupted-session-recovery"
    assert control.evaluate().ok
    assert len(transport.started_services) == 1


def test_interrupted_session_recovery_refuses_mismatched_operation_reference(tmp_path):
    from dataclasses import replace
    from zog.box_control.errors import RecoveryRequired
    project, control, reconciler, _transport = _interrupted_finished_evaluation(tmp_path)
    refs = reconciler.references.load()
    runtime_id = next(iter(refs))
    refs[runtime_id] = replace(refs[runtime_id], generation="foreign-generation")
    reconciler.references.save(refs)
    marker = project.state_dir / "mutation-incomplete.json"
    with pytest.raises(RecoveryRequired, match="runtime identity"):
        control.recover_interrupted_session()
    assert marker.exists()
    assert not list((project.state_dir / "session-recovery").glob("*.json"))


def test_interrupted_session_recovery_refuses_unfinished_or_unrecorded_work(tmp_path):
    from zog.box_control.errors import RecoveryRequired
    from zog.box_control.durability import replace_json
    project, control, reconciler, _transport = _interrupted_finished_evaluation(tmp_path)
    record = reconciler.operations.store.records()[0]
    record["finished"] = False
    reconciler.operations.store.save(record)
    with pytest.raises(RecoveryRequired, match="unfinished"):
        control.recover_interrupted_session()
    assert (project.state_dir / "mutation-incomplete.json").exists()
    record["finished"] = True
    reconciler.operations.store.save(record)
    replace_json(project.state_dir / "retention-incomplete.json",
                 {"schema": 1, "deletions": []})
    with pytest.raises(RecoveryRequired, match="another persistence"):
        control.recover_interrupted_session()


def test_interrupted_session_recovery_refuses_result_mismatch(tmp_path):
    from zog.box_control.durability import replace_json
    from zog.box_control.errors import RecoveryRequired
    project = Project(tmp_path / "project")
    _write_keep_running(project)
    project.state_dir.mkdir(parents=True)
    project.require_state_allowed = lambda: None
    _queue_boot_request(project, "BOOT-INTENT", created_at=1.0)
    transport = FakeSystemdTransport()
    reconciler = Reconciler(project, image_provider=FakeImageProvider(tmp_path / "image-root"),
        systemd_runtime=SystemdServiceRuntime(project, transport),
        boot_id_provider=lambda: "boot-1")
    assert reconciler.reconcile().ok
    from zog.box_control.api import BoxControl
    control = BoxControl(project, boot_id_provider=lambda: "boot-1")
    marker = project.state_dir / "mutation-incomplete.json"
    replace_json(marker, {"schema": 1, "status": "mutation-incomplete"})
    path = project.application_request_result_dir / "BOOT-INTENT.json"
    raw = json.loads(path.read_text())
    raw["status"] = "failed"
    replace_json(path, raw)
    with pytest.raises(RecoveryRequired, match="request result contradicts"):
        control.recover_interrupted_session()
    assert marker.exists()


def test_final_unlink_barrier_failure_leaves_resumable_audit_interlock(tmp_path, monkeypatch):
    from zog.box_control import durability, operations
    from zog.box_control.errors import RecoveryRequired, PersistenceError
    project, control, _reconciler, _transport = _interrupted_finished_evaluation(tmp_path)
    original_remove = operations.remove_file
    pending = project.state_dir / "session-recovery-pending.json"
    def fail_after_marker_unlink(path):
        if path == pending:
            raise PersistenceError("injected final removal failure")
        return original_remove(path)
    monkeypatch.setattr(operations, "remove_file", fail_after_marker_unlink)
    with pytest.raises(PersistenceError):
        control.recover_interrupted_session()
    assert pending.exists()
    assert not (project.state_dir / "mutation-incomplete.json").exists()
    status = control.recovery_status()
    assert status.mutation_status == "recovery-required"
    assert any(f.code == "session-recovery-incomplete" for f in status.faults)
    with pytest.raises(RecoveryRequired):
        control.request_application_launch("hello")
    monkeypatch.setattr(operations, "remove_file", original_remove)
    # A new process loses the volatile storage failure latch but still sees
    # the durable two-phase recovery pointer.
    durability._blocked_projects.discard(project.path)
    recovered = control.recover_interrupted_session()
    assert recovered["resumed"]
    assert not pending.exists()
