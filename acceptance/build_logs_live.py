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

def execute(folder, name, command, timeout=30):
    from zog.image_build.box_control_adapter import BuildExecutionPending
    req=BuildExecutionRequest(folder/'root',folder/'source',folder/'output',tuple(command),
        {'PATH':'/usr/bin:/bin','HOME':'/tmp','LANG':'C'},'/image-build/source',timeout)
    try:
        BoxControlExecutionAdapter(control,**dict(config,wait_timeout_seconds=0)).execute_for_attempt(req,folder/(name+'.controller.json'))
    except BuildExecutionPending:
        pass
    saved=json.loads((folder/(name+'.controller.json')).read_text())
    return control.inspect_build_job(saved['job_id'])


def logs(job, expected, cursor=None):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        page=control.build_job_logs(job['job_id'],cursor=cursor,limit=100)
        if page['status']=='ok' and any(expected in (e['message'] or '') for e in page['entries']):
            return page
        time.sleep(.2)
    raise AssertionError((expected,page))


def finish(job, expected):
    deadline=time.monotonic()+45
    while not job['process_cleanup_complete'] and time.monotonic()<deadline:
        time.sleep(.2)
        job=control.refresh_build_job(job['job_id'])
    assert job['outcome']==expected and job['process_cleanup_complete'],job
    (evidence/(job['job_id']+'.json')).write_text(json.dumps(job,indent=2))
    return job

try:
    for _ in range(100):
        if socket.exists(): break
        time.sleep(.1)
    seed=project.path/'seed'
    release=subprocess.check_output(['rpm','-q','--queryformat','%{VERSION}','system-release'],text=True).strip()
    subprocess.run(['dnf','--installroot='+str(seed),'--releasever='+release,'install','-y',
                    'gcc','binutils','coreutils','bash','make'],check=True)
    for item in seed.rglob('*'):
        if not item.is_symlink(): item.chmod(stat.S_IMODE(item.stat().st_mode) & 0o777)
    folder=project.state_dir/'image-build/attempts/fixture'
    folder.mkdir(parents=True)
    shutil.copytree(seed,folder/'root',symlinks=True)
    for n in ('source','output'): (folder/n).mkdir()
    (folder/'source/hello.c').write_text('#include <stdio.h>\nint main(void){puts("compiled-program-output");return 0;}\n')
    (folder/'source/Makefile').write_text('all:\n\t$(CC) hello.c -o hello\ninstall:\n\tcp hello /image-build/output/hello\n')
    jobs=[]
    for name,command in [
        ('configure',['/bin/sh','-c','echo configure-begin; touch configured; sleep 2; echo configure-end']),
        ('compile',['/bin/sh','-c','test -f configured && echo compile-begin && make && sleep 3 && echo compile-end']),
        ('install',['/bin/sh','-c','echo install-begin; make install && /image-build/output/hello; echo install-end'])]:
        job=execute(folder,name,command)
        first=logs(job,name+'-begin')
        if name=='compile': assert not control.inspect_build_job(job['job_id'])['process_cleanup_complete']
        job=finish(job,'success'); jobs.append(job)
        last=logs(job,name+'-end')
        if name=='compile':
            follow=logs(job,'compile-end',first['next_cursor'])
            assert all('compile-begin' not in (e['message'] or '') for e in follow['entries'])
        results.append(dict(case=name,job_id=job['job_id'],logs=last))
    assert len({j['invocation_id'] for j in jobs})==3
    assert (folder/'output/hello').exists()
    try: control.build_job_logs(jobs[1]['job_id'],cursor=results[0]['logs']['next_cursor'])
    except Exception as exc:
        assert 'cursor belongs to another log stream' in str(exc),exc
    else: raise AssertionError('cross-job cursor accepted')
    for name,script,outcome,timeout in [
        ('nonzero','echo nonzero-evidence; sleep 1; exit 7','nonzero-exit',30),
        ('timeout','echo timeout-evidence; sleep 60 & wait','timeout',2),
        ('cancel','trap "" TERM; echo cancel-evidence; while :; do sleep 1; done','cancelled',30)]:
        job=execute(folder,name,['/bin/sh','-c',script],timeout)
        logs(job,name+'-evidence')
        if name=='cancel':
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(control.cancel_build_job,job['job_id'])
                time.sleep(.3)
                assert not pending.done(), "cancellation finished before concurrent poll"
                started=time.monotonic(); page=control.build_job_logs(jobs[0]['job_id']); elapsed=time.monotonic()-started
                assert page['status']=='ok' and elapsed<2,(page,elapsed)
                job=pending.result(timeout=30)
                results.append(dict(case='poll-during-cancel',seconds=elapsed))
        job=finish(job,outcome);jobs.append(job)
        page=logs(job,name+'-evidence')
        if name=='nonzero': assert job['exit_code']==7
        results.append(dict(case=name,outcome=job['outcome'],logs=page))
    for job in jobs: control.release_build_job(job['job_id'])
    control.release_build_root(jobs[0]['request']['build_root_id'])
    for job in jobs:
        page=control.build_job_logs(job['job_id'])
        assert page['status']=='ok' and page['entries'],page
    results.append(dict(case='logs-after-resource-release',jobs=len(jobs)))
    (evidence/'result.json').write_text(json.dumps({'passed':True,'cases':results},indent=2))
finally:
    if (project.state_dir/'build-job').exists():
        shutil.copytree(project.state_dir/'build-job',evidence/'build-jobs',dirs_exist_ok=True)
    (evidence/'partial-results.json').write_text(json.dumps(results,indent=2))
    daemon.terminate(); daemon.wait(timeout=10); daemon_log.close()
