import json
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
