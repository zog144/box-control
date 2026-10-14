from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from zog.box_control.api import BoxControl
from zog.box_control.errors import ConfigurationError, RuntimeOperationError
from zog.box_control.model import ApplicationRuntimeState
from zog.box_control.project import Project
from zog.box_control.requests import ApplicationRequestStatus
from zog.box_control.runtime.systemd import SystemdUnitObservation


class FakeImageProvider:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        # Published fixture image contains its data mount targets.
        for target in ("var/lib/browser", "data", "python", "tmp/.X11-unix"):
            (self.root / target).mkdir(parents=True, exist_ok=True)

    def ensure(self, project):
        return SimpleNamespace(
            generation="generation-1",
            root=self.root,
            manifest={
                "fingerprint": "generation-1",
                "packages": [],
                "distribution_packages": [],
            },
            previous_manifest=None,
            reused=True,
        )


class FakeTransport:
    def __init__(self):
        self.units = {}
        self.started = []
        self.counter = 0

    def version(self):
        return 257

    def start_slice(self, *, project_root, slice_name, description):
        pass

    def start_service(self, *, project_root, generation, definition):
        self.counter += 1
        self.started.append(definition.unit_name)
        self.units[definition.unit_name] = SystemdUnitObservation(
            unit_name=definition.unit_name,
            exists=True,
            transient=True,
            invocation_id=f"invocation-{self.counter}",
            active_state="active",
            sub_state="running",
            result="success",
            main_pid=1000 + self.counter,
            control_group=f"/{definition.unit_name}",
            properties=definition.expected_properties(),
        )

    def observe(self, *, project_root, unit_name):
        return self.units.get(
            unit_name, SystemdUnitObservation(unit_name=unit_name, exists=False)
        )

    def kill(self, *, project_root, unit_name, signal_number):
        if unit_name in self.units:
            self.units[unit_name] = replace(
                self.units[unit_name],
                active_state="inactive",
                sub_state="dead",
                main_pid=None,
            )

    def stop(self, *, project_root, unit_name):
        self.units.pop(unit_name, None)

    def operation_budget(self, timeout_seconds):
        from contextlib import nullcontext
        return nullcontext()

    def release(self, *, project_root, unit_name):
        pass

    def reset_failed(self, *, project_root, unit_name):
        pass

    def release_runtime_root(self, *, project_root, runtime_id):
        pass


class DelayedStopTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.allow_stop = False

    def kill(self, *, project_root, unit_name, signal_number):
        if self.allow_stop:
            super().kill(
                project_root=project_root,
                unit_name=unit_name,
                signal_number=signal_number,
            )

    def stop(self, *, project_root, unit_name):
        if self.allow_stop:
            super().stop(project_root=project_root, unit_name=unit_name)


def write_application(
    project, *, start_policy="externally-controlled", multiple_instances=False
):
    directory = project.application_dir / "desktop"
    directory.mkdir(parents=True)
    (directory / "application.py").write_text(
        "application(name=\"desktop\", "
        f"start_policy=\"{start_policy}\", "
        f"multiple_instances={multiple_instances!r}, "
        "programs=(program(name=\"browser\", "
        "command=(\"/usr/bin/browser\",)),))",
        encoding="utf-8",
    )


def control_for(project, tmp_path, transport, boot):
    project.require_state_allowed = lambda: None
    return BoxControl(
        project,
        image_provider=FakeImageProvider(tmp_path / "root"),
        systemd_transport=transport,
        boot_id_provider=lambda: boot[0],
    )


def test_start_once_per_boot_records_attempt_and_starts_again_on_new_boot(tmp_path):
    project = Project(tmp_path / "project")
    write_application(project, start_policy="start-once-per-boot")
    transport = FakeTransport()
    boot = ["boot-1"]
    control = control_for(project, tmp_path, transport, boot)

    assert control.evaluate().ok
    first = control.current_application_runtimes("desktop")[0]
    control.terminate_application_runtime(first.runtime_id)
    assert control.evaluate().ok
    assert len(transport.started) == 1

    boot[0] = "boot-2"
    assert control.evaluate().ok
    assert len(transport.started) == 2
    second = control.current_application_runtimes("desktop")[0]
    assert second.boot_id == "boot-2"


