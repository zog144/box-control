from types import SimpleNamespace
import uuid
import pytest
from zog.box_control.host_gateway import dispatch


def test_readonly_inspection_and_peer_check():
    rid=str(uuid.uuid4());request=dict(version=1,request_id=str(uuid.uuid4()),operation='inspect',runtime_id=rid)
    class Control:
        def application_runtime(self,value):
            assert value==rid
            return SimpleNamespace(state=SimpleNamespace(value='running'),cleanup_pending=False)
    result=dispatch(Control(),request,970)
    assert result['result']==dict(runtime_id=rid,state='running',cleanup_pending=False)
    with pytest.raises(PermissionError):dispatch(Control(),request,1000)
    for operation in ('launch','terminate','execute','inspect; shell'):
        with pytest.raises(ValueError):dispatch(Control(),dict(request,operation=operation),970)
    with pytest.raises(ValueError):dispatch(Control(),dict(request,project='/etc'),970)


def test_socket_peer_and_bounded_frame(monkeypatch):
    import json,struct
    from zog.box_control import host_gateway
    monkeypatch.setattr(host_gateway.os,'geteuid',lambda:971)
    class Connection:
        uid=1000
        def settimeout(self,value):pass
        def getsockopt(self,*args):return struct.pack('3i',1,self.uid,970)
        def recv(self,size):pytest.fail('wrong peer must not send input')
    with pytest.raises(PermissionError):host_gateway.serve_one(None,Connection())
    class Overflow(Connection):
        uid=970
        def recv(self,size):return b'x'*size
    with pytest.raises(ValueError):host_gateway.serve_one(None,Overflow())
    class Duplicate(Connection):
        uid=970
        def recv(self,size):return b'{"operation":"inspect","operation":"launch"}\n'
    with pytest.raises(ValueError):host_gateway.serve_one(None,Duplicate())
