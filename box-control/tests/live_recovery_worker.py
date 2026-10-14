"""Subprocess driver for opt-in VM tests; never installed as controller code."""
import json
from pathlib import Path
import signal
import sys

from zog.box_control.api import BoxControl
from zog.box_control.errors import RecoveryRequired
from zog.box_control.images import ImageSelection
from zog.box_control.project import Project
from zog.box_control.runtime.root_control import RootControlSystemdTransport
from zog.box_control.runtime.systemd import SystemdServiceRuntime


class FixtureImages:
    def ensure(self, project):
        generation = (project.path / 'selected-generation').read_text()
        root = project.rootfs_dir / 'generations' / generation / 'root'
        assert root.is_dir(), 'test generation was reclaimed unexpectedly'
        return ImageSelection(generation, root, {'fingerprint': generation}, reused=True)


def control_at(path, socket):
    project = Project(Path(path))
    # pytest uses a disposable /tmp project, outside the normal home layout.
    project.require_state_allowed = lambda: None
    return BoxControl(project, image_provider=FixtureImages(),
                      systemd_transport=RootControlSystemdTransport(Path(socket)))


def main():
    path, socket, point, ready_path = sys.argv[1:]
    control = control_at(path, socket)
    transport = control.systemd_transport

    def pause(**evidence):
        Path(ready_path).write_text(json.dumps(evidence))
        # The parent sends SIGKILL: no exception handling or finally can run.
        while True:
            signal.pause()

    if point == 'before-launch':
        def before_launch(self, *args, **kwargs):
            pause(runtime_id=kwargs['prepared'].runtime_id)
        SystemdServiceRuntime.launch = before_launch
    elif point == 'after-start':
        start = transport.start_service
        def after_start(**kwargs):
            start(**kwargs)
            observation = transport.observe(project_root=control.project.path,
                                            unit_name=kwargs['definition'].unit_name)
            assert observation.active and observation.invocation_id
            pause(unit_name=observation.unit_name, invocation_id=observation.invocation_id)
        transport.start_service = after_start
    elif point == 'during-cleanup':
        release = transport.release
        def during_cleanup(**kwargs):
            for reference in control.application_runtimes().values():
                for program in reference.programs:
                    if program.unit_name == kwargs['unit_name'] and program.result == 'exit-code':
                        assert reference.cleanup_pending
                        pause(runtime_id=reference.runtime_id, unit_name=program.unit_name,
                              invocation_id=program.invocation_id, result=program.result)
            return release(**kwargs)
        transport.release = during_cleanup
    elif point == 'blocked-cleanup':
        def blocked(**kwargs):
            raise RecoveryRequired('test cleanup transport unavailable')
        transport.stop = blocked

    report = control.evaluate()
    # A failed StartTransientUnit reply first leaves an uncertain operation.
    # A second public evaluation observes its failure and enters rollback.
    if point == 'during-cleanup' and not report.ok:
        report = control.evaluate()
    print(json.dumps({'ok': report.ok, 'errors': report.errors}), flush=True)
    return 0 if report.ok else 2


if __name__ == '__main__':
    sys.exit(main())