def test_restart_preserves_instance_identity_but_creates_new_runtime(tmp_path):
    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=True)
    control = control_for(project, tmp_path, FakeTransport(), ["boot-1"])

    first = control.launch_application("desktop")
    restarted = control.restart_application_runtime(first.runtime_id)

    assert restarted.runtime_id != first.runtime_id
    assert restarted.instance_id == first.instance_id
    assert restarted.replaces_runtime_id == first.runtime_id
    assert len(control.application_instance_runtimes(first.instance_id)) == 2
    assert control.application_runtime(first.runtime_id).state == ApplicationRuntimeState.TERMINATED
    with pytest.raises(ConfigurationError, match="no longer current"):
        control.terminate_application_runtime(first.runtime_id)


def test_request_file_launch_is_correlated_and_idempotent(tmp_path):
    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=True)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ["boot-1"])

    request = control.request_application_launch(
        "desktop", request_id=issued(control, 'REQUEST-0001')
    )
    assert control.evaluate().ok
    result = control.application_request_result(request.request_id)
    assert result.status == ApplicationRequestStatus.SATISFIED
    reference = control.application_runtime(result.runtime_id)
    assert reference.request_id == request.request_id
    assert not (project.application_request_dir / (issued(control, 'REQUEST-0001') + '.json')).exists()

    control.request_application_launch("desktop", request_id=issued(control, 'REQUEST-0001'))
    assert control.evaluate().ok
    assert len(transport.started) == 1
    with pytest.raises(RuntimeOperationError, match="already used"):
        control.request_application_termination(
            reference.runtime_id, request_id=issued(control, 'REQUEST-0001')
        )


def test_pending_singleton_replacement_retains_replaced_runtime_identity(tmp_path):
    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=False)
    transport = DelayedStopTransport()
    control = control_for(project, tmp_path, transport, ["boot-1"])
    first = control.launch_application("desktop")
    request = control.request_application_launch(
        "desktop", request_id=issued(control, 'REQUEST-REPLACE')
    )

    assert not control.evaluate().ok
    assert control.application_request_result(request.request_id) is None
    pending = next(record for record in control.application_operations() if record["request_id"] == request.request_id)
    assert first.runtime_id in pending['stop_runtime_ids']

    transport.allow_stop = True
    assert control.evaluate().ok
    satisfied = control.application_request_result(request.request_id)
    replacement = control.application_runtime(satisfied.runtime_id)
    assert satisfied.status == ApplicationRequestStatus.SATISFIED
    assert replacement.replaces_runtime_id == first.runtime_id


def test_current_and_completed_history_are_separate_views(tmp_path):
    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=True)
    control = control_for(project, tmp_path, FakeTransport(), ["boot-1"])

    first = control.launch_application("desktop")
    second = control.launch_application("desktop")
    control.terminate_application_runtime(first.runtime_id)

    assert [item.runtime_id for item in control.current_application_runtimes()] == [
        second.runtime_id
    ]
    assert [item.runtime_id for item in control.application_runtime_history()] == [
        first.runtime_id
    ]


