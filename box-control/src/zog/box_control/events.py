"""Best-effort structured diagnostics. Durable operation records remain authoritative."""
from contextvars import ContextVar
from functools import wraps
import logging
import time
from .diagnostics import fault_from_exception

_context = ContextVar('zog_event_context', default={})
logger = logging.getLogger('zog.box_control.lifecycle')


def emit(event, **fields):
    value = {**_context.get(), 'event':event, 'timestamp':time.time(), **fields}
    try:
        logger.info(event, extra={'zog_event': value})
    except Exception:
        # A broken embedding handler must not alter a committed lifecycle action.
        pass


def operation_context(method):
    @wraps(method)
    def wrapped(self, record, references, *, recovering=False):
        token = _context.set(dict(operation_id=record['operation_id'], request_id=record['request_id'],
                                  operation=record['operation'], runtime_ids=list(record['runtime_ids'])))
        emit('recovery.started' if recovering else 'operation.started', phase=record['phase'])
        try:
            result = method(self, record, references, recovering=recovering)
            emit('recovery.returned' if recovering else 'operation.returned', phase=record['phase'],
                 finished=record['finished'], published=record['published'])
            return result
        except Exception as exc:
            emit('operation.interrupted', phase=record['phase'], fault=fault_from_exception(exc).to_dict())
            raise
        finally:
            _context.reset(token)
    return wrapped


def runtime_action(method):
    @wraps(method)
    def wrapped(self, reference, *args, **kwargs):
        fields=dict(runtime_id=reference.runtime_id, request_id=reference.request_id)
        # The active operation may target a runtime created by a different request.
        if _context.get().get('request_id') is not None: fields.pop('request_id')
        emit('runtime.'+method.__name__+'.started', **fields)
        try:
            result=method(self, reference, *args, **kwargs)
            emit('runtime.'+method.__name__+'.returned', cleanup_pending=result.cleanup_pending, **fields)
            return result
        except Exception as exc:
            emit('runtime.'+method.__name__+'.interrupted', fault=fault_from_exception(exc).to_dict(), **fields)
            raise
    return wrapped


def recorded(record):
    emit('operation.recorded',operation_id=record['operation_id'],request_id=record['request_id'],
         operation=record['operation'],phase=record['phase'],finished=record['finished'],
         published=record['published'],runtime_ids=list(record['runtime_ids']))


class EventFormatter(logging.Formatter):
    """Optional embedding-service formatter; does not install logging handlers."""
    def format(self, record):
        import json
        value = getattr(record, 'zog_event', None)
        if value is None:
            value = {'event':'log.message','timestamp':record.created,'message':record.getMessage()}
        return json.dumps({'logger':record.name,'level':record.levelname,**value},ensure_ascii=True)
