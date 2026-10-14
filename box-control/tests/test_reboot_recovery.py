"""Changed boot identity + empty fake system manager, in fresh processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('case', ['completed', 'prepared', 'uncertain', 'failed'])
def test_reboot_phases_in_fresh_processes(tmp_path, case):
    driver = Path(__file__).with_name('reboot_phases.py')
    rootfs = tmp_path / 'fixture'
    rootfs.mkdir()
    project = tmp_path / 'project'
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    common = [str(driver), '--case', case, '--project', str(project)]
    def run(phase, boot, *extra):
        return subprocess.run([sys.executable, *common, phase, '--simulate-boot-id', boot, *extra],
                              env=environment, capture_output=True, text=True, timeout=30)
    prepared = run('prepare', 'boot-before', '--rootfs', str(rootfs))
    assert prepared.returncode == 0, prepared.stderr
    checkpoint = json.loads((project / 'reboot-checkpoint.json').read_text())
    # A lost connection alone must never count as proof of reboot.
    unchanged = run('verify', 'boot-before')
    assert unchanged.returncode != 0 and 'boot ID has not changed' in unchanged.stderr
    assert not (project / 'reboot-result.json').exists()
    verified = run('verify', 'boot-after')
    assert verified.returncode == 0, verified.stderr
    result = json.loads(verified.stdout)
    assert result['passed'] and result['simulated']
    assert result['before_boot'] == checkpoint['boot_id']
    assert result['after_boot'] == 'boot-after'
