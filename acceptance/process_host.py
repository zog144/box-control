"""Minimal live-journal acceptance; avoids rebuilding a distribution seed."""
import json
from pathlib import Path
import re
import shutil
import subprocess

root=Path.cwd(); evidence=root/'evidence';evidence.mkdir(exist_ok=True)
env=root/'environment'; fixture=root/'fixture'
def run(args):subprocess.run([str(a) for a in args],check=True)
run(['dnf','install','-y','python3.11','python3.11-pip','dbus-daemon'])
run(['python3.11','-m','venv',env])
python=env/'bin/python'
run([python,'-m','pip','install','-e','../dependencies/zog144/image-build','-e','../dependencies/zog144/root-control','-e','./box-control[test]'])
for name in ('bin','etc','data','tmp','proc','sys','dev'):(fixture/name).mkdir(parents=True,exist_ok=True)
for executable in ('/bin/sh','/bin/sleep','/bin/true'):
    shutil.copy2(executable,fixture/executable.lstrip('/'))
    for library in re.findall(r'(/[^\s()]+)',subprocess.check_output(['ldd',executable],text=True)):
        target=fixture/library.lstrip('/');target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(library,target)
(fixture/'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\n')
(fixture/'etc/group').write_text('root:x:0:\n')
run([python,'-m','pytest','-v','box-control/tests/test_systemd_integration.py','-k','structured_process_inspection',
     '--run-systemd-integration','--systemd-rootfs='+str(fixture),
     '--junitxml='+str(evidence/'station.xml'),'-o','pythonpath=box-control/src ../dependencies/zog144/image-build'])
