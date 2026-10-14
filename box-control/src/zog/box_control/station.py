"""Station-access projections; authorization stays at the station-access boundary."""
from contextlib import nullcontext
from dataclasses import asdict
import json
import logging
import time
from .diagnostics import fault_from_exception
from .errors import LifecycleOperationPending, PersistenceError, ProjectBusy, RuntimeOperationError
from .runtime.systemd import SystemdServiceRuntime, RuntimeIntegrityError

logger = logging.getLogger('zog.box_control.operations')


def execute_operation(control, operation, target, *, request_id):
    if operation not in ('launch','restart','terminate') or not isinstance(request_id,str) or not request_id:
        raise ValueError('operation and explicit request identity are required')
    context=dict(operation=operation,target=target,request_id=request_id)
    try:
        method={'launch':control.launch_application,'restart':control.restart_application_runtime,
                'terminate':control.terminate_application_runtime}[operation]
        reference=method(target,request_id=request_id)
        result=dict(context,status='completed',runtime=asdict(reference),fault=None)
    except Exception as exc:
        status=('busy' if isinstance(exc,ProjectBusy) else
                'blocked' if isinstance(exc,PersistenceError) else
                'pending' if isinstance(exc,LifecycleOperationPending) else 'failed')
        fault=fault_from_exception(exc,request_id=request_id,
            runtime_ids=() if operation=='launch' else (target,)).to_dict()
        if status=='busy': fault.update(code='project-busy',blocking=False,guidance='retry-same-request')
        result=dict(context,status=status,runtime=None,fault=fault)
    result=json.loads(json.dumps(result,default=lambda value:value.value))
    try:
        logger.info('application operation %s: %s',operation,result['status'],extra={'zog_event':result})
    except Exception:
        pass  # Diagnostic handlers must not turn a completed operation into an API failure.
    return result


def observe_runtime(control, runtime_id):
    reference=control.application_runtime(runtime_id)
    if reference is None: raise RuntimeOperationError('unknown or pruned runtime identity')
    result=dict(runtime_id=runtime_id,observed_at=time.time(),snapshot='unlocked',programs=[],
                persisted_state=reference.state.value,cleanup_pending=reference.cleanup_pending)
    runtime=SystemdServiceRuntime(control.project,control.systemd_transport)
    try:
        boot=control.boot_id_provider()
    except Exception as exc:
        return dict(result,status='unavailable',fault=fault_from_exception(exc).to_dict())
    deadline=time.monotonic()+5
    budget=getattr(control.systemd_transport,'operation_budget',None)
    with budget(5) if budget else nullcontext():
        for member in reference.programs:
            item=dict(program=member.program,recorded_invocation_id=member.invocation_id)
            if not reference.boot_id:
                item.update(status='identity-unavailable',observation=None)
            elif reference.boot_id != boot:
                item.update(status='previous-boot',observation=None)
            elif member.invocation_id is None:
                item.update(status='identity-unavailable',observation=None)
            else:
                try:
                    if time.monotonic()>=deadline: raise TimeoutError('observation deadline exceeded')
                    observed=runtime._observe_program(member)
                    item.update(status='observed' if observed.exists else 'absent',observation=asdict(observed))
                except Exception as exc:
                    item.update(status='integrity-fault' if isinstance(exc,RuntimeIntegrityError) else 'unavailable',
                                observation=None,fault=fault_from_exception(exc,runtime_ids=(runtime_id,)).to_dict())
            result['programs'].append(item)
    result['status']='partial' if any(p['status'] in ('unavailable','integrity-fault','identity-unavailable') for p in result['programs']) else 'observed'
    return result
