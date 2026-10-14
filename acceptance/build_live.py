"""Live finite-build acceptance on a disposable root-owned Amazon Linux host."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback
import stat
from types import SimpleNamespace
import uuid

from zog.box_control import BoxControl, Project
from zog.box_control.runtime.root_control import RootControlSystemdTransport
from zog.image_build import ImageBuild, load_packages
from zog.image_build.box_control_adapter import BoxControlExecutionAdapter
from zog.image_build.runner import BoxControlRunner, BuildExecutionRequest
from zog.image_build.filesystem import inventory

source=Path.cwd(); evidence=source/'evidence'; evidence.mkdir(exist_ok=True)
project=Project(Path('/root')/('zogbuild'+uuid.uuid4().hex[:8]))
project.path.mkdir(); results=[]
socket=Path('/run')/(project.path.name+'.sock')
daemon_log=(evidence/'root-control.log').open('w')
daemon=subprocess.Popen(['/root/zog-build-test-environment/bin/python','-c',
    'import sys; from zog.root_control.daemon import RootControlDaemon; RootControlDaemon(sys.argv[1]).serve()',str(socket)],
    stdout=daemon_log,stderr=subprocess.STDOUT)
control=BoxControl(project,systemd_transport=RootControlSystemdTransport(socket,timeout_seconds=90))

def manifest(request):
    return hashlib.sha256(json.dumps(inventory(request.root),sort_keys=True).encode()).hexdigest()

config=dict(execution_user_id=21001,execution_group_id=21001,startup_timeout_seconds=20,
            termination_grace_seconds=3,wait_timeout_seconds=180,
            resource_limits={'thread-count-maximum':128,'memory-maximum-bytes':1024**3},input_manifest_id=manifest)
adapter=BoxControlExecutionAdapter(control,**config)
runner=BoxControlRunner(execute=adapter,timeout=120)

def run_case(name, command, *, timeout=30, expected='success'):
    folder=project.state_dir/'image-build/attempts'/('case-'+name)
    folder.mkdir(parents=True)
    shutil.copytree(seed,folder/'root',symlinks=True)
    for n in ('source','output'): (folder/n).mkdir()
    req=BuildExecutionRequest(folder/'root',folder/'source',folder/'output',tuple(command),
        {'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C'},'/image-build/source',timeout)
    try:
        adapter.execute_for_attempt(req,folder/'command.controller.json')
    except Exception:
        traceback.print_exc()
        # Classification is asserted from authoritative evidence, not exception text.
        pass
    checkpoint=json.loads((folder/'command.controller.json').read_text())
    from zog.box_control.build_jobs import BuildJobs
    record=control.inspect_build_job(BuildJobs.job_id(checkpoint['request_id']))
    assert record['outcome']==expected, record
    assert record['process_cleanup_complete'],record
    assert record['invocation_id'],record
    results.append({'case':name,'job_id':record['job_id'],'outcome':record['outcome'],'cleanup':True})
    (evidence/(name+'.json')).write_text(json.dumps(record,indent=2))

try:
    for _ in range(100):
        if socket.exists(): break
        time.sleep(.1)
    seed=project.path/'seed'
    release=subprocess.check_output(['rpm','-q','--queryformat','%{VERSION}','system-release'],text=True).strip()
    subprocess.run(['dnf','--installroot='+str(seed),'--releasever='+release,'install','-y',
                    'gcc','glibc-static','binutils','coreutils','bash','make'],check=True)
    # A host seed contains privileged utilities; the fixture removes their set-id bits.
    for item in seed.rglob('*'):
        if not item.is_symlink(): item.chmod(stat.S_IMODE(item.stat().st_mode) & 0o777)
    run_case('isolation',['/bin/bash','-c',
        'test "$(id -u)" = 21001 && touch /image-build/source/yes /image-build/output/yes /tmp/yes && '
        '! touch /usr/forbidden && ! test -S /run/dbus/system_bus_socket && '
        '! timeout 2 /bin/bash -c "echo blocked >/dev/tcp/1.1.1.1/53"'])
    run_case('nonzero',['/bin/sh','-c','exit 7'],expected='nonzero-exit')
    run_case('signal',['/bin/sh','-c','kill -TERM $$'],expected='signal')
    run_case('timeout-descendant',['/bin/sh','-c','sleep 100 & wait'],timeout=2,expected='timeout')
    spec=importlib.util.spec_from_file_location('examples',source.parent/'dependencies/zog144/image-build/examples/create_packages.py')
    examples=importlib.util.module_from_spec(spec); spec.loader.exec_module(examples)
    examples.create(project.path/'package')
    builder=ImageBuild(package_dir=project.path/'package',state_dir=project.state_dir/'image-build',runner=runner)
    attempt=project.state_dir/'image-build/attempts/example'; attempt.mkdir(parents=True)
    packages=load_packages(project.path/'package')
    # Host-seeded integration fixture only: do not fabricate a promoted stage-2 toolchain.
    built=builder._build_set(packages,['number-print'],SimpleNamespace(root=seed,generation='host-seed'),attempt)
    builder._compose(packages,['number-print'],built,attempt/'composed')
    assert not (attempt/'composed/usr/lib/libzog-number.a').exists()
    folder=project.state_dir/'image-build/attempts/verify'; folder.mkdir(parents=True)
    for n in ('source','output'): (folder/n).mkdir()
    request=BuildExecutionRequest(attempt/'composed',folder/'source',folder/'output',('/usr/bin/number-print',),{},'/',30)
    final=adapter.execute_for_attempt(request,folder/'command.controller.json')
    journal=subprocess.check_output(['journalctl','--no-pager','-o','cat','_SYSTEMD_INVOCATION_ID='+final.invocation_id],text=True)
    assert journal.strip()=='42',journal
    results.append({'case':'image-build-example','output':journal.strip(),'build_only_library_absent':True})
    (evidence/'result.json').write_text(json.dumps({'passed':True,'cases':results,'self_hosting_tested':False},indent=2))
finally:
    if (project.state_dir/'build-job').exists():
        shutil.copytree(project.state_dir/'build-job',evidence/'build-jobs',dirs_exist_ok=True)
    (evidence/'partial-results.json').write_text(json.dumps(results,indent=2))
    daemon.terminate()
    daemon.wait(timeout=10)
    daemon_log.close()
