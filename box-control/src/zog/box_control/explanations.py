"""Station-access explanations derived solely from the recovery inspector."""
import re
import time
from .inspection import inspect_recovery

_GUIDANCE = {
    'evaluate-for-recovery': ('run-recovery', 'Run evaluation to recover the recorded operation.'),
    'evaluate-to-resolve-outcome': ('run-recovery', 'Run evaluation to determine the attempted launch outcome; do not submit a replacement request.'),
    'evaluate-for-cleanup': ('run-recovery', 'Run evaluation to continue recorded cleanup.'),
    'evaluate-for-retention-recovery': ('run-recovery', 'Run evaluation to resume interrupted record pruning.'),
    'restore-recorded-input': ('restore-recorded-input', 'Restore the exact prepared input before recovery; do not substitute a newer generation.'),
    'restore-storage-then-evaluate': ('restore-storage', 'Restore storage access, then run evaluation; never delete the mutation marker to bypass recovery.'),
    'refresh-recorded-build': ('inspect-build-recovery', 'Inspect the recorded build identity, then use its existing refresh or cancellation operation.'),
    'retry-inspection': ('retry-inspection', 'Retry inspection after the current mutation completes.'),
    'manual-investigation': ('inspect-evidence', 'Investigate the recorded evidence before attempting recovery.'),
}


def _action(guidance):
    code, message = _GUIDANCE.get(guidance, ('inspect-evidence', 'Inspect the recorded evidence before choosing a mutation.'))
    return dict(code=code, message=message, advisory=True)


def _actions(faults):
    actions={}
    for fault in faults:
        action=_action(fault['guidance'])
        actions[action['code']]=action
    return list(actions.values())


def _reason(fault):
    result=fault.to_dict()
    result['message']=result['message'][:4096]
    result['basis']='persisted-evidence'
    if result['code']=='storage-interlock':result['basis']='process-local-interlock'
    if result['code']=='inspection-busy':result['basis']='lock-observation'
    return result


def _operation(record, reasons, limit):
    terminal=record['phase'] in {'committed','failed','abandoned'}
    complete=record['finished'] and record['published']
    progress=('completed' if complete else 'awaiting-cleanup' if terminal and not record['finished']
              else 'awaiting-publication' if terminal else
              {'prepared':'prepared','stopping':'terminating','launching':'launching'}[record['phase']])
    if record['phase']=='committed':outcome='committed'
    elif record['phase'] in {'failed','abandoned'}:outcome=record['phase']
    else:outcome='not-recorded'
    linked=[f for f in reasons if f['operation_id']==record['operation_id'] or
            (not f['operation_id'] and ((f['request_id'] is not None and f['request_id']==record['request_id'])
             or bool(set(f['runtime_ids']) & set(record['runtime_ids']))))]
    if not terminal and record['start_attempt_recorded']:
        linked.append(dict(code='launch-outcome-unresolved', message='A start attempt is recorded, but no terminal operation outcome is recorded.',
                           blocking=True, guidance='evaluate-to-resolve-outcome',basis='persisted-evidence',
                           operation_id=record['operation_id'],request_id=record['request_id'],
                           runtime_ids=record['runtime_ids'],phase=record['phase'],evidence=[],causes=[]))
    actions=_actions(linked)
    if not terminal:
        actions.append(dict(code='consider-explicit-abandonment',advisory=True,
            message='If the outcome remains unresolved and you intend to abandon this operation, use abandon_application_operation with this operation ID. It rechecks safety and may leave cleanup pending.'))
    return dict(operation_id=record['operation_id'], request_id=record['request_id'],operation=record['operation'],
                target_runtime_id=record['target_runtime_id'],runtime_ids=record['runtime_ids'],
                generations=record['generations'],
                operation_protects_generations=not complete,
                recorded_phase=record['phase'],progress=progress,outcome=outcome,
                recorded_error=record['error'][:4096] if record['error'] else None,
                reasons=linked[:limit],reasons_truncated=len(linked)>limit,next_actions=actions)


def explain_recovery(project, *, operation_id=None, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer from 1 through 100')
    if operation_id is not None and (not isinstance(operation_id,str) or not re.fullmatch('[0-9a-f]{32}',operation_id)):
        raise ValueError('invalid operation identity')
    report=inspect_recovery(project)
    reasons=[_reason(f) for f in report.faults]
    records=[r for r in report.operations if operation_id is None or r['operation_id']==operation_id]
    # Operation selection does not hide project-wide blockers or faults in other records.
    operations=[_operation(r,reasons,limit) for r in records[:limit]]
    known_requests={r['request_id'] for r in report.operations if r['request_id']}
    results={r['request_id'] for r in report.results}
    queued=[dict(r,progress='result-recorded-request-retained' if r['request_id'] in results else 'accepted-unprepared',
                 next_actions=[dict(code='retry-same-request',advisory=True,
                    message='Use the same request identity to inspect/retry; evaluation resolves pending work.')])
            for r in report.requests if r['request_id'] not in known_requests]
    return dict(schema=1,observed_at=time.time(),snapshot=report.snapshot,
                mutation_status=report.mutation_status,advisory=True,
                selection_status=('not-requested' if operation_id is None else 'found' if records else
                                  'unavailable' if report.snapshot=='busy' or reasons else 'not-found'),
                operations=operations,operations_truncated=len(records)>limit,
                project_reasons=reasons[:limit],reasons_truncated=len(reasons)>limit,
                next_actions=_actions(reasons),
                requests=queued[:limit],requests_truncated=len(queued)>limit,
                runtimes=report.runtimes[:limit],runtimes_truncated=len(report.runtimes)>limit,
                build_jobs=report.build_jobs[:limit],build_jobs_truncated=len(report.build_jobs)>limit,
                build_resources=report.build_resources[:limit],build_resources_truncated=len(report.build_resources)>limit)
