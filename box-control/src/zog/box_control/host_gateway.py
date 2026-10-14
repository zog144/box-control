"""Narrow host-discover gateway, v1: inspect persisted runtime identity only.

Trusted composition supplies the existing BoxControl object and listening Unix
socket. No socket creation, project selection, lifecycle action or systemd API is
exposed to a network caller. Invoke serve_one from the UID971 service supervisor.
"""
import json
import os
import socket
import struct
import uuid

SCHEMA=1


def identifier(value):
    if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError('Canonical UUID required')


def dispatch(control,request,peer_uid):
    if peer_uid!=970:raise PermissionError('Only host-discover may call this gateway')
    if type(request) is not dict or set(request)!={'version','request_id','operation','runtime_id'} or type(request['version']) is not int or request['version']!=1 or request['operation']!='inspect':
        raise ValueError('Unsupported gateway operation')
    identifier(request['request_id']);identifier(request['runtime_id'])
    reference=control.application_runtime(request['runtime_id'])
    if reference is None:raise ValueError('Unknown runtime')
    # Public immutable identity and operational state only; omit spec/env/secrets.
    result=dict(runtime_id=request['runtime_id'],state=reference.state.value,cleanup_pending=reference.cleanup_pending)
    return dict(version=1,request_id=request['request_id'],status='completed',result=result)


def serve_one(control,connection):
    if os.geteuid()!=971:raise PermissionError('Gateway server must run as box-control')
    connection.settimeout(5)
    _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    if uid!=970:raise PermissionError('Wrong peer')
    raw=bytearray()
    while len(raw)<=4096:
        data=connection.recv(min(1024,4097-len(raw)))
        if not data:break
        raw.extend(data)
        if b'\n' in raw:break
    if len(raw)>4096 or not raw.endswith(b'\n') or raw.count(b'\n')!=1:raise ValueError('Invalid gateway frame')
    def pairs(items):
        value={}
        for key,item in items:
            if key in value:raise ValueError('Duplicate field')
            value[key]=item
        return value
    result=dispatch(control,json.loads(raw,object_pairs_hook=pairs),uid)
    connection.sendall(json.dumps(result,separators=(',',':')).encode()+b'\n')