@pytest.mark.parametrize("delay_cleanup", [False, True])
def test_failed_queued_launch_records_failed_result_and_runtime(tmp_path, delay_cleanup):
    class FailedTransport(DelayedStopTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            if self.counter == 2:
                unit = kwargs["definition"].unit_name
                self.units[unit] = replace(
                    self.units[unit], active_state="failed", sub_state="failed",
                    result="exit-code", main_pid=None,
                )

    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=True)
    (project.application_dir / "desktop" / "application.py").write_text(
        'application(name="desktop", start_policy="externally-controlled", '
        'multiple_instances=True, programs=('
        'program(name="first", command=("/usr/bin/first",)), '
        'program(name="second", command=("/usr/bin/second",))))',
        encoding="utf-8",
    )
    transport = FailedTransport()
    transport.allow_stop = not delay_cleanup
    control = control_for(project, tmp_path, transport, ["boot-1"])
    request = control.request_application_launch("desktop", request_id=issued(control, 'FAILED-LAUNCH'))
    assert not control.evaluate().ok
    result = control.application_request_result(request.request_id)
    assert result.status == ApplicationRequestStatus.FAILED
    reference = control.application_runtime(result.runtime_id)
    assert reference.request_id == request.request_id
    assert reference.state == (
        ApplicationRuntimeState.TERMINATING if delay_cleanup else ApplicationRuntimeState.FAILED
    )
    assert "exit-code" in result.error
    transport.allow_stop = True
    control.request_application_launch("desktop", request_id=request.request_id)
    assert control.evaluate().ok
    assert len(transport.started) == 2
    assert control.application_request_result(request.request_id).status == ApplicationRequestStatus.FAILED
    assert control.application_runtime(reference.runtime_id).state == ApplicationRuntimeState.FAILED


def test_committed_launch_result_survives_later_runtime_failure(tmp_path):
    from zog.box_control.runtime.reference import RuntimeReferenceStore

    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=True)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ["boot-1"])
    reference = control.launch_application("desktop", request_id=issued(control, 'CRASHED-REQUEST'))
    store = RuntimeReferenceStore(project.runtime_reference_file)
    store.save({reference.runtime_id: replace(
        reference, state=ApplicationRuntimeState.FAILED, completed_at=1.0, error=None
    )})
    request = control.request_application_launch("desktop", request_id=issued(control, 'CRASHED-REQUEST'))
    assert control.evaluate().ok
    result = control.application_request_result(request.request_id)
    assert result.status == ApplicationRequestStatus.SATISFIED
    assert result.runtime_id == reference.runtime_id
    assert len(transport.started) == 1


def test_successful_short_lived_queued_launch_is_satisfied(tmp_path):
    class ShortTransport(FakeTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            unit = kwargs["definition"].unit_name
            self.units[unit] = replace(
                self.units[unit], active_state="inactive", sub_state="dead", main_pid=None
            )

    project = Project(tmp_path / "project")
    write_application(project)
    control = control_for(project, tmp_path, ShortTransport(), ["boot-1"])
    request = control.request_application_launch("desktop", request_id=issued(control, 'SHORT-RUN'))
    assert control.evaluate().ok
    result = control.application_request_result(request.request_id)
    assert result.status == ApplicationRequestStatus.SATISFIED
    assert control.application_runtime(result.runtime_id).state == ApplicationRuntimeState.TERMINATED


def test_mount_data_is_separate_for_concurrent_runtimes_and_restart(tmp_path):
    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=True)
    path = project.application_dir / "desktop" / "application.py"
    path.write_text(path.read_text().replace(
        'command=("/usr/bin/browser",)',
        'mounts={"data": "/var/lib/browser"}, command=("/usr/bin/browser",)'
    ), encoding="utf-8")
    control = control_for(project, tmp_path, FakeTransport(), ["boot-1"])
    first = control.launch_application("desktop")
    second = control.launch_application("desktop")

    def source(reference):
        binds = dict(reference.programs[0].expected_properties)["BindPaths"]
        assert binds[0][1] == "/var/lib/browser"
        return Path(binds[0][0])

    first_source = source(first)
    second_source = source(second)
    assert first_source == project.mounts_dir / "desktop" / first.runtime_id / "browser" / "data"
    (first_source / "marker").write_text("first runtime data", encoding="utf-8")
    assert not (second_source / "marker").exists()
    restarted = control.restart_application_runtime(first.runtime_id)
    assert restarted.instance_id == first.instance_id
    assert source(restarted) not in (first_source, second_source)
    assert not (source(restarted) / "marker").exists()
    assert (first_source / "marker").read_text() == "first runtime data"
    retried = control.launch_application("desktop", request_id=issued(control, 'SAME-REQUEST'))
    same = control.launch_application("desktop", request_id=issued(control, 'SAME-REQUEST'))
    assert source(same) == source(retried)


