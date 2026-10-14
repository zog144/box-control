from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from zog.box_control.api import BoxControl
from zog.box_control.model import ApplicationRuntimeState, ApplicationSpec, ProgramSpec
from zog.box_control.project import Project
from zog.box_control.runtime.reference import (
    ApplicationRuntimeReference,
    ProgramRuntimeReference,
    RuntimeReferenceStore,
)
from zog.box_control.runtime.systemd import (
    RuntimeIntegrityError,
    SystemdServiceRuntime,
    SystemdUnitObservation,
    application_definition,
)


class FakeImageProvider:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def ensure(self, project):
        return SimpleNamespace(
            generation="generation-1",
            root=self.root,
            manifest={"fingerprint": "generation-1", "packages": [], "distribution_packages": []},
            previous_manifest=None,
            reused=True,
        )


class FakeTransport:
    def __init__(self, *, exit_on_sigint=True, before_slice=None):
        self.units = {}
        self.kills = []
        self.stops = []
        self.counter = 0
        self.exit_on_sigint = exit_on_sigint
        self.before_slice = before_slice
        self.released_runtime_roots = []

    def version(self):
        return 257

    def start_slice(self, *, project_root, slice_name, description):
        if self.before_slice:
            self.before_slice()
        self.units[slice_name] = SystemdUnitObservation(
            unit_name=slice_name, exists=True, transient=True,
            active_state="active", sub_state="active")

    def start_service(self, *, project_root, generation, definition):
        self.counter += 1
        self.units[definition.unit_name] = SystemdUnitObservation(
            unit_name=definition.unit_name,
            exists=True,
            transient=True,
            invocation_id=f"inv-{self.counter}",
            active_state="active",
            sub_state="running",
            result="success",
            main_pid=2000 + self.counter,
            control_group=f"/{definition.unit_name}",
            properties=definition.expected_properties(),
        )

    def observe(self, *, project_root, unit_name):
        return self.units.get(unit_name, SystemdUnitObservation(unit_name=unit_name, exists=False))

    def kill(self, *, project_root, unit_name, signal_number):
        self.kills.append((unit_name, signal_number))
        if unit_name not in self.units:
            return
        if signal_number == 15 or (signal_number == 2 and self.exit_on_sigint):
            old = self.units[unit_name]
            self.units[unit_name] = replace(
                old, active_state="inactive", sub_state="dead", main_pid=None
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

    def release_runtime_root(self, *, project_root, runtime_id):
        self.released_runtime_roots.append(runtime_id)


def app():
    return ApplicationSpec(
        name="desktop",
        programs=(ProgramSpec(name="browser", command=("/usr/bin/browser",)),),
    )


@pytest.fixture(autouse=True)
def _existing_generation_root(tmp_path):
    (tmp_path / "root").mkdir(parents=True, exist_ok=True)


def test_launch_persists_starting_reference_before_systemd_mutation(tmp_path):
    project = Project(tmp_path / "project")
    project.state_dir.mkdir(parents=True)
    store = RuntimeReferenceStore(project.runtime_reference_file)
    seen = []

    def before_slice():
        refs = store.load()
        assert len(refs) == 1
        seen.append(next(iter(refs.values())).state)

    runtime = SystemdServiceRuntime(project, FakeTransport(before_slice=before_slice))
    refs = {}
    launched = runtime.launch(
        app(), instance_id="desktop", generation="generation-1", generation_root=tmp_path / "root",
        references=refs, store=store,
    )
    assert seen == [ApplicationRuntimeState.STARTING]
    assert launched.state == ApplicationRuntimeState.RUNNING
    assert launched.programs[0].invocation_id == "inv-1"


def test_invocation_id_mismatch_is_integrity_fault(tmp_path):
    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(tmp_path / "refs.json")
    transport = FakeTransport()
    runtime = SystemdServiceRuntime(project, transport)
    refs = {}
    launched = runtime.launch(
        app(), instance_id="desktop", generation="generation-1", generation_root=tmp_path / "root",
        references=refs, store=store,
    )
    unit = launched.programs[0].unit_name
    transport.units[unit] = replace(transport.units[unit], invocation_id="foreign")
    with pytest.raises(RuntimeIntegrityError, match="invocation ID changed"):
        runtime.observe_reference(launched)


def test_material_property_change_is_integrity_fault(tmp_path):
    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(tmp_path / "refs.json")
    transport = FakeTransport()
    runtime = SystemdServiceRuntime(project, transport)
    refs = {}
    launched = runtime.launch(
        app(), instance_id="desktop", generation="generation-1", generation_root=tmp_path / "root",
        references=refs, store=store,
    )
    unit = launched.programs[0].unit_name
    observation = transport.units[unit]
    props = dict(observation.properties)
    props["Restart"] = "always"
    transport.units[unit] = replace(observation, properties=tuple(props.items()))
    with pytest.raises(RuntimeIntegrityError, match="Restart"):
        runtime.observe_reference(launched)


def test_terminate_uses_sigint_then_sigterm_for_remaining_cgroup(tmp_path):
    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(tmp_path / "refs.json")
    transport = FakeTransport(exit_on_sigint=False)
    runtime = SystemdServiceRuntime(project, transport, stop_grace_seconds=0)
    refs = {}
    launched = runtime.launch(
        app(), instance_id="desktop", generation="generation-1", generation_root=tmp_path / "root",
        references=refs, store=store,
    )
    unit = launched.programs[0].unit_name
    final = runtime.terminate(launched, references=refs, store=store)
    assert transport.kills == [(unit, 2), (unit, 15)]
    assert final.state == ApplicationRuntimeState.TERMINATED
    assert launched.slice_name in transport.stops


def _write_application(project, *, multiple_instances):
    project.application_dir.mkdir(parents=True)
    definition = project.application_dir / "desktop"
    definition.mkdir()
    (definition / "application.py").write_text(
        "application(name=\"desktop\", multiple_instances="
        f"{multiple_instances!r}, programs=(program(name=\"browser\", "
        "command=(\"/usr/bin/browser\",)),))",
        encoding="utf-8",
    )


def test_public_api_replaces_single_instance_application(tmp_path):
    project = Project(tmp_path / "project")
    _write_application(project, multiple_instances=False)
    project.require_state_allowed = lambda: None
    transport = FakeTransport()
    control = BoxControl(
        project,
        image_provider=FakeImageProvider(tmp_path / "root"),
        systemd_transport=transport,
        boot_id_provider=lambda: "boot-1",
    )
    first = control.launch_application("desktop")
    second = control.launch_application("desktop")
    assert first.runtime_id != second.runtime_id
    assert first.instance_id == second.instance_id == "desktop"
    assert second.replaces_runtime_id == first.runtime_id
    assert control.application_runtime(first.runtime_id).state == ApplicationRuntimeState.TERMINATED
    assert len(control.application_runtimes()) == 2


def test_public_api_creates_distinct_multi_instance_application_instances(tmp_path):
    project = Project(tmp_path / "project")
    _write_application(project, multiple_instances=True)
    project.require_state_allowed = lambda: None
    control = BoxControl(
        project,
        image_provider=FakeImageProvider(tmp_path / "root"),
        systemd_transport=FakeTransport(),
        boot_id_provider=lambda: "boot-1",
    )
    first = control.launch_application("desktop")
    second = control.launch_application("desktop")
    assert first.runtime_id != second.runtime_id
    assert first.instance_id != second.instance_id
    assert len(control.current_application_runtimes("desktop")) == 2


def test_successful_short_lived_service_is_recorded_terminal_not_running(tmp_path):
    class ShortLivedTransport(FakeTransport):
        def start_service(self, *, project_root, generation, definition):
            self.counter += 1
            self.units[definition.unit_name] = SystemdUnitObservation(
                unit_name=definition.unit_name,
                exists=True,
                transient=True,
                invocation_id=f"inv-{self.counter}",
                active_state="inactive",
                sub_state="dead",
                result="success",
                main_pid=None,
                control_group=f"/{definition.unit_name}",
                properties=definition.expected_properties(),
            )

    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(tmp_path / "refs.json")
    runtime = SystemdServiceRuntime(project, ShortLivedTransport())
    refs = {}
    launched = runtime.launch(
        app(), instance_id="desktop", generation="generation-1", generation_root=tmp_path / "root",
        references=refs, store=store,
    )
    assert launched.state == ApplicationRuntimeState.TERMINATED
    assert launched.programs[0].result == "success"
    assert launched.completed_at is not None


def test_interrupted_partial_launch_is_not_committed_as_running(tmp_path):
    project = Project(tmp_path / "project")
    application = ApplicationSpec(
        name="desktop",
        programs=(
            ProgramSpec(name="browser", command=("/usr/bin/browser",)),
            ProgramSpec(name="helper", command=("/usr/bin/helper",)),
        ),
    )
    definition = application_definition(
        project, application, "A7K3Q2", generation_root=tmp_path / "root"
    )
    transport = FakeTransport()
    transport.start_service(
        project_root=project.path,
        generation="generation-1",
        definition=definition.services[0],
    )
    reference = ApplicationRuntimeReference(
        runtime_id="A7K3Q2",
        application="desktop",
        instance_id="desktop",
        generation="generation-1",
        state=ApplicationRuntimeState.STARTING,
        slice_name=definition.slice_name,
        programs=tuple(
            ProgramRuntimeReference(
                program=program.name,
                unit_name=service.unit_name,
                command=program.command,
                expected_properties=service.expected_properties(),
            )
            for program, service in zip(
                application.programs, definition.services, strict=True
            )
        ),
    )

    observed = SystemdServiceRuntime(project, transport).observe_reference(reference)
    assert observed.state == ApplicationRuntimeState.TERMINATING
    assert "before every required program executed" in observed.error


class LaunchFailureTransport(FakeTransport):
    def __init__(self, *, failure_position, defer_failure=False, delay_cleanup=False):
        super().__init__()
        self.failure_position = failure_position
        self.defer_failure = defer_failure
        self.delay_cleanup = delay_cleanup
        self.started = []

    def start_service(self, **kwargs):
        super().start_service(**kwargs)
        unit = kwargs["definition"].unit_name
        self.started.append(unit)
        if not self.defer_failure and len(self.started) == self.failure_position:
            self.fail(unit)

    def fail(self, unit):
        self.units[unit] = replace(
            self.units[unit], active_state="failed", sub_state="failed",
            result="exit-code", main_pid=None,
        )

    def observe(self, **kwargs):
        if self.defer_failure and len(self.started) == 3 and self.started[self.failure_position - 1] in self.units:
            self.fail(self.started[self.failure_position - 1])
        return super().observe(**kwargs)

    def kill(self, **kwargs):
        if self.delay_cleanup:
            self.kills.append((kwargs["unit_name"], kwargs["signal_number"]))
        else:
            super().kill(**kwargs)

    def stop(self, **kwargs):
        if not self.delay_cleanup:
            super().stop(**kwargs)


def composed_application():
    return ApplicationSpec(
        name="desktop",
        programs=tuple(
            ProgramSpec(name=name, command=(f"/usr/bin/{name}",))
            for name in ("first", "middle", "last")
        ),
    )


@pytest.mark.parametrize("failure_position", [1, 2, 3])
@pytest.mark.parametrize("defer_failure", [False, True])
def test_required_member_failure_rolls_back_launch(tmp_path, failure_position, defer_failure):
    from zog.box_control.errors import RuntimeOperationError

    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = LaunchFailureTransport(
        failure_position=failure_position, defer_failure=defer_failure
    )
    runtime = SystemdServiceRuntime(project, transport)
    references = {}
    with pytest.raises(RuntimeOperationError, match="Result=exit-code"):
        runtime.launch(
            composed_application(), instance_id="desktop", generation="generation-1",
            generation_root=tmp_path / "root", references=references, store=store,
        )
    failed = next(iter(store.load().values()))
    assert failed.state == ApplicationRuntimeState.FAILED
    assert failed.completed_at is not None
    assert "Result=exit-code" in failed.error
    assert len(transport.started) == (3 if defer_failure else failure_position)
    assert transport.kills == [(unit, 15) for unit in reversed(transport.started)]
    assert all(not observation.active for observation in transport.units.values())


def test_failed_launch_stays_failed_after_asynchronous_cleanup(tmp_path):
    from zog.box_control.errors import RuntimeOperationError
    from zog.box_control.reconcile import Reconciler

    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = LaunchFailureTransport(failure_position=2, delay_cleanup=True)
    runtime = SystemdServiceRuntime(project, transport, stop_grace_seconds=0)
    references = {}
    with pytest.raises(RuntimeOperationError, match="Result=exit-code"):
        runtime.launch(
            composed_application(), instance_id="desktop", generation="generation-1",
            generation_root=tmp_path / "root", references=references, store=store,
        )
    failed = next(iter(store.load().values()))
    assert failed.state == ApplicationRuntimeState.TERMINATING
    assert failed.completed_at is None
    assert Reconciler._protected_generations(references) == {"generation-1"}
    transport.delay_cleanup = False
    finished = runtime.terminate(failed, references=references, store=store)
    assert finished.state == ApplicationRuntimeState.FAILED
    assert finished.error == failed.error
    assert finished.completed_at is not None


@pytest.mark.parametrize("bad_state,bad_result,invocation", [
    ("failed", "exit-code", "inv-2"),
    ("active", "exit-code", "inv-2"),
    ("activating", "success", "inv-2"),
    ("inactive", None, "inv-2"),
    ("active", "success", None),
])
def test_recovered_start_requires_acceptance_of_every_member(
    tmp_path, bad_state, bad_result, invocation
):
    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = FakeTransport()
    runtime = SystemdServiceRuntime(project, transport, stop_grace_seconds=0)
    references = {}
    launched = runtime.launch(
        composed_application(), instance_id="desktop", generation="generation-1",
        generation_root=tmp_path / "root", references=references, store=store,
    )
    # Simulate a crash before the final commit, with the last identity not yet saved.
    starting = replace(
        launched, state=ApplicationRuntimeState.STARTING,
        programs=tuple(replace(p, invocation_id=None) for p in launched.programs),
    )
    unit = starting.programs[1].unit_name
    transport.units[unit] = replace(
        transport.units[unit], active_state=bad_state, result=bad_result,
        invocation_id=invocation,
    )
    observed = runtime.observe_reference(starting)
    assert observed.state == ApplicationRuntimeState.TERMINATING
    assert observed.error
    finished = runtime.terminate(observed, references=references, store=store)
    assert finished.state == ApplicationRuntimeState.FAILED


def test_member_failure_after_commit_keeps_other_members_running(tmp_path):
    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = FakeTransport()
    runtime = SystemdServiceRuntime(project, transport)
    launched = runtime.launch(
        composed_application(), instance_id="desktop", generation="generation-1",
        generation_root=tmp_path / "root", references={}, store=store,
    )
    unit = launched.programs[1].unit_name
    transport.units[unit] = replace(
        transport.units[unit], active_state="failed", result="exit-code"
    )
    observed = runtime.observe_reference(launched)
    assert observed.state == ApplicationRuntimeState.RUNNING
    assert observed.error is None
    assert transport.kills == []


def test_cleanup_observation_failure_keeps_generation_protected(tmp_path):
    from zog.box_control.errors import RuntimeOperationError
    from zog.box_control.reconcile import Reconciler

    class UnavailableTransport(FakeTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            raise RuntimeOperationError("lost connection after start")

        def observe(self, **kwargs):
            raise RuntimeOperationError("connection unavailable")

        def kill(self, **kwargs):
            raise RuntimeOperationError("connection unavailable")

    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    runtime = SystemdServiceRuntime(project, UnavailableTransport())
    references = {}
    with pytest.raises(RuntimeOperationError, match="lost connection"):
        runtime.launch(
            app(), instance_id="desktop", generation="generation-1",
            generation_root=tmp_path / "root", references=references, store=store,
        )
    failed = next(iter(store.load().values()))
    assert failed.state == ApplicationRuntimeState.TERMINATING
    assert Reconciler._protected_generations(references) == {"generation-1"}


def test_pruned_runtime_identity_cannot_reuse_retained_mount_data(tmp_path, monkeypatch):
    project = Project(tmp_path / "project")
    retained = project.mounts_dir / "desktop" / "OLD001"
    retained.mkdir(parents=True)
    (retained / "marker").write_text("retained", encoding="utf-8")
    identities = iter(("OLD001", "NEW001"))
    monkeypatch.setattr(
        "zog.box_control.runtime.systemd.new_application_runtime_id", lambda: next(identities)
    )
    runtime = SystemdServiceRuntime(project, FakeTransport())
    launched = runtime.launch(
        app(), instance_id="desktop", generation="generation-1",
        generation_root=tmp_path / "root", references={},
        store=RuntimeReferenceStore(project.runtime_reference_file),
    )
    assert launched.runtime_id == "NEW001"
    assert (retained / "marker").read_text() == "retained"


def test_terminal_cleanup_retries_preserves_evidence_and_protects_history(tmp_path):
    from zog.box_control.errors import RuntimeOperationError
    from zog.box_control.reconcile import Reconciler

    class RetainedTransport(FakeTransport):
        blocked = True
        resets = []
        released = []

        def stop(self, *, project_root, unit_name):
            self.stops.append(unit_name)
            if unit_name.endswith(".slice"):
                self.units.pop(unit_name, None)
            elif self.blocked:
                raise RuntimeOperationError("stop reply lost")
            # Failed service stays loaded until reset and release.

        def reset_failed(self, *, project_root, unit_name):
            persisted = store.load()[reference.runtime_id]
            assert persisted.programs[0].result == "exit-code"
            self.resets.append(unit_name)
            self.units[unit_name] = replace(self.units[unit_name],
                active_state="inactive", sub_state="dead", result="success")

        def release(self, *, project_root, unit_name):
            self.released.append(unit_name)
            self.units.pop(unit_name, None)

    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = RetainedTransport()
    runtime = SystemdServiceRuntime(project, transport)
    references = {}
    reference = runtime.launch(app(), instance_id="desktop", generation="generation-1",
        generation_root=tmp_path / "root", references=references, store=store)
    unit = reference.programs[0].unit_name
    transport.units[unit] = replace(transport.units[unit], active_state="failed",
                                   sub_state="failed", result="exit-code", main_pid=None)
    reference = runtime.observe_reference(reference)
    assert reference.state == ApplicationRuntimeState.FAILED
    pending = runtime.cleanup(reference, references=references, store=store)
    assert pending.cleanup_pending
    assert "stop reply lost" in pending.cleanup_error
    assert reference.slice_name not in transport.stops
    assert "generation-1" in Reconciler._protected_generations(references)
    assert not store.prune_completed(references, limit=1)
    # Simulate another controller evaluation using only persisted state.
    references = store.load()
    transport.blocked = False
    finished = runtime.cleanup(references[reference.runtime_id], references=references, store=store)
    assert finished.state == ApplicationRuntimeState.FAILED
    assert not finished.cleanup_pending
    assert finished.cleanup_error is None
    assert finished.programs[0].result == "exit-code"
    assert transport.resets == [unit]
    assert transport.released == [unit, reference.slice_name]
    before = list(transport.stops)
    assert runtime.cleanup(finished, references=references, store=store) == finished
    assert before == transport.stops


def test_successful_terminal_units_and_slice_are_unloaded(tmp_path):
    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = FakeTransport()
    runtime = SystemdServiceRuntime(project, transport)
    references = {}
    launched = runtime.launch(app(), instance_id="desktop", generation="generation-1",
        generation_root=tmp_path / "root", references=references, store=store)
    unit = launched.programs[0].unit_name
    transport.units[unit] = replace(transport.units[unit], active_state="inactive",
                                   sub_state="dead", main_pid=None)
    finished = runtime.terminate(launched, references=references, store=store)
    assert not finished.cleanup_pending
    assert transport.units == {}
    assert finished.programs[0].result == "success"
    assert not transport.kills


@pytest.mark.parametrize("fragment,allowed", [
    ("/run/systemd/transient/{unit}", True),
    ("/etc/systemd/system/{unit}", False),
    ("/run/systemd/transient/other.service", False),
])
def test_transient_fragment_must_be_systemd_generated_path(tmp_path, fragment, allowed):
    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = FakeTransport()
    runtime = SystemdServiceRuntime(project, transport)
    reference = runtime.launch(app(), instance_id="desktop", generation="generation-1",
        generation_root=tmp_path / "root", references={}, store=store)
    unit = reference.programs[0].unit_name
    transport.units[unit] = replace(transport.units[unit], fragment_path=fragment.format(unit=unit))
    if allowed:
        assert runtime.observe_reference(reference).state == ApplicationRuntimeState.RUNNING
    else:
        with pytest.raises(RuntimeIntegrityError):
            runtime.observe_reference(reference)


@pytest.mark.parametrize("rollback_observation", ["unavailable", "absent", "reset"])
def test_failed_launch_preserves_saved_failure_during_rollback(tmp_path, rollback_observation):
    from zog.box_control.errors import RuntimeOperationError
    from zog.box_control.reconcile import Reconciler

    class EvidenceTransport(FakeTransport):
        rolling_back = False

        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            unit = kwargs["definition"].unit_name
            self.units[unit] = replace(self.units[unit], active_state="failed",
                sub_state="failed", result="exit-code", main_pid=None)

        def kill(self, **kwargs):
            saved = next(iter(store.load().values()))
            assert saved.programs[0].result == "exit-code"
            self.rolling_back = True

        def observe(self, **kwargs):
            observation = super().observe(**kwargs)
            if not self.rolling_back or not kwargs["unit_name"].endswith(".service"):
                return observation
            if rollback_observation == "unavailable":
                raise RuntimeOperationError("rollback transport unavailable")
            if rollback_observation == "absent":
                return SystemdUnitObservation(unit_name=kwargs["unit_name"], exists=False)
            return replace(observation, active_state="inactive", sub_state="dead", result="success")

    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    runtime = SystemdServiceRuntime(project, EvidenceTransport())
    references = {}
    with pytest.raises(RuntimeOperationError, match="Result=exit-code"):
        runtime.launch(app(), instance_id="desktop", generation="generation-1",
            generation_root=tmp_path / "root", references=references, store=store)
    failed = next(iter(store.load().values()))
    assert failed.programs[0].result == "exit-code"
    assert "Result=exit-code" in failed.error
    if rollback_observation == "unavailable":
        assert failed.cleanup_pending
        assert failed.state == ApplicationRuntimeState.TERMINATING
        assert Reconciler._protected_generations(store.load()) == {"generation-1"}
    else:
        assert failed.state == ApplicationRuntimeState.FAILED
    store.save(store.load())
    assert next(iter(store.load().values())).programs[0].result == "exit-code"


@pytest.mark.parametrize('after_start', [False, True])
def test_persistence_fault_stops_launch_without_rollback(tmp_path, monkeypatch, after_start):
    from zog.box_control.errors import PersistenceError, RecoveryRequired

    project = Project(tmp_path / 'project')
    _write_application(project, multiple_instances=False)
    (project.application_dir / 'desktop' / 'application.py').write_text(
        'application(name="desktop", programs=(' + ','.join(
            f'program(name="{name}", command=("/usr/bin/{name}",))'
            for name in ('first', 'middle', 'last')
        ) + ',))'
    )
    project.require_state_allowed = lambda: None
    transport = FakeTransport()
    control = BoxControl(project, image_provider=FakeImageProvider(tmp_path / 'root'),
                         systemd_transport=transport, boot_id_provider=lambda: 'boot-1')
    save = RuntimeReferenceStore.save

    def failing_save(store, references):
        if references and (not after_start or transport.counter):
            raise PersistenceError('reference barrier failed')
        save(store, references)

    monkeypatch.setattr(RuntimeReferenceStore, 'save', failing_save)
    with pytest.raises(PersistenceError, match='reference barrier'):
        control.launch_application('desktop')
    assert transport.counter == int(after_start)
    assert transport.kills == []
    assert transport.stops == []
    if after_start:
        saved = next(iter(control.application_runtimes().values()))
        assert saved.state == ApplicationRuntimeState.STARTING
        assert saved.cleanup_pending
    monkeypatch.setattr(RuntimeReferenceStore, 'save', save)
    prepared_id = control.application_operations()[0]['new_runtime_id']
    assert control.evaluate().ok
    assert transport.counter == 3
    assert transport.kills == []
    assert transport.stops == []
    assert set(control.application_runtimes()) == {prepared_id}


def test_cleanup_storage_fault_stops_before_unloading_evidence(tmp_path, monkeypatch):
    from zog.box_control.errors import PersistenceError

    project = Project(tmp_path / 'project')
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = FakeTransport()
    runtime = SystemdServiceRuntime(project, transport)
    references = {}
    launched = runtime.launch(composed_application(), instance_id='desktop', generation='generation-1',
        generation_root=tmp_path / 'root', references=references, store=store)
    for unit, observation in list(transport.units.items()):
        if unit.endswith('.service'):
            transport.units[unit] = replace(observation, active_state='failed', result='exit-code')
    terminal = runtime.observe_reference(launched)

    def fail_save(references):
        raise PersistenceError('evidence barrier failed')

    monkeypatch.setattr(store, 'save', fail_save)
    with pytest.raises(PersistenceError):
        runtime.cleanup(terminal, references=references, store=store)
    assert transport.stops == []
    assert transport.kills == []
    assert len(transport.units) == 4



def test_runtime_scaffold_cleanup_failure_remains_pending_and_retries(tmp_path):
    from zog.box_control.errors import RuntimeOperationError
    from zog.box_control.reconcile import Reconciler

    class ScaffoldCleanupTransport(FakeTransport):
        fail_scaffold_cleanup = True

        def release_runtime_root(self, *, project_root, runtime_id):
            if self.fail_scaffold_cleanup:
                raise RuntimeOperationError("runtime scaffold cleanup unavailable")
            super().release_runtime_root(
                project_root=project_root, runtime_id=runtime_id
            )

    project = Project(tmp_path / "project")
    store = RuntimeReferenceStore(project.runtime_reference_file)
    transport = ScaffoldCleanupTransport()
    runtime = SystemdServiceRuntime(project, transport)
    references = {}
    launched = runtime.launch(
        app(),
        instance_id="desktop",
        generation="generation-1",
        generation_root=tmp_path / "root",
        references=references,
        store=store,
    )

    pending = runtime.terminate(
        launched, references=references, store=store
    )
    assert pending.cleanup_pending
    assert "runtime scaffold cleanup unavailable" in pending.cleanup_error
    assert "generation-1" in Reconciler._protected_generations(references)
    assert transport.units == {}

    persisted = store.load()[pending.runtime_id]
    assert persisted.cleanup_pending
    transport.fail_scaffold_cleanup = False
    finished = runtime.cleanup(
        persisted, references=references, store=store
    )
    assert not finished.cleanup_pending
    assert finished.cleanup_error is None
    assert transport.released_runtime_roots == [finished.runtime_id]
