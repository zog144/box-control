"""Opt-in real-host tests. These mutate only uniquely named test runtime units.

See catalogue/systemd-vm-testing.md. A missing prerequisite is a failure when
explicitly enabled, not a silent skipped acceptance test.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from zog.box_control.errors import RuntimeOperationError
from zog.box_control.model import ApplicationRuntimeState, ApplicationSpec, ProgramSpec
from zog.box_control.project import Project
from zog.box_control.runtime.reference import RuntimeReferenceStore
from zog.box_control.runtime.root_control import RootControlSystemdTransport
from zog.box_control.runtime.systemd import SystemdServiceRuntime


@pytest.fixture
def live(request, tmp_path):
    if not request.config.getoption("--run-systemd-integration"):
        pytest.skip("requires explicit --run-systemd-integration on a disposable VM")
    assert os.geteuid() == 0, "integration tests require root"
    assert Path("/run/systemd/system").is_dir(), "systemd must be the system manager"
    source = request.config.getoption("--systemd-rootfs")
    assert source, "pass --systemd-rootfs=/path/to/minimal/rootfs"
    source = Path(source).resolve()
    assert source != Path("/") and source.is_dir(), "provide a dedicated minimal test rootfs"
    assert all((source / path).exists() for path in ("bin/sh", "bin/sleep", "bin/true"))
    project = Project(tmp_path / ("vmtest" + uuid.uuid4().hex[:8]))
    generation_root = project.rootfs_dir / "generations" / "vmroot" / "root"
    shutil.copytree(source, generation_root, symlinks=True)
    (generation_root / ".box-control-rootfs.json").write_text(json.dumps({"fingerprint": "vmroot"}))
    for directory in ("data", "tmp", "proc", "sys", "dev"):
        (generation_root / directory).mkdir(exist_ok=True)
    socket_path = tmp_path / "root.sock"
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join([package_root, *sys.path])
    daemon = subprocess.Popen([
        sys.executable, "-c",
        "import sys; from zog.root_control.daemon import RootControlDaemon; RootControlDaemon(sys.argv[1]).serve()",
        str(socket_path),
    ], env=environment)
    transport = RootControlSystemdTransport(socket_path)
    runtime = SystemdServiceRuntime(project, transport, stop_grace_seconds=.1)
    store = RuntimeReferenceStore(project.runtime_reference_file)
    references = {}

    def launch(programs):
        return runtime.launch(ApplicationSpec(name="probe", programs=tuple(programs)),
            instance_id="probe", generation="vmroot", generation_root=generation_root,
            references=references, store=store,
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip())

    def finish(reference):
        reference = runtime.terminate(reference, references=references, store=store)
        deadline = time.monotonic() + 15
        while reference.cleanup_pending and time.monotonic() < deadline:
            time.sleep(.1)
            reference = runtime.cleanup(reference, references=references, store=store)
        assert not reference.cleanup_pending, reference.cleanup_error
        for name in [*(program.unit_name for program in reference.programs), reference.slice_name]:
            assert not transport.observe(project_root=project.path, unit_name=name).exists
        return reference

    try:
        deadline = time.monotonic() + 5
        while not socket_path.exists() and daemon.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert socket_path.exists() and daemon.poll() is None, "root-control failed to start"
        while True:
            try:
                assert transport._request("ping") is None
                break
            except RuntimeOperationError:
                if daemon.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        runtime.preflight()
        yield SimpleNamespace(launch=launch, finish=finish, runtime=runtime,
            store=store, references=references, project=project, transport=transport)
    finally:
        try:
            for reference in list(store.load().values()):
                if reference.cleanup_pending:
                    finish(reference)
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=3)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=3)


def program(name, command, **kwargs):
    return ProgramSpec(name=name, command=tuple(command), user="root", **kwargs)


def test_live_fast_success_retains_invocation_and_unloads(live):
    reference = live.launch([program("quick", ["/bin/true"])])
    assert reference.programs[0].invocation_id
    final = live.finish(reference)
    assert final.state == ApplicationRuntimeState.TERMINATED
    assert final.programs[0].result == "success"


def test_live_exec_failure_rolls_back_active_peer_and_slice(live):
    with pytest.raises(RuntimeOperationError) as failure:
        live.launch([program("peer", ["/bin/sleep", "60"]),
                     program("broken", ["/does-not-exist"])])
    reference = next(iter(live.store.load().values()))
    peer, broken = reference.programs
    assert peer.program == "peer" and peer.invocation_id
    assert broken.program == "broken" and broken.invocation_id
    assert broken.result == "exit-code"
    assert broken.unit_name in str(failure.value)
    assert broken.unit_name in reference.error
    final = live.finish(reference)
    assert final.state == ApplicationRuntimeState.FAILED
    assert final.programs[0].invocation_id == peer.invocation_id
    assert final.programs[1].result == "exit-code"
    assert final.error == reference.error


def test_live_descendant_keeps_service_alive_after_main_exit(live):
    reference = live.launch([program("children", ["/bin/sh", "-c", "/bin/sleep 60 & exit 0"])])
    time.sleep(.2)
    assert live.runtime.observe_reference(reference).state == ApplicationRuntimeState.RUNNING
    live.finish(reference)


def test_live_stop_escalates_for_signal_ignoring_program(live):
    reference = live.launch([program("stubborn", ["/bin/sh", "-c",
        "trap '' INT TERM; while :; do /bin/sleep 1; done"])])
    time.sleep(.2)
    before = time.monotonic()
    live.finish(reference)
    assert time.monotonic() - before < 40


def test_live_runtime_mounts_do_not_share_data(live):
    first = live.launch([program("writer", ["/bin/sh", "-c",
        "echo first > /data/value; exec /bin/sleep 60"], mounts=(("data", "/data"),))])
    second = live.launch([program("writer", ["/bin/sh", "-c",
        "echo second > /data/value; exec /bin/sleep 60"], mounts=(("data", "/data"),))])
    paths = [live.project.mounts_dir / "probe" / item.runtime_id / "writer" / "data" / "value"
             for item in (first, second)]
    deadline = time.monotonic() + 3
    while not all(path.exists() for path in paths) and time.monotonic() < deadline:
        time.sleep(.02)
    assert [path.read_text().strip() for path in paths] == ["first", "second"]
    live.finish(first)
    live.finish(second)
    for reference in (first, second):
        assert not (
            live.project.mounts_dir / "runtime-roots" / reference.runtime_id
        ).exists()


@pytest.fixture
def recovery(live, tmp_path):
    """Fresh controller processes share only durable files and the real daemon."""
    import importlib.util

    worker = Path(__file__).with_name("live_recovery_worker.py")
    specification = importlib.util.spec_from_file_location("live_recovery_worker", worker)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    control_at = module.control_at

    socket = tmp_path / 'root.sock'
    project = live.project
    selected = project.path / 'selected-generation'
    selected.write_text('vmroot')
    (project.rootfs_dir / 'active').symlink_to('generations/vmroot')
    control = control_at(project.path, socket)
    worker = Path(__file__).with_name('live_recovery_worker.py')
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))

    def definition(*, changed=False, broken=False):
        directory = project.application_dir / 'probe'
        directory.mkdir(parents=True, exist_ok=True)
        commands = [('peer', ('/bin/sleep', '300' if changed else '240'))]
        if broken:
            commands.append(('broken', ('/does-not-exist',)))
        else:
            commands.append(('second', ('/bin/sleep', '240')))
        programs = ','.join(f'program(name={name!r}, command={command!r}, user="root")'
                            for name, command in commands)
        (directory / 'application.py').write_text(
            f'application(name="probe", start_policy="externally-controlled", programs=({programs},))')

    def select_new_generation():
        root = project.rootfs_dir / 'generations' / 'newroot' / 'root'
        shutil.copytree(project.rootfs_dir / 'generations' / 'vmroot' / 'root', root, symlinks=True)
        (root / '.box-control-rootfs.json').write_text(json.dumps({'fingerprint': 'newroot'}))
        (project.rootfs_dir / 'active').unlink()
        (project.rootfs_dir / 'active').symlink_to('generations/newroot')
        selected.write_text('newroot')

    def evaluate(point='recover', *, interrupted=False, success=True):
        import signal
        ready = tmp_path / (uuid.uuid4().hex + '.ready')
        log = tmp_path / (ready.stem + '.worker.log')
        with log.open('w') as output:
            child = subprocess.Popen([sys.executable, str(worker), str(project.path),
                str(socket), point, str(ready)], env=environment, stdout=output, stderr=output)
            try:
                if interrupted:
                    deadline = time.monotonic() + 90
                    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    assert ready.exists(), log.read_text()
                    child.kill()
                    assert child.wait(timeout=10) == -signal.SIGKILL
                    return json.loads(ready.read_text())
                assert child.wait(timeout=90) == (0 if success else 2), log.read_text()
                report = json.loads(log.read_text().splitlines()[-1])
                assert report['ok'] == success, report
                return report
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)

    def record(request):
        return next(item for item in control.application_operations()
                    if item['request_id'] == request.request_id)

    def completed(request, status='satisfied'):
        result = control.application_request_result(request.request_id)
        assert result.status.value == status
        assert not (project.application_request_dir / f'{request.request_id}.json').exists()
        assert not (project.state_dir / 'mutation-incomplete.json').exists()
        operation = record(request)
        assert operation['finished'] and operation['published']
        return control.application_runtime(result.runtime_id)

    definition()
    return SimpleNamespace(control=control, definition=definition, select_new=select_new_generation,
        evaluate=evaluate, record=record, completed=completed, project=project)


def test_live_recovery_after_termination_uses_prepared_inputs(live, recovery):
    r = recovery
    first = r.control.launch_application('probe')
    request = r.control.request_application_launch('probe')
    evidence = r.evaluate('before-launch', interrupted=True)
    stopped = r.control.application_runtime(first.runtime_id)
    assert stopped.state == ApplicationRuntimeState.TERMINATED and not stopped.cleanup_pending
    for program in stopped.programs:
        assert not live.transport.observe(project_root=r.project.path, unit_name=program.unit_name).exists
    prepared = r.record(request)
    assert evidence['runtime_id'] == prepared['new_runtime_id']
    assert not prepared['attempted_units']
    assert r.control.application_request_result(request.request_id) is None
    r.definition(changed=True)
    r.select_new()
    r.evaluate()
    recovered = r.completed(request)
    assert recovered.runtime_id == prepared['new_runtime_id']
    assert recovered.replaces_runtime_id == first.runtime_id
    assert recovered.generation == 'vmroot'
    assert recovered.programs[0].command == ('/bin/sleep', '240')
    assert (r.project.rootfs_dir / 'generations' / 'vmroot').exists()
    invocations = [p.invocation_id for p in recovered.programs]
    assert all(invocations)
    r.control.request_application_launch('probe', request_id=request.request_id)
    r.evaluate()
    assert [p.invocation_id for p in r.completed(request).programs] == invocations


def test_live_recovery_after_start_does_not_repeat_execution(live, recovery):
    r = recovery
    request = r.control.request_application_launch('probe')
    evidence = r.evaluate('after-start', interrupted=True)
    prepared = r.record(request)
    pending = r.control.application_runtime(prepared['new_runtime_id'])
    assert pending.programs[0].invocation_id is None
    assert prepared['attempted_units'] == [evidence['unit_name']]
    assert not live.transport.observe(project_root=r.project.path,
                                      unit_name=pending.programs[1].unit_name).exists
    r.evaluate()
    recovered = r.completed(request)
    assert recovered.runtime_id == pending.runtime_id
    assert recovered.programs[0].invocation_id == evidence['invocation_id']
    assert recovered.programs[1].invocation_id
    assert len(r.control.application_runtimes()) == 1
    r.control.request_application_launch('probe', request_id=request.request_id)
    r.evaluate()
    assert [p.invocation_id for p in r.completed(request).programs] == [
        p.invocation_id for p in recovered.programs]


def test_live_recovery_during_rollback_preserves_failure_and_protection(live, recovery):
    r = recovery
    r.definition(broken=True)
    request = r.control.request_application_launch('probe')
    evidence = r.evaluate('during-cleanup', interrupted=True)
    prepared = r.record(request)
    pending = r.control.application_runtime(prepared['new_runtime_id'])
    assert pending.cleanup_pending
    assert pending.programs[0].invocation_id  # The peer really started.
    assert pending.programs[1].invocation_id == evidence['invocation_id']
    assert pending.programs[1].result == 'exit-code'
    assert not prepared['finished']
    r.select_new()
    blocked = r.evaluate('blocked-cleanup', success=False)
    assert 'cleanup' in str(blocked['errors'])
    assert (r.project.state_dir / 'mutation-incomplete.json').exists()
    assert (r.project.rootfs_dir / 'generations' / 'vmroot').exists()
    assert r.control.application_runtime(pending.runtime_id).cleanup_pending
    r.evaluate()
    recovered = r.completed(request, 'failed')
    assert recovered.state == ApplicationRuntimeState.FAILED
    assert recovered.programs[1].result == 'exit-code'
    assert recovered.programs[1].invocation_id == evidence['invocation_id']
    assert recovered.error and evidence['unit_name'] in recovered.error
    assert not recovered.cleanup_pending
    for name in [*(p.unit_name for p in recovered.programs), recovered.slice_name]:
        assert not live.transport.observe(project_root=r.project.path, unit_name=name).exists
    assert not (r.project.rootfs_dir / 'generations' / 'vmroot').exists()
    r.control.request_application_launch('probe', request_id=request.request_id)
    r.evaluate()
    assert r.completed(request, 'failed').runtime_id == pending.runtime_id
    assert len(r.control.application_runtimes()) == 1


def test_live_station_log_cursor_and_invocation_isolation(live):
    """Real journal/RPC boundary; intentionally separate from local fake-reader tests."""
    from zog.box_control import BoxControl
    first=live.launch([program('logger',('/bin/sh','-c','echo station-first; /bin/sleep 30'))])
    control=BoxControl(live.project,systemd_transport=live.transport)
    deadline=time.monotonic()+10
    page=None
    while time.monotonic()<deadline:
        page=control.application_logs(first.runtime_id,'logger')
        if any(e['message']=='station-first' for e in page['entries']): break
        time.sleep(.1)
    assert page and any(e['message']=='station-first' for e in page['entries']),page
    cursor=page['next_cursor']
    following=control.application_logs(first.runtime_id,'logger',cursor=cursor)
    assert following['status']=='ok' and not following['entries']
    assert control.observe_application_runtime(first.runtime_id)['programs'][0]['status']=='observed'
    live.finish(first)
    second=live.launch([program('logger',('/bin/sh','-c','echo station-second; /bin/sleep 30'))])
    assert second.programs[0].invocation_id != first.programs[0].invocation_id
    with pytest.raises(RuntimeOperationError):
        control.application_logs(second.runtime_id,'logger',cursor=cursor)
    history=control.application_logs(first.runtime_id,'logger')
    assert all(e['message']!='station-second' for e in history['entries'])
    live.finish(second)


def test_live_station_poll_during_stop(live):
    from concurrent.futures import ThreadPoolExecutor
    from zog.box_control import BoxControl
    from zog.box_control.locking import ProjectLock
    reference=live.launch([program('logger',('/bin/sh','-c',"trap '' TERM; echo station-contention; while :; do /bin/sleep 1; done"))])
    control=BoxControl(live.project,systemd_transport=live.transport)
    # Confirm shell installed its trap before stop is sent.
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        page=control.application_logs(reference.runtime_id,'logger')
        if any(e['message']=='station-contention' for e in page['entries']):break
        time.sleep(.1)
    assert page['entries'],page
    with ProjectLock(live.project.lock_file):
        marker=live.project.state_dir/'mutation-incomplete.json'
        marker.write_text('{"schema":1,"status":"mutation-incomplete"}')
        with ThreadPoolExecutor() as pool:
            stopped=pool.submit(live.transport.stop,project_root=live.project.path,unit_name=reference.programs[0].unit_name)
            time.sleep(.5)
            started=time.monotonic()
            try:
                page=control.application_logs(reference.runtime_id,'logger')
                status=page['status']
            except RuntimeOperationError:
                status='transport-timeout'
            elapsed=time.monotonic()-started
            stopped.result(timeout=30)
    destination=Path.cwd()/'evidence';destination.mkdir(exist_ok=True)
    (destination/'contention.json').write_text(json.dumps({'poll_seconds':elapsed,'status':status},indent=2))
    # Measurement gate for introducing isolated log readers.
    assert status=='ok' and elapsed<3, (status,elapsed)
    live.finish(reference)


def test_live_structured_process_inspection(live):
    from zog.box_control import BoxControl
    from zog.box_control.locking import ProjectLock
    from zog.box_control.durability import replace_json
    reference=live.launch([program('worker',('/bin/sh','-c','/bin/sleep 60 & wait'))])
    control=BoxControl(live.project,systemd_transport=live.transport)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        result=control.application_processes(reference.runtime_id,'worker')
        if len(result['processes'])>=2:break
        time.sleep(.1)
    assert result['status']=='observed',result
    assert len(result['processes'])>=2,result
    assert any('/bin/sleep' in item['command'] for item in result['processes']),result
    assert result['expected_command']==['/bin/sh','-c','/bin/sleep 60 & wait']
    assert all(item['start_time_ticks']>0 for item in result['processes'])
    with ProjectLock(live.project.lock_file):
        assert control.application_processes(reference.runtime_id,'worker',limit=1)['truncated']
    # Exercise the build-record selection/RPC with the same verified live unit.
    # This is not a claim of launching a build job in this fixture.
    job='d'*32
    replace_json(live.project.state_dir/'build-job'/(job+'.json'),dict(schema=1,entity='job:'+job,
        request={'command':['recorded-build-command']},boot_id=reference.boot_id,
        invocation_id=reference.programs[0].invocation_id,unit_name=reference.programs[0].unit_name))
    build=control.build_job_processes(job)
    assert build['status']=='observed' and build['processes'],build
    assert build['expected_command']==['recorded-build-command']
    live.finish(reference)
    gone=control.application_processes(reference.runtime_id,'worker')
    assert gone['status']=='absent' and not gone['processes'],gone
    evidence=Path.cwd()/'evidence';evidence.mkdir(exist_ok=True)
    (evidence/'processes.json').write_text(json.dumps({'application':result,'build_record_projection':build,'after_cleanup':gone},indent=2))


def test_live_generation_read_only_and_persistent_data_survives_new_generation(live):
    """Actual kernel mount semantics; explicitly gated disposable-VM acceptance."""
    first_root = live.project.rootfs_dir / 'generations' / 'vmroot' / 'root'
    # Root-owned, mode-writable sentinel separates mount protection from DAC.
    sentinel = first_root / 'software-sentinel'
    sentinel.write_text('immutable')
    sentinel.chmod(0o666)
    second_root = live.project.rootfs_dir / 'generations' / 'vmroot2' / 'root'
    shutil.copytree(first_root, second_root, symlinks=True)
    (second_root / '.box-control-rootfs.json').write_text(json.dumps({'fingerprint':'vmroot2'}))
    storage_id = uuid.uuid4().hex
    for index, (generation, root) in enumerate((('vmroot', first_root), ('vmroot2', second_root))):
        command = ('/bin/sh', '-c',
            'if (echo changed > /software-sentinel) 2>/dev/null; then exit 91; fi; '
            'echo retained >> /data/value; echo ready > /data/ready; exec /bin/sleep 60')
        application = ApplicationSpec(name='mount-proof', persistent=True, storage_id=storage_id,
            programs=(ProgramSpec(name='writer', user='65534', group='65534', command=command, mounts=(('data','/data'),)),))
        reference = live.runtime.launch(application, instance_id='mount-proof', generation=generation,
            generation_root=root, references=live.references, store=live.store,
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        data = live.project.mounts_dir / 'persistent' / storage_id / 'data'
        deadline = time.monotonic() + 5
        while not (data / 'ready').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert (data / 'ready').exists(), 'software write unexpectedly succeeded or program failed'
        assert (data / 'value').read_text().splitlines() == ['retained'] * (index + 1)
        assert (root / 'software-sentinel').read_text() == 'immutable'
        live.finish(reference)
        (data / 'ready').unlink()
    assert sentinel.read_text() == 'immutable'


def test_live_nested_writable_overlay_preserves_parent_and_state(live):
    """Nested writable state overlays a read-only rootfs parent without mutation."""
    from zog.box_control import BoxControl
    from zog.image_build.filesystem import inventory as filesystem_inventory

    root = live.project.rootfs_dir / "generations" / "vmroot" / "root"
    target = root / "var/lib/nested-state"
    target.mkdir(parents=True, exist_ok=True)
    image_before = filesystem_inventory(root)
    storage_id = uuid.uuid4().hex
    data = live.project.mounts_dir / "persistent" / storage_id / "data"
    observations = []
    runtime_ids = []

    for marker in ("first", "second"):
        application = ApplicationSpec(
            name="nested-state",
            persistent=True,
            storage_id=storage_id,
            programs=(
                ProgramSpec(
                    name="writer",
                    user="65534",
                    group="65534",
                    command=(
                        "/bin/sh",
                        "-c",
                        f"echo {marker} >> /var/lib/nested-state/value; "
                        "echo ready > /var/lib/nested-state/ready; "
                        "exec /bin/sleep 60",
                    ),
                    mounts=(("data", "/var/lib/nested-state"),),
                ),
            ),
        )
        reference = live.runtime.launch(
            application,
            instance_id="nested-state",
            generation="vmroot",
            generation_root=root,
            references=live.references,
            store=live.store,
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
        deadline = time.monotonic() + 10
        while not (data / "ready").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert (data / "ready").exists()

        observation = BoxControl(
            live.project, systemd_transport=live.transport
        ).observe_application_runtime_mounts(reference.runtime_id)
        assert observation["status"] == "observed"
        mounts = {
            item["destination"]: item
            for item in observation["programs"][0]["mounts"]
        }
        assert mounts["/var"]["observed_access"] == "read-only"
        assert mounts["/var"]["backing_identity"] == "verified"
        assert mounts["/var/lib/nested-state"]["observed_access"] == "read-write"
        assert mounts["/var/lib/nested-state"]["backing_identity"] == "verified"
        observations.append(observation)
        runtime_ids.append(reference.runtime_id)

        live.finish(reference)
        after = BoxControl(
            live.project, systemd_transport=live.transport
        ).observe_application_runtime_mounts(reference.runtime_id)
        assert after["status"] == "absent"
        (data / "ready").unlink()

    persistent_values = (data / "value").read_text().splitlines()
    assert persistent_values == ["first", "second"]
    assert filesystem_inventory(root) == image_before

    evidence = Path.cwd() / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "nested-writable-live.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "runtime_ids": runtime_ids,
                "kernel_mount_observations": observations,
                "persistent_values": persistent_values,
                "generation_unchanged": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def test_live_application_software_view_is_read_only_and_state_survives_replacement(live):
    """Actual application-view bind mounts, writable state, inspection and retention."""
    import platform

    from zog.box_control import BoxControl
    from zog.box_control.application_software import (
        ApplicationSoftwareReclaimer,
        protection_from_reference,
        resolve_binding,
    )
    from zog.box_control.images import ImageSelection
    from zog.image_build.application_software import ApplicationSoftwareStore
    from zog.image_build.application_views import ApplicationSoftwareViewStore
    from zog.image_build.filesystem import inventory as filesystem_inventory

    compatibility = {
        "schema": 1,
        "mount_contract": "applications-v1",
        "architecture": platform.machine(),
        "python": None,
        "libraries": [],
    }
    software = ApplicationSoftwareStore(live.project.state_dir)

    def publish_artifact(operation, files):
        prepared = software.begin(
            operation,
            application="software-probe",
            revision="git:" + operation[0] * 40,
            compatibility_requirements=compatibility,
            notices=(),
            provenance={"acceptance": operation},
        )
        for name, contents in files.items():
            path = prepared.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
            if name.startswith("bin/"):
                path.chmod(0o755)
        return software.finalize(operation)

    executable = publish_artifact(
        "application-software-executable",
        {
            "bin/probe": (
                "#!/bin/sh\n"
                "set -eu\n"
                "if (echo mutated >> \"$0\") 2>/dev/null; then exit 91; fi\n"
                "if (echo created > /applications/software-probe/created) 2>/dev/null; then exit 92; fi\n"
                "IFS= read -r revision < /applications/software-probe/share/revision.txt\n"
                "echo \"$revision\" >> /data/value\n"
                "echo ready > /data/ready\n"
                "exec /bin/sleep 60\n"
            )
        },
    )
    revision_one = publish_artifact(
        "application-software-revision-one",
        {"share/revision.txt": "view-one\n"},
    )
    views = ApplicationSoftwareViewStore(live.project.state_dir)
    view_one = views.publish(
        "application-software-view-one",
        application="software-probe",
        artifacts=[executable.artifact, revision_one.artifact],
    )

    root = live.project.rootfs_dir / "generations" / "vmroot" / "root"
    assert not (root / "applications").exists()
    image_before = filesystem_inventory(root)
    artifact_before = {
        artifact.artifact: filesystem_inventory(artifact.root)
        for artifact in (executable, revision_one)
    }

    selection = ImageSelection(
        "vmroot",
        root,
        {
            "kind": "image",
            "application_software_compatibility": compatibility,
        },
        reused=True,
    )
    storage_id = uuid.uuid4().hex
    program_spec = ProgramSpec(
        name="probe",
        user="65534",
        group="65534",
        command=("/applications/software-probe/bin/probe",),
        mounts=(("data", "/data"),),
    )

    def start(view):
        application = ApplicationSpec(
            name="software-probe",
            programs=(program_spec,),
            persistent=True,
            writable_mounts=("data",),
            storage_id=storage_id,
            software_view=view.view,
        )
        bound = resolve_binding(live.project, application, selection)
        reference = live.runtime.launch(
            bound,
            instance_id="software-probe",
            generation="vmroot",
            generation_root=root,
            references=live.references,
            store=live.store,
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
        data = live.project.mounts_dir / "persistent" / storage_id / "data"
        deadline = time.monotonic() + 10
        while not (data / "ready").exists() and time.monotonic() < deadline:
            if reference.programs[0].result not in (None, "", "success"):
                break
            time.sleep(.05)
        assert (data / "ready").exists(), (
            "application software or view unexpectedly became writable, "
            "or the mounted executable failed"
        )
        observation = live.transport.observe(
            project_root=live.project.path,
            unit_name=reference.programs[0].unit_name,
        )
        observed_properties = observation.property_map()
        expected_properties = dict(reference.programs[0].expected_properties)
        assert observed_properties["BindReadOnlyPaths"] == expected_properties["BindReadOnlyPaths"]
        assert observed_properties["PrivateMounts"] is True
        control = BoxControl(
            live.project, systemd_transport=live.transport
        )
        inspection = control.application_runtime_mounts(reference.runtime_id)
        assert inspection["software_view"] == view.view
        assert inspection["software_binding_digest"]
        assert inspection["software_manifest_digest"]
        assert inspection["software_artifacts"]
        assert inspection["read_only_software_mounts"]
        assert inspection["writable_state_destinations"] == ["/data"]
        mount_observation = control.observe_application_runtime_mounts(
            reference.runtime_id
        )
        assert mount_observation["schema"] == "zog-runtime-mount-observation-v1"
        assert mount_observation["status"] == "observed"
        program_observation = mount_observation["programs"][0]
        assert program_observation["status"] == "observed"
        observed_mounts = {
            item["destination"]: item
            for item in program_observation["mounts"]
        }
        assert observed_mounts["/applications/software-probe"]["observed_access"] == "read-only"
        assert observed_mounts["/applications/software-probe"]["backing_identity"] == "verified"
        assert observed_mounts["/data"]["observed_access"] == "read-write"
        assert observed_mounts["/data"]["backing_identity"] == "verified"
        artifact_destinations = [
            destination
            for destination in observed_mounts
            if destination.startswith("/applications/.store/")
        ]
        assert artifact_destinations
        assert all(
            observed_mounts[destination]["observed_access"] == "read-only"
            and observed_mounts[destination]["backing_identity"] == "verified"
            for destination in artifact_destinations
        )
        return (
            reference,
            data,
            observed_properties["BindReadOnlyPaths"],
            inspection,
            mount_observation,
        )

    executable_before = (executable.root / "bin/probe").read_text()
    first, data, first_mounts, first_inspection, first_kernel_mounts = start(view_one)
    assert (data / "value").read_text().splitlines() == ["view-one"]
    assert (executable.root / "bin/probe").read_text() == executable_before
    protected_views, protected_artifacts = protection_from_reference(first)
    assert ApplicationSoftwareReclaimer(live.project).reclaim_unreferenced(
        protected_views,
        protected_artifacts,
    ) == ((), ())
    assert views.resolve(view_one.view).view == view_one.view
    live.finish(first)
    first_after_stop = BoxControl(
        live.project, systemd_transport=live.transport
    ).observe_application_runtime_mounts(first.runtime_id)
    assert first_after_stop["status"] == "absent"
    assert not (
        live.project.mounts_dir / "runtime-roots" / first.runtime_id
    ).exists()
    assert filesystem_inventory(root) == image_before
    assert not (root / "applications").exists()
    for artifact in (executable, revision_one):
        assert filesystem_inventory(artifact.root) == artifact_before[artifact.artifact]
    (data / "ready").unlink()

    revision_two = publish_artifact(
        "application-software-revision-two",
        {"share/revision.txt": "view-two\n"},
    )
    view_two = views.publish(
        "application-software-view-two",
        application="software-probe",
        artifacts=[executable.artifact, revision_two.artifact],
    )
    revision_two_before = filesystem_inventory(revision_two.root)
    second, data, second_mounts, second_inspection, second_kernel_mounts = start(view_two)
    assert (data / "value").read_text().splitlines() == ["view-one", "view-two"]
    assert (executable.root / "bin/probe").read_text() == executable_before
    assert first.runtime_id != second.runtime_id
    assert first.software_view == view_one.view
    assert second.software_view == view_two.view

    evidence = Path.cwd() / "evidence"
    evidence.mkdir(exist_ok=True)
    (evidence / "application-software-live.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "first_runtime_id": first.runtime_id,
                "second_runtime_id": second.runtime_id,
                "view_one": view_one.view,
                "view_two": view_two.view,
                "view_one_artifacts": list(first.software_artifacts),
                "view_two_artifacts": list(second.software_artifacts),
                "first_observed_bind_read_only_paths": first_mounts,
                "second_observed_bind_read_only_paths": second_mounts,
                "first_inspection": first_inspection,
                "second_inspection": second_inspection,
                "first_kernel_mount_observation": first_kernel_mounts,
                "first_after_stop_mount_observation": first_after_stop,
                "second_kernel_mount_observation": second_kernel_mounts,
                "second_after_stop_mount_observation": second_after_stop,
                "persistent_values": (data / "value").read_text().splitlines(),
                "immutable_executable_unchanged": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    live.finish(second)
    second_after_stop = BoxControl(
        live.project, systemd_transport=live.transport
    ).observe_application_runtime_mounts(second.runtime_id)
    assert second_after_stop["status"] == "absent"
    assert not (
        live.project.mounts_dir / "runtime-roots" / second.runtime_id
    ).exists()
    assert filesystem_inventory(root) == image_before
    assert not (root / "applications").exists()
    assert filesystem_inventory(executable.root) == artifact_before[executable.artifact]
    assert filesystem_inventory(revision_two.root) == revision_two_before



@pytest.mark.skipif(
    not (os.environ.get("ZOG_VM_PREPARATION_USER")
         and os.environ.get("ZOG_VM_PREPARATION_GROUP")),
    reason="real preparation identity acceptance requires an explicit existing non-root host principal",
)
def test_live_fast_exit_preparation_completion_survives_unit_unload(live):
    """Opt-in real systemd end-to-end test, never a production grant provisioner.

    The host administrator chooses an existing non-root test principal; this
    test creates no accounts and uses a unique disposable Project/rootfs copy.
    """
    import grp
    import pwd

    from zog.box_control.api import BoxControl
    from zog.box_control.application_storage import ApplicationStorage

    user = os.environ["ZOG_VM_PREPARATION_USER"]
    group = os.environ["ZOG_VM_PREPARATION_GROUP"]
    account = pwd.getpwnam(user) if not user.isdecimal() else pwd.getpwuid(int(user))
    gid = int(group) if group.isdecimal() else grp.getgrnam(group).gr_gid
    assert account.pw_uid != 0 and gid != 0, "test must execute as a non-root principal"

    directory = live.project.application_dir / "fast-preparation"
    directory.mkdir(parents=True)
    declaration = (
        "application(name='fast-preparation', persistent=True, "
        "writable_mounts=('data',), preparation_revision='canary-1', "
        "preparation=(program(name='instant', command=('/bin/true',), "
        "execution_timeout_seconds=60, mounts={'data':'/data'}, "
        f"user={user!r}, group={group!r}),), "
        "programs=(program(name='observer', command=('/bin/sleep','30'), "
        "mounts={'data':'/data'}, "
        f"user={user!r}, group={group!r}),))"
    )
    (directory / "application.py").write_text(declaration, encoding="utf-8")
    generation_root = live.project.rootfs_dir / "generations" / "vmroot" / "root"
    image_provider = SimpleNamespace(
        ensure=lambda project: SimpleNamespace(
            generation="vmroot", root=generation_root,
            manifest={"fingerprint": "vmroot", "packages": []},
            previous_manifest=None, reused=True,
        )
    )
    control = BoxControl(
        live.project, image_provider=image_provider,
        systemd_transport=live.transport,
        boot_id_provider=lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    )
    prepared = control.prepare_application("fast-preparation")
    assert prepared["state"] == "preparing"
    step = prepared["steps"][0]
    reference_before = RuntimeReferenceStore(live.project.runtime_reference_file).load()[
        step["runtime_id"]
    ]
    assert reference_before.state == ApplicationRuntimeState.TERMINATED
    assert not reference_before.cleanup_pending
    assert reference_before.programs[0].result == "success"
    assert reference_before.programs[0].invocation_id
    assert not live.transport.observe(
        project_root=live.project.path,
        unit_name=reference_before.programs[0].unit_name,
    ).exists
    completed = control.refresh_application_preparation("fast-preparation")
    assert completed["state"] == "ready"
    assert completed["steps"][0]["invocation_id"] == reference_before.programs[0].invocation_id
    assert completed["steps"][0]["completion_source"] == "durable-terminal-runtime"
    assert not completed["cleanup_pending"]
    assert ApplicationStorage(live.project).load("fast-preparation")["state"] == "ready"
    assert RuntimeReferenceStore(live.project.runtime_reference_file).load()[
        step["runtime_id"]
    ].programs[0].invocation_id == reference_before.programs[0].invocation_id
