"""Opt-in disposable-host acceptance through the public finite-build API.

Requires root, installed static journal-wait, Python dependencies and a C compiler.
Never run on a shared host: includes brief journald suspension and broker restarts.
"""
import json
import os
import pwd
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

from zog.box_control import BoxControl, Project
from zog.box_control.runtime.root_control import RootControlSystemdTransport

BASE=Path('/var/lib/zog-barrier-acceptance')
BASE.mkdir(exist_ok=True)
try:
    pwd.getpwuid(21001)
except KeyError:
    subprocess.run(['useradd','--uid','21001','--user-group','--no-create-home','--shell','/sbin/nologin','zog-barrier-test'],check=True)
project=Project(Path.home()/('probe'+uuid.uuid4().hex[:8]));project.path.mkdir()
management=Path('/run')/(project.path.name+'.sock')
log=(BASE/'daemon.log').open('w')
results=[]

def start_daemon():
    env=dict(os.environ,ROOT_CONTROL_SOCKET=str(management),ROOT_CONTROL_JOURNAL_BARRIER='1')
    management.unlink(missing_ok=True)
    proc=subprocess.Popen([sys.executable,'-m','zog.root_control.daemon'],env=env,stdout=log,stderr=log)
    for _ in range(100):
        if proc.poll() is not None: raise RuntimeError('root-control failed: '+(BASE/'daemon.log').read_text())
        if management.exists():return proc
        time.sleep(.05)
    raise TimeoutError('root-control did not start')

payload=BASE/'payload.c'
payload.write_text(r'''
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/wait.h>
int main(int n,char**v) {
 if(geteuid()==0)return 90;
 if(n>1 && !strcmp(v[1],"wait")) sleep(2);
 printf("43\n");fprintf(stderr,"44\n");fflush(NULL);
 if(n>1 && !strcmp(v[1],"child")){if(!fork()){puts("45");return 0;}wait(NULL);}
 if(n>1 && !strcmp(v[1],"signal"))raise(SIGTERM);
 if(n>1 && !strcmp(v[1],"long"))sleep(30);
 return n>1 && !strcmp(v[1],"failure") ? 7 : 0;
}
''')
subprocess.run(['cc','-static','-O2',str(payload),'-o',str(BASE/'payload')],check=True)
evil=BASE/'evil.c';evil.write_text('#include <unistd.h>\n#include <fcntl.h>\n__attribute__((constructor)) void bad(void){if(geteuid()==0){int f=open("/var/tmp/zog-barrier-root-breach",O_WRONLY|O_CREAT,0600);if(f>=0)close(f);}}\n')
subprocess.run(['cc','-shared','-fPIC',str(evil),'-o',str(BASE/'evil.so')],check=True)
daemon=start_daemon()
control=BoxControl(project,systemd_transport=RootControlSystemdTransport(management,timeout_seconds=15))


