"""host-deploy 0.5 adapter; run from its extracted source directory as root."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

source = Path(__file__).resolve().parents[1]
state = Path(os.environ['HOST_DEPLOY_STATE_DIRECTORY']).resolve()
phase = os.environ['HOST_DEPLOY_PHASE']
evidence = state / 'evidence'
evidence.mkdir(exist_ok=True)
python = state / 'environment/bin/python'
fixture = state / 'rootfs'
cases = ('completed', 'prepared', 'uncertain', 'failed')


def run(name, command, *, cwd=source):
    print(name, flush=True)
    with (evidence / (name + '.log')).open('w') as log:
        subprocess.run([str(v) for v in command], cwd=cwd, stdout=log,
                       stderr=subprocess.STDOUT, check=True)


try:
    if phase == 'preparation':
        run('packages', ['dnf', 'install', '-y', 'python3.11', 'python3.11-pip', 'dbus-daemon'])
        run('venv', ['python3.11', '-m', 'venv', state / 'environment'])
        run('dependencies', [python, '-m', 'pip', 'install', '-e', source / 'image-build', '-e', source / 'root-control',
                             '-e', str(source / 'box-control') + '[test]'])
        for name in ('bin', 'etc', 'data', 'tmp', 'proc', 'sys', 'dev'):
            (fixture / name).mkdir(parents=True, exist_ok=True)
        for executable in ('/bin/sh', '/bin/sleep', '/bin/true'):
            shutil.copy2(executable, fixture / executable.lstrip('/'))
            libraries = subprocess.check_output(['ldd', executable], text=True)
            for library in re.findall(r'(/[^\s()]+)', libraries):
                destination = fixture / library.lstrip('/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(library, destination)
        (fixture / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
        (fixture / 'etc/group').write_text('root:x:0:\n')
        run('baseline', [python, '-m', 'pytest', '-q', '--junitxml=' + str(evidence / 'baseline.xml')])
        run('lifecycle', [python, '-m', 'pytest', '-v', 'box-control/tests/test_systemd_integration.py',
            '--run-systemd-integration', '--systemd-rootfs=' + str(fixture),
            '--basetemp=/tmp/zog-reboot-live', '--junitxml=' + str(evidence / 'lifecycle.xml')])
    suffix = hashlib.sha256(str(state).encode()).hexdigest()[:10]
    socket = Path('/run') / ('zog-reboot-' + suffix + '.sock')
    # Independent transient service: it survives preparation finishing, but
    # deliberately disappears on reboot and must be created on the new boot.
    run(phase + '-daemon', ['systemd-run', '--unit=zog-reboot-daemon-' + suffix,
        '--property=Type=simple', python, '-c',
        'import sys; from zog.root_control.daemon import RootControlDaemon; RootControlDaemon(sys.argv[1]).serve()', socket])
    deadline = time.monotonic() + 15
    while not socket.exists() and time.monotonic() < deadline:
        time.sleep(.1)
    assert socket.exists(), 'root-control daemon did not create its socket'
    for case in cases:
        project = state / 'projects' / ('reboot-' + case)
        command = [python, source / 'box-control/tests/reboot_phases.py',
                   'prepare' if phase == 'preparation' else 'verify',
                   '--case', case, '--project', project, '--socket', socket]
        if phase == 'preparation':
            command.extend(['--rootfs', fixture])
        run(phase + '-' + case, command)
        path = project / ('reboot-checkpoint.json' if phase == 'preparation' else 'reboot-result.json')
        record = json.loads(path.read_text())
        assert record['simulated'] is False
        if phase == 'preparation':
            assert record['boot_id'] == os.environ['HOST_DEPLOY_BEFORE_BOOT_ID']
        else:
            assert record['passed']
            assert record['before_boot'] == os.environ['HOST_DEPLOY_BEFORE_BOOT_ID']
            assert record['after_boot'] == os.environ['HOST_DEPLOY_AFTER_BOOT_ID']
finally:
    # Keep records and definitions without copying all rootfs binaries into the
    # SSM-limited evidence archive. host-deploy snapshots this separately per phase.
    projects = state / 'projects'
    if projects.exists():
        destination = evidence / (phase + '-projects')
        shutil.copytree(projects, destination, dirs_exist_ok=True, symlinks=True,
                        ignore=shutil.ignore_patterns('rootfs', 'mounts'))
    run(phase + '-journal', ['journalctl', '-b', '--no-pager', '-n', '1000'])
