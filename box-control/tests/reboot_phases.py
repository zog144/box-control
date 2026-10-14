"""Two-phase reboot acceptance driver, outside installed controller code.

Use separate processes for prepare and verify. Real mode requires an existing
root-control daemon and a dedicated rootfs. Simulation replaces only systemd and
the boot-ID provider; it does not claim to simulate storage power loss.
"""
import argparse
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import shutil

from zog.box_control.api import BoxControl
from zog.box_control.boot import current_boot_id
from zog.box_control.durability import replace_json
from zog.box_control.errors import RecoveryRequired
from zog.box_control.images import ImageSelection
from zog.box_control.model import ApplicationRuntimeState
from zog.box_control.project import Project
from zog.box_control.runtime.reference import RuntimeReferenceStore
from zog.box_control.runtime.root_control import RootControlSystemdTransport
from zog.box_control.runtime.systemd import SystemdServiceRuntime

CASES = ('completed', 'prepared', 'uncertain', 'failed')
REQUEST = 'REBOOT-REQUEST'


class FixtureImages:
    def ensure(self, project):
        root = (project.rootfs_dir / 'active').resolve() / 'root'
        assert root.is_dir(), 'required fixture generation missing'
        return ImageSelection(root.parent.name, root,
                              {'fingerprint': root.parent.name}, reused=True)