def test_replacement_waits_for_terminal_runtime_cleanup(tmp_path):
    from zog.box_control.errors import RecoveryRequired, RuntimeOperationError

    class RetainedTransport(FakeTransport):
        blocked = True
        def stop(self, **kwargs):
            if self.blocked:
                raise RuntimeOperationError("unit cleanup unavailable")
            return super().stop(**kwargs)

    project = Project(tmp_path / "project")
    write_application(project, multiple_instances=True)
    transport = RetainedTransport()
    control = control_for(project, tmp_path, transport, ["boot-1"])
    first = control.launch_application("desktop")
    unit = first.programs[0].unit_name
    transport.units[unit] = replace(transport.units[unit], active_state="inactive", sub_state="dead")
    with pytest.raises(RecoveryRequired):
        control.restart_application_runtime(first.runtime_id)
    assert len(transport.started) == 1
    assert control.application_runtime(first.runtime_id).cleanup_pending
    transport.blocked = False
    assert control.evaluate().ok
    replacement = control.current_application_runtimes("desktop")[0]
    assert replacement.runtime_id != first.runtime_id
    assert len(transport.started) == 2



def test_unexpected_replacement_interruption_preserves_mutation_block(tmp_path, monkeypatch):
    from zog.box_control.durability import _blocked_projects
    from zog.box_control.errors import RecoveryRequired
    from zog.box_control.runtime.systemd import SystemdServiceRuntime

    project = Project(tmp_path / "project")
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ["boot-1"])
    first = control.launch_application("desktop")
    request = control.request_application_launch("desktop", request_id=issued(control, 'INTERRUPTED-REPLACE'))
    interrupted = []

    def interrupt_before_launch(self, *args, **kwargs):
        saved = control.application_runtime(first.runtime_id)
        assert saved.state == ApplicationRuntimeState.TERMINATED
        assert not saved.cleanup_pending
        interrupted.append(first.runtime_id)
        raise RuntimeError("injected interruption after termination")

    monkeypatch.setattr(SystemdServiceRuntime, "launch", interrupt_before_launch)
    report = control.evaluate()
    assert not report.ok
    assert report.errors == ["unexpected error: injected interruption after termination"]
    assert interrupted == [first.runtime_id]
    assert len(transport.started) == 1
    assert control.application_request_result(request.request_id) is None
    assert (project.application_request_dir / f"{request.request_id}.json").exists()
    marker = project.state_dir / "mutation-incomplete.json"
    assert marker.exists()

    # Discard the process-local latch to verify that persisted evidence alone
    # blocks a fresh facade, as it would after restarting the controller.
    _blocked_projects.discard(project.path)
    fresh = control_for(Project(project.path), tmp_path, transport, ["boot-1"])
    with pytest.raises(RuntimeError, match="injected interruption"):
        fresh.request_application_launch("desktop", request_id=issued(control, 'NEXT-REQUEST'))
    with pytest.raises(RuntimeError, match="injected interruption"):
        fresh.launch_application("desktop")
    assert not fresh.evaluate().ok
    assert marker.exists()
    assert fresh.application_runtime(first.runtime_id).state == ApplicationRuntimeState.TERMINATED
    assert fresh.application_request_result(request.request_id) is None
    assert not (project.application_request_dir / (issued(control, 'NEXT-REQUEST') + '.json')).exists()
    assert len(transport.started) == 1


class SimulatedCrash(BaseException):
    pass


def operation_for(control, request):
    return next(record for record in control.application_operations() if record['request_id'] == request.request_id)


