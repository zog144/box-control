"""Real local socket timing plus durable import/replay regression coverage."""
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from pathlib import Path
from types import SimpleNamespace

import pytest

from zog.box_control.errors import RootControlReplyTimeout, RuntimeOperationError
from zog.box_control.runtime.root_control import RootControlSystemdTransport
from zog.root_control.build import BuildBackend
from zog.root_control.daemon import RootControlDaemon
from zog.root_control.protocol import decode, encode
from zog.root_control.systemd import BusctlSystemdBackend


class _Systemd(BusctlSystemdBackend):
    def __init__(self):
        self.properties = None

    def version(self):
        return 250

    def start_slice(self, **kwargs):
        pass

    def _job(self, method, signature, body):
        self.properties = dict(body[2])


@pytest.fixture
def registered(tmp_path, monkeypatch):
    """Cross-repository build registration fixture owned by this integration test."""
    ownership = []
    monkeypatch.setattr(
        os,
        "chown",
        lambda path, uid, gid, **kwargs: ownership.append((Path(path), uid, gid)),
    )
    project = tmp_path / "project"
    attempt = project / "state/image-build/attempts/one"
    attempt.mkdir(parents=True)
    root, source, output = [attempt / name for name in ("root", "source", "output")]
    for path in (root, source, output):
        path.mkdir()
    (root / "bin").mkdir()
    (root / "bin/cc").write_text("fake compiler")
    (root / "bin/cc").chmod(0o755)
    (source / "input").write_text("source")
    backend = BuildBackend(_Systemd(), tmp_path / "privileged")
    backend.test_ownership = ownership
    resource_id = "b" * 32
    args = dict(
        project_root=project,
        resource_id=resource_id,
        prepared_root=root,
        source_directory=source,
        output_directory=output,
        input_manifest_id="verified-manifest",
        execution_user_id=12345,
        execution_group_id=12345,
    )
    backend.register(**args)
    request = dict(
        build_root_id=resource_id,
        source_workspace_id=resource_id + "-source",
        output_workspace_id=resource_id + "-output",
        command=["cc", "-v"],
        environment={"PATH": "/bin"},
        working_directory="/image-build/source",
        execution_user_id=12345,
        execution_group_id=12345,
        startup_timeout_seconds=5,
        execution_timeout_seconds=8,
        termination_grace_seconds=2,
        resource_limits={
            "thread-count-maximum": 32,
            "memory-maximum-bytes": 10000000,
        },
        read_only_root=True,
        network_access=False,
    )
    return backend, args, request



def test_reply_timeout_is_distinct_from_connect_failure(tmp_path, monkeypatch):
    # socketpair exercises real framing/deadlines without needing a listening
    # socket, which some local test environments prohibit.
    client_peer, server_peer = socket.socketpair()
    class Connected:
        def __enter__(self): return self
        def __exit__(self, *args): client_peer.close()
        def connect(self, path): pass
        def __getattr__(self, name): return getattr(client_peer, name)
    monkeypatch.setattr('zog.box_control.runtime.root_control.socket.socket', lambda *a: Connected())
    received = threading.Event(); finish = threading.Event()
    def serve():
        with server_peer:
            assert decode(server_peer)['operation'] == 'build_register'
            received.set(); finish.wait(3)
    thread = threading.Thread(target=serve); thread.start()
    client = RootControlSystemdTransport(tmp_path/'socket', timeout_seconds=.1)
    try:
        with pytest.raises(RootControlReplyTimeout, match='request sent; outcome may be unresolved') as error:
            client.build_call('register', project_root=tmp_path)
        assert received.is_set() and error.value.operation == 'build_register'
    finally:
        finish.set(); thread.join(3)
    class Missing(Connected):
        def connect(self, path): raise FileNotFoundError('missing')
    monkeypatch.setattr('zog.box_control.runtime.root_control.socket.socket', lambda *a: Missing())
    with pytest.raises(RuntimeOperationError, match='connect failure.*request not sent') as error:
        RootControlSystemdTransport(tmp_path/'absent', timeout_seconds=.1).version()
    assert not isinstance(error.value, RootControlReplyTimeout)


def test_progress_snapshot_and_replay_do_not_copy_again(registered, monkeypatch):
    backend, args, _ = registered
    rid=args['resource_id']; root=args['project_root']
    ready=backend.registration_status(project_root=root, resource_id=rid)
    assert ready['phase']=='ready' and ready['elapsed_seconds']>=0
    monkeypatch.setattr('zog.root_control.build.shutil.copytree',lambda *a,**k: pytest.fail('unexpected recopy'))
    assert backend.register(**args)==ready
    with pytest.raises(RuntimeError,match='already bound'):
        backend.register(**dict(args,input_manifest_id='different'))
    assert backend.registration_status(project_root=root,resource_id='e'*32)['state']=='absent'


def test_interrupted_import_is_observable_and_resumable(registered, monkeypatch):
    backend,args,_=registered
    old=args['resource_id']; args=dict(args, resource_id='e'*32)
    # Independent workspace; retain the earlier registration untouched.
    for name in ('source_directory','output_directory'):
        args[name] = args[name].with_name('new-'+args[name].name)
        args[name].mkdir()
    original=backend._tree
    def fail(*a,**kw): raise OSError('simulated interrupted sync')
    monkeypatch.setattr(backend,'_tree',fail)
    with pytest.raises(OSError): backend.register(**args)
    record=backend.registration_status(project_root=args['project_root'],resource_id=args['resource_id'])
    assert record['state']=='importing' and record['phase']=='sync-root'
    monkeypatch.setattr(backend,'_tree',original)
    assert backend.register(**args)['state']=='ready'


def test_inspection_lane_works_while_registration_waits_and_logs_late_reply(tmp_path, capsys):
    daemon=RootControlDaemon(tmp_path/'unused',systemd_backend=SimpleNamespace())
    entered=threading.Event(); finish=threading.Event()
    def register(**kw): entered.set(); assert finish.wait(3); return {'state':'ready'}
    daemon.build=SimpleNamespace(register=register,registration_status=lambda **kw: {'state':'importing','phase':'copy-root'})
    mutation_client, mutation_server=socket.socketpair()
    inspect_client, inspect_server=socket.socketpair()
    slots=(BoundedSemaphore(8),BoundedSemaphore(2))
    with ThreadPoolExecutor(max_workers=1) as m, ThreadPoolExecutor(max_workers=2) as r:
        try:
            daemon._dispatch(mutation_server,{'operation':'build_register','resource_id':'a'*32},(m,r),slots)
            assert entered.wait(2)
            daemon._dispatch(inspect_server,{'operation':'build_registration_status','resource_id':'a'*32,'project_root':str(tmp_path)},(m,r),slots)
            inspect_client.settimeout(1)
            assert decode(inspect_client)['result']['phase']=='copy-root'
            mutation_client.close() # Deadline elapsed; server must still finish.
        finally:
            finish.set(); inspect_client.close()
    events=[json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert any(e['event']=='build-registration-finish' and e['ok'] for e in events)
    assert any(e['event']=='root-control-response-undelivered' for e in events)