def simulated_transport():
    source = Path(__file__).with_name('test_application_control.py')
    spec = importlib.util.spec_from_file_location('reboot_fake', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Transport(module.FakeTransport):
        def start_service(self, **kwargs):
            super().start_service(**kwargs)
            if kwargs['definition'].command[0] == '/does-not-exist':
                name = kwargs['definition'].unit_name
                self.units[name] = replace(self.units[name], active_state='failed',
                                          sub_state='failed', result='exit-code', main_pid=None)
    return Transport()


def write_definition(project, *, broken=False, changed=False):
    directory = project.application_dir / 'probe'
    directory.mkdir(parents=True, exist_ok=True)
    second = '/does-not-exist' if broken else '/bin/sleep'
    command = (second,) if broken else (second, '240')
    (directory / 'application.py').write_text(
        'application(name="probe", start_policy="externally-controlled", programs=('
        f'program(name="peer", command=("/bin/sleep", "{300 if changed else 240}"), user="root"),'
        f'program(name="second", command={command!r}, user="root")))')


def generation(project, name, source):
    root = project.rootfs_dir / 'generations' / name / 'root'
    shutil.copytree(source, root, symlinks=True)
    replace_json(root / '.box-control-rootfs.json', {'fingerprint': name})
    active = project.rootfs_dir / 'active'
    if active.is_symlink():
        active.unlink()
    active.symlink_to('generations/' + name)


def prepare(control, case, fixture, checkpoint, simulated):
    assert not checkpoint.exists(), 'preparation already recorded; do not repeat it'
    project = control.project
    assert not project.state_dir.exists(), 'prepare requires a fresh dedicated project'
    generation(project, 'original', fixture)
    write_definition(project, broken=case == 'failed')
    old = control.launch_application('probe') if case == 'prepared' else None
    global REQUEST
    REQUEST = control.issue_application_request_id()
    request = control.request_application_launch('probe', request_id=REQUEST)

    def finish():
        record = next(r for r in control.application_operations() if r['request_id'] == REQUEST)
        reference = control.application_runtime(record['new_runtime_id'])
        if case == 'prepared':
            stopped = control.application_runtime(old.runtime_id)
            assert stopped.state == ApplicationRuntimeState.TERMINATED and not stopped.cleanup_pending
            assert not record['attempted_units']
        if case == 'failed':
            assert reference.programs[0].invocation_id
            assert reference.programs[1].result == 'exit-code' and reference.cleanup_pending
        if case == 'uncertain':
            assert record['attempted_units'] and reference.programs[0].invocation_id is None
            assert control.systemd_transport.observe(project_root=project.path,
                unit_name=reference.programs[0].unit_name).invocation_id
        # Publish changed desired inputs only after the operation is bound.
        generation(project, 'later', fixture)
        write_definition(project, changed=True)
        replace_json(checkpoint, dict(schema=1, case=case, simulated=simulated,
            boot_id=control.boot_id_provider(), runtime_id=reference.runtime_id,
            operation_id=record['operation_id'], old_runtime_id=old.runtime_id if old else None,
            failure_invocation=reference.programs[1].invocation_id if case == 'failed' else None,
            request_id=request.request_id))
        # No controller rollback/finally handlers run after the checkpoint.
        os._exit(0)

    if case == 'prepared':
        SystemdServiceRuntime.launch = lambda *args, **kwargs: finish()
    elif case == 'uncertain':
        start = control.systemd_transport.start_service
        def after_start(**kwargs):
            start(**kwargs)
            finish()
        control.systemd_transport.start_service = after_start
    elif case == 'failed':
        save = RuntimeReferenceStore.save
        def after_failure(self, references):
            save(self, references)
            if any(p.result == 'exit-code' for r in references.values() for p in r.programs):
                finish()
        RuntimeReferenceStore.save = after_failure
    report = control.evaluate()
    if case == 'completed':
        assert report.ok, report.errors
        finish()
    # Real failed exec may require observation on the next evaluation.
    control.evaluate()
    raise AssertionError('requested interruption point was not reached')


def verify(control, checkpoint, simulated):
    global REQUEST
    saved = json.loads(checkpoint.read_text())
    REQUEST = saved['request_id']
    assert saved['simulated'] == simulated, 'cannot mix simulation and real phases'
    assert saved['boot_id'] != control.boot_id_provider(), 'boot ID has not changed'
    project = control.project
    before = control.application_runtime(saved['runtime_id'])
    # Before recovery, the new system manager must have none of the old units.
    for name in [*(p.unit_name for p in before.programs), before.slice_name]:
        assert not control.systemd_transport.observe(project_root=project.path, unit_name=name).exists
    report = control.evaluate()
    case = saved['case']
    if case == 'uncertain':
        assert not report.ok and 'outcome unknown' in str(report.errors), report
        assert control.application_request_result(REQUEST) is None
        assert (project.state_dir / 'mutation-incomplete.json').exists()
        assert (project.rootfs_dir / 'generations' / 'original').exists()
        try:
            control.request_application_launch('probe', request_id='MUST-BLOCK')
        except RecoveryRequired:
            pass
        else:
            raise AssertionError('uncertain operation allowed another mutation')
        assert control.application_runtime(saved['runtime_id'])  # Inspection remains available.
        control.abandon_application_operation(saved['operation_id'])
        assert control.evaluate().ok
        expected = 'abandoned'
    else:
        assert report.ok, report.errors
        expected = 'failed' if case == 'failed' else 'satisfied'
    result = control.application_request_result(REQUEST)
    assert result.status.value == expected and result.runtime_id == saved['runtime_id']
    reference = control.application_runtime(saved['runtime_id'])
    if case == 'prepared':
        assert reference.generation == 'original'
        assert reference.replaces_runtime_id == saved['old_runtime_id']
        assert reference.programs[0].command == ('/bin/sleep', '240')
        assert reference.boot_id == control.boot_id_provider()
        assert all(p.invocation_id for p in reference.programs)
        assert (project.rootfs_dir / 'generations' / 'original').exists()
    else:
        assert not reference.cleanup_pending
        if case == 'failed':
            assert reference.programs[1].result == 'exit-code'
            assert reference.programs[1].invocation_id == saved['failure_invocation']
        assert not (project.rootfs_dir / 'generations' / 'original').exists()
    assert not (project.state_dir / 'mutation-incomplete.json').exists()
    assert not (project.application_request_dir / (REQUEST + '.json')).exists()
    identities = set(control.application_runtimes())
    invocations = [p.invocation_id for p in reference.programs]
    control.request_application_launch('probe', request_id=REQUEST)
    repeated = control.evaluate()
    assert repeated.ok, repeated.errors
    assert control.application_request_result(REQUEST) == result
    assert set(control.application_runtimes()) == identities
    assert [p.invocation_id for p in control.application_runtime(reference.runtime_id).programs] == invocations
    if simulated:
        assert len(control.systemd_transport.started) == (2 if case == 'prepared' else 0)
    record = next(r for r in control.application_operations() if r['operation_id'] == saved['operation_id'])
    assert record['finished'] and record['published']
    return dict(case=case, simulated=simulated, before_boot=saved['boot_id'],
                after_boot=control.boot_id_provider(), status=expected, passed=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('prepare', 'verify'))
    parser.add_argument('--case', choices=CASES, required=True)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--rootfs', type=Path)
    parser.add_argument('--socket', type=Path)
    parser.add_argument('--simulate-boot-id')
    args = parser.parse_args()
    simulated = args.simulate_boot_id is not None
    if not simulated and args.socket is None:
        parser.error('real mode requires --socket for the root-control daemon')
    project = Project(args.project)
    project.require_state_allowed = lambda: None  # Dedicated acceptance workspace.
    control = BoxControl(project, image_provider=FixtureImages(),
        systemd_transport=simulated_transport() if simulated else RootControlSystemdTransport(args.socket),
        boot_id_provider=(lambda: args.simulate_boot_id) if simulated else current_boot_id)
    checkpoint = project.path / 'reboot-checkpoint.json'
    if args.phase == 'prepare':
        if args.rootfs is None:
            parser.error('prepare requires --rootfs')
        prepare(control, args.case, args.rootfs, checkpoint, simulated)
    else:
        assert json.loads(checkpoint.read_text())['case'] == args.case
        result = verify(control, checkpoint, simulated)
        replace_json(project.path / 'reboot-result.json', result)
        print(json.dumps(result))


if __name__ == '__main__':
    main()