def test_recovery_after_stop_uses_frozen_definition_and_identity(tmp_path, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    first = control.launch_application('desktop')
    request = control.request_application_launch('desktop', request_id=issued(control, 'RECOVER-FROZEN'))
    original = SystemdServiceRuntime.launch

    def crash(self, *args, **kwargs):
        assert control.application_runtime(first.runtime_id).state == ApplicationRuntimeState.TERMINATED
        raise SimulatedCrash()

    monkeypatch.setattr(SystemdServiceRuntime, 'launch', crash)
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    prepared = operation_for(control, request)
    assert prepared['new_runtime_id'] != first.runtime_id
    definition = project.application_dir / 'desktop' / 'application.py'
    definition.write_text(definition.read_text().replace('/usr/bin/browser', '/usr/bin/changed'))
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', original)
    fresh = control_for(Project(project.path), tmp_path, transport, ['boot-1'])
    assert fresh.evaluate().ok
    result = fresh.application_request_result(request.request_id)
    assert result.runtime_id == prepared['new_runtime_id']
    recovered = fresh.application_runtime(result.runtime_id)
    assert recovered.programs[0].command == ('/usr/bin/browser',)
    assert recovered.replaces_runtime_id == first.runtime_id
    assert len(transport.started) == 2
    assert fresh.evaluate().ok
    assert len(transport.started) == 2


@pytest.mark.parametrize('lost_evidence', ['none', 'absent', 'reboot'])
def test_start_reply_interruption_never_repeats_started_member(tmp_path, lost_evidence):
    class InterruptedTransport(FakeTransport):
        crash = True
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            if self.crash:
                self.crash = False
                raise SimulatedCrash()

    project = Project(tmp_path / 'project')
    write_application(project)
    path = project.application_dir / 'desktop' / 'application.py'
    path.write_text('application(name="desktop", programs=(program(name="first", command=("/first",)), program(name="second", command=("/second",))))')
    transport = InterruptedTransport()
    boot = ['boot-1']
    control = control_for(project, tmp_path, transport, boot)
    request = control.request_application_launch('desktop', request_id=issued(control, 'PARTIAL-START'))
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    prepared = operation_for(control, request)
    if lost_evidence == 'absent':
        transport.units.clear()
    if lost_evidence == 'reboot':
        boot[0] = 'boot-2'
        transport.units.clear()
    fresh = control_for(Project(project.path), tmp_path, transport, boot)
    report = fresh.evaluate()
    if lost_evidence == 'none':
        assert report.ok, report.errors
        assert fresh.application_request_result(request.request_id).runtime_id == prepared['new_runtime_id']
        assert len(transport.started) == 2
        assert len(set(transport.started)) == 2
    else:
        assert not report.ok
        assert fresh.application_request_result(request.request_id) is None
        assert len(transport.started) == 1
        assert (project.state_dir / 'mutation-incomplete.json').exists()


def test_committed_operation_repairs_result_without_relaunch(tmp_path, monkeypatch):
    from zog.box_control.requests import ApplicationRequestStore
    from zog.box_control.errors import PersistenceError

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    request = control.request_application_launch('desktop', request_id=issued(control, 'COMMIT-REPAIR'))
    save = ApplicationRequestStore.save_result

    def fail(self, result):
        raise PersistenceError('result barrier failed')

    monkeypatch.setattr(ApplicationRequestStore, 'save_result', fail)
    assert not control.evaluate().ok
    record = operation_for(control, request)
    assert record['phase'] == 'committed'
    assert not record['published']
    monkeypatch.setattr(ApplicationRequestStore, 'save_result', save)
    assert control.evaluate().ok
    assert control.application_request_result(request.request_id).status == ApplicationRequestStatus.SATISFIED
    assert len(transport.started) == 1
    assert not (project.application_request_dir / f'{request.request_id}.json').exists()


def test_operation_repairs_interrupted_reference_projection(tmp_path, monkeypatch):
    from zog.box_control.runtime.reference import RuntimeReferenceStore
    from zog.box_control.errors import PersistenceError

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    request = control.request_application_launch('desktop', request_id=issued(control, 'REFERENCE-REPAIR'))
    save = RuntimeReferenceStore.save

    def fail(self, references):
        if any(reference.state == ApplicationRuntimeState.RUNNING for reference in references.values()):
            raise PersistenceError('reference barrier failed')
        save(self, references)

    monkeypatch.setattr(RuntimeReferenceStore, 'save', fail)
    assert not control.evaluate().ok
    record = operation_for(control, request)
    assert record['references']['runtimes'][record['new_runtime_id']]['state'] == 'running'
    assert control.application_runtime(record['new_runtime_id']).state == ApplicationRuntimeState.STARTING
    monkeypatch.setattr(RuntimeReferenceStore, 'save', save)
    assert control.evaluate().ok
    assert control.application_runtime(record['new_runtime_id']).state == ApplicationRuntimeState.RUNNING
    assert len(transport.started) == 1


def test_abandon_uncertain_start_records_unknown_outcome_and_cleans(tmp_path):
    class InterruptedTransport(FakeTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            raise SimulatedCrash()

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = InterruptedTransport()
    boot = ['boot-1']
    control = control_for(project, tmp_path, transport, boot)
    request = control.request_application_launch('desktop', request_id=issued(control, 'ABANDON-START'))
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    record = operation_for(control, request)
    boot[0] = 'boot-2'
    transport.units.clear()
    assert not control.evaluate().ok
    abandoned = control.abandon_application_operation(record['operation_id'])
    assert abandoned['finished']
    result = control.application_request_result(request.request_id)
    assert result.status == ApplicationRequestStatus.ABANDONED
    assert result.error == 'abandoned; original outcome unknown'
    assert len(transport.started) == 1
    assert not (project.state_dir / 'mutation-incomplete.json').exists()
    control.request_application_launch('desktop', request_id=request.request_id)
    assert control.evaluate().ok
    assert len(transport.started) == 1


def test_missing_prepared_rootfs_blocks_before_replacement_start(tmp_path, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime
    import shutil

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    request = control.request_application_launch('desktop', request_id=issued(control, 'MISSING-ROOTFS'))
    original = SystemdServiceRuntime.launch
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', lambda *args, **kwargs: (_ for _ in ()).throw(SimulatedCrash()))
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    shutil.rmtree(tmp_path / 'root')
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', original)
    assert not control.evaluate().ok
    assert len(transport.started) == 0
    assert control.application_request_result(request.request_id) is None


def test_unexplained_reference_contradiction_blocks_recovery(tmp_path, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime
    from zog.box_control.runtime.reference import RuntimeReferenceStore

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    request = control.request_application_launch('desktop', request_id=issued(control, 'CONTRADICTION'))
    original = SystemdServiceRuntime.launch
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', lambda *a, **k: (_ for _ in ()).throw(SimulatedCrash()))
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    store = RuntimeReferenceStore(project.runtime_reference_file)
    references = store.load()
    record = operation_for(control, request)
    identity = record['new_runtime_id']
    references[identity] = replace(references[identity], generation='foreign-generation')
    store.save(references)
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', original)
    report = control.evaluate()
    assert not report.ok
    assert 'contradicts' in report.errors[0]
    assert not transport.started
    assert store.load()[identity].generation == 'foreign-generation'


def test_saved_failure_after_reboot_is_cleaned_without_relaunch(tmp_path, monkeypatch):
    from zog.box_control.runtime.reference import RuntimeReferenceStore

    class FailedTransport(FakeTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            unit = kwargs['definition'].unit_name
            self.units[unit] = replace(self.units[unit], active_state='failed', result='exit-code')

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FailedTransport()
    boot = ['boot-1']
    control = control_for(project, tmp_path, transport, boot)
    request = control.request_application_launch('desktop', request_id=issued(control, 'KNOWN-FAILURE'))
    original = RuntimeReferenceStore.save

    def crash(self, references):
        original(self, references)
        if any(program.result == 'exit-code' for reference in references.values() for program in reference.programs):
            raise SimulatedCrash()

    monkeypatch.setattr(RuntimeReferenceStore, 'save', crash)
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    monkeypatch.setattr(RuntimeReferenceStore, 'save', original)
    boot[0] = 'boot-2'
    transport.units.clear()
    assert control.evaluate().ok
    result = control.application_request_result(request.request_id)
    assert result.status == ApplicationRequestStatus.FAILED
    reference = control.application_runtime(result.runtime_id)
    assert reference.programs[0].result == 'exit-code'
    assert not reference.cleanup_pending
    assert len(transport.started) == 1


def test_operation_retry_survives_pruned_runtime_history(tmp_path):
    from zog.box_control.runtime.reference import RuntimeReferenceStore

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    first = control.launch_application('desktop', request_id=issued(control, 'DURABLE-RETRY'))
    control.terminate_application_runtime(first.runtime_id)
    RuntimeReferenceStore(project.runtime_reference_file).save({})
    retried = control.launch_application('desktop', request_id=issued(control, 'DURABLE-RETRY'))
    assert retried.runtime_id == first.runtime_id
    assert len(transport.started) == 1
    with pytest.raises(RuntimeOperationError, match='different intent'):
        control.launch_application('another', request_id=issued(control, 'DURABLE-RETRY'))


def test_incomplete_preparation_without_record_recovers_without_stopping_peer(tmp_path, monkeypatch):
    from zog.box_control.operations import OperationStore

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    first = control.launch_application('desktop')
    request = control.request_application_launch('desktop', request_id=issued(control, 'PREPARATION-CRASH'))
    save = OperationStore.save

    def crash(self, record):
        if record['request_id'] == request.request_id:
            raise SimulatedCrash()
        save(self, record)

    monkeypatch.setattr(OperationStore, 'save', crash)
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    assert control.application_runtime(first.runtime_id).state == ApplicationRuntimeState.RUNNING
    assert len(transport.started) == 1
    monkeypatch.setattr(OperationStore, 'save', save)
    assert control.evaluate().ok
    assert len(transport.started) == 2


def test_abandonment_retains_protection_until_cleanup_finishes(tmp_path):
    from zog.box_control.errors import RecoveryRequired

    class InterruptedTransport(DelayedStopTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            raise SimulatedCrash()

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = InterruptedTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    request = control.request_application_launch('desktop', request_id=issued(control, 'ABANDON-CLEANUP'))
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    record = operation_for(control, request)
    abandoned = control.abandon_application_operation(record['operation_id'])
    assert not abandoned['finished']
    reference = control.application_runtime(record['new_runtime_id'])
    assert reference.cleanup_pending
    assert reference.error == 'abandoned; original outcome unknown'
    assert control.application_request_result(request.request_id).status == ApplicationRequestStatus.ABANDONED
    with pytest.raises(RecoveryRequired):
        control.request_application_launch('desktop', request_id=issued(control, 'BLOCKED-BY-CLEANUP'))
    transport.allow_stop = True
    assert control.evaluate().ok
    assert operation_for(control, request)['finished']
    assert len(transport.started) == 1


def test_termination_recovers_after_signal_without_signalling_again(tmp_path):
    class InterruptedTransport(FakeTransport):
        signals = 0
        def kill(self, **kwargs):
            super().kill(**kwargs)
            self.signals += 1
            raise SimulatedCrash()

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = InterruptedTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    first = control.launch_application('desktop')
    request = control.request_application_termination(first.runtime_id, request_id=issued(control, 'STOP-RECOVERY'))
    with pytest.raises(SimulatedCrash):
        control.evaluate()
    assert control.evaluate().ok
    assert transport.signals == 1
    assert control.application_request_result(request.request_id).status == ApplicationRequestStatus.SATISFIED
    assert not control.application_runtime(first.runtime_id).cleanup_pending


def test_committed_short_lived_operation_retains_pending_cleanup(tmp_path):
    class RetainedTransport(FakeTransport):
        blocked = True
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            unit = kwargs['definition'].unit_name
            self.units[unit] = replace(self.units[unit], active_state='inactive', sub_state='dead')
        def stop(self, **kwargs):
            if self.blocked:
                raise RuntimeOperationError('cleanup unavailable')
            super().stop(**kwargs)

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = RetainedTransport()
    control = control_for(project, tmp_path, transport, ['boot-1'])
    request = control.request_application_launch('desktop', request_id=issued(control, 'COMMITTED-CLEANUP'))
    assert not control.evaluate().ok
    record = operation_for(control, request)
    assert record['phase'] == 'committed'
    assert not record['finished']
    transport.blocked = False
    assert control.evaluate().ok
    assert operation_for(control, request)['finished']
    assert len(transport.started) == 1


def test_prepared_short_run_counts_toward_actual_execution_boot(tmp_path, monkeypatch):
    from zog.box_control.runtime.systemd import SystemdServiceRuntime

    class ShortTransport(FakeTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            name = kwargs['definition'].unit_name
            self.units[name] = replace(self.units[name], active_state='inactive',
                                       sub_state='dead', main_pid=None)

    project = Project(tmp_path / 'project')
    write_application(project, start_policy='start-once-per-boot')
    transport = ShortTransport()
    boot = ['preparation-boot']
    control = control_for(project, tmp_path, transport, boot)
    original = SystemdServiceRuntime.launch
    def crash(*args, **kwargs):
        raise SimulatedCrash()
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', crash)
    with pytest.raises(SimulatedCrash):
        control.launch_application('desktop', request_id=issued(control, 'BOOT-BINDING'))
    assert not transport.started
    boot[0] = 'execution-boot'
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', original)
    assert control.evaluate().ok
    assert len(transport.started) == 1
    assert control.evaluate().ok
    assert len(transport.started) == 1
    record = next(r for r in control.application_operations() if r['request_id'] == issued(control, 'BOOT-BINDING'))
    assert record['prepared_boot_id'] == 'preparation-boot'
    assert record['boot_id'] == 'execution-boot'
    assert control.application_runtime(record['new_runtime_id']).boot_id == 'execution-boot'
    boot[0] = 'next-boot'
    assert control.evaluate().ok
    assert len(transport.started) == 2


@pytest.mark.parametrize('after_write', [False, True])
def test_execution_boot_barrier_failure_prevents_service_start(tmp_path, monkeypatch, after_write):
    from zog.box_control.errors import PersistenceError
    from zog.box_control.operations import OperationStore
    from zog.box_control.runtime.systemd import SystemdServiceRuntime

    project = Project(tmp_path / 'project')
    write_application(project)
    transport = FakeTransport()
    boot = ['boot-before']
    control = control_for(project, tmp_path, transport, boot)
    launch = SystemdServiceRuntime.launch
    def crash(*args, **kwargs):
        raise SimulatedCrash()
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', crash)
    with pytest.raises(SimulatedCrash):
        control.launch_application('desktop', request_id=issued(control, 'BOOT-BARRIER'))
    monkeypatch.setattr(SystemdServiceRuntime, 'launch', launch)
    boot[0] = 'boot-after'
    save = OperationStore.save
    def fail(self, record):
        if record['boot_id'] == 'boot-after':
            if after_write:
                save(self, record)
            raise PersistenceError('execution boot barrier failed')
        save(self, record)
    monkeypatch.setattr(OperationStore, 'save', fail)
    report = control.evaluate()
    assert not report.ok and 'boot barrier' in str(report.errors)
    assert not transport.started
    assert (project.state_dir / 'mutation-incomplete.json').exists()
    monkeypatch.setattr(OperationStore, 'save', save)
    fresh = control_for(Project(project.path), tmp_path, transport, boot)
    assert fresh.evaluate().ok
    assert len(transport.started) == 1
    record = next(r for r in fresh.application_operations() if r['request_id'] == issued(control, 'BOOT-BARRIER'))
    assert record['boot_id'] == 'boot-after'
    assert record['prepared_boot_id'] == 'boot-before'
    assert fresh.application_runtime(record['new_runtime_id']).boot_id == 'boot-after'


_issued_aliases = {}

def issued(control, label):
    key = (control.project.path, label)
    if key not in _issued_aliases:
        _issued_aliases[key] = control.issue_application_request_id()
    return _issued_aliases[key]