def run_case(name,mode='success',*,pause=False,restart=False,cancel=False,timeout=10,broker_loss=False,restart_after_exit=False):
    global daemon
    folder=project.state_dir/'image-build/attempts'/name
    for n in ('root','source','output'):(folder/n).mkdir(parents=True)
    shutil.copyfile(BASE/'payload',folder/'root/fixture');(folder/'root/fixture').chmod(0o755)
    # A fake in-root host executable must never be used by the privileged broker.
    (folder/'root/usr/bin').mkdir(parents=True)
    shutil.copyfile(BASE/'payload',folder/'root/usr/bin/journalctl')
    rid=uuid.uuid4().hex
    control.register_build_root(resource_id=rid,prepared_root=folder/'root',source_directory=folder/'source',
        output_directory=folder/'output',input_manifest_id='controlled-static-fixture',execution_user_id=21001,execution_group_id=21001)
    request=dict(build_root_id=rid,source_workspace_id=rid+'-source',output_workspace_id=rid+'-output',command=['/fixture',mode],
        environment={'LD_PRELOAD':str(BASE/'evil.so'),'PYTHONPATH':'/image-build/source','PATH':'/image-build/source'},
        working_directory='/',execution_user_id=21001,execution_group_id=21001,startup_timeout_seconds=5,
        execution_timeout_seconds=timeout,termination_grace_seconds=3,
        resource_limits={'thread-count-maximum':32,'memory-maximum-bytes':128*1024*1024},read_only_root=True,network_access=False)
    journal_pid=int(subprocess.check_output(['systemctl','show','systemd-journald','-p','MainPID','--value']))
    resume=None
    try:
        if pause:
            resume=subprocess.Popen([sys.executable,'-c',
                'import os,signal,time,sys;time.sleep(.5);os.kill(int(sys.argv[1]),signal.SIGCONT)',str(journal_pid)])
            os.kill(journal_pid,signal.SIGSTOP)
        request_id=control.issue_build_request_id()
        job=control.submit_build_job(request_id=request_id,**request)
        assert control.submit_build_job(request_id=request_id,**request)['job_id']==job['job_id']
        if restart or broker_loss:
            if restart_after_exit:time.sleep(3)
            daemon.kill();daemon.wait(timeout=5)
            if restart:
                daemon=start_daemon()
                time.sleep(3)  # Allow terminal unit to unload before controller observation.
            else:
                time.sleep(5)
                daemon=start_daemon()
        if cancel:job=control.cancel_build_job(job['job_id'])
        deadline=time.monotonic()+20
        while not job['process_cleanup_complete']:
            if time.monotonic()>deadline:raise TimeoutError(job)
            job=control.refresh_build_job(job['job_id']);time.sleep(.02)
            if broker_loss and job['outcome']=='unknown':
                assert job['journal_barrier']['status']=='unconfirmed'
                assert not job['process_cleanup_complete']
                job=control.cancel_build_job(job['job_id'])
        expected='unknown' if broker_loss else 'cancelled' if cancel else ('timeout' if timeout<2 else {'failure':'nonzero-exit','signal':'signal'}.get(mode,'success'))
        assert job['outcome']==expected,job
        if restart: assert job.get('exit_evidence_source')=='exit-hook',job
        expected_barrier='unconfirmed' if broker_loss else 'synchronized'
        assert job['journal_barrier']['status']==expected_barrier,job
        assert not Path('/var/tmp/zog-barrier-root-breach').exists()
        if not broker_loss:
            logs=control.build_job_logs(job['job_id'],limit=50)
            (BASE/(name+'-logs.json')).write_text(json.dumps(logs,indent=2))
            messages=[e.get('message') for e in logs.get('entries',[])]
            assert '43' in messages and '44' in messages,(job,logs)
        results.append(dict(case=name,job_id=job['job_id'],outcome=job['outcome'],barrier=job['journal_barrier'],invocation_id=job['invocation_id'],cleanup=True,exit_evidence_source=job.get('exit_evidence_source')))
        # Same UID and a disclosed token still cannot impersonate the exit hook.
        import hashlib
        record_path=Path('/var/lib/zog/build-resources')/hashlib.sha256(str(project.path.resolve()).encode()).hexdigest()/rid/'jobs'/(job['job_id']+'.json')
        token=json.loads(record_path.read_text())['journal_barrier_token']
        impostor=subprocess.run([sys.executable,'-c',
            "import os,socket,sys;os.setgid(21001);os.setuid(21001);s=socket.socket(socket.AF_UNIX);s.settimeout(2);s.connect('/run/zog-journal-broker/socket');s.sendall((sys.argv[1]+chr(10)).encode());print(s.recv(1).hex())",token],capture_output=True,text=True,check=True)
        assert not impostor.stdout.strip(),impostor.stdout
        control.release_build_job(job['job_id']);control.release_build_root(rid)
    finally:
        os.kill(journal_pid,signal.SIGCONT)
        if resume:resume.wait(timeout=2)

try:
    for i,mode in enumerate(('success','failure','child')):
        run_case('short-'+mode,mode,pause=True)
    run_case('signal','signal')
    run_case('timeout','long',timeout=1)
    run_case('cancel','long',cancel=True)
    run_case('restart','wait',restart=True)
    run_case('after-exit','wait',restart=True,restart_after_exit=True)
    run_case('broker-loss','wait',broker_loss=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda name:run_case(name,'wait'),('concurrent-a','concurrent-b')))
finally:
    (BASE/'results.json').write_text(json.dumps({'cases':results,'passed':len(results)==11},indent=2))
    daemon.terminate();daemon.wait(timeout=5);log.close()
print(json.dumps({'cases':len(results),'passed':len(results)==11}))
