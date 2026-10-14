"""Best-effort persisted recovery inspection. No transport, repair or writes."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import json
import math
import re
import stat
from pathlib import Path

from .diagnostics import Fault, fault_from_exception
from .durability import _blocked_projects
from .operations import OperationStore
from .requests import ApplicationRequestStore
from .runtime.reference import RuntimeReferenceStore


@dataclass
class RecoveryInspection:
    mutation_status: str = 'no-recorded-block'
    snapshot: str = 'unlocked'
    faults: list[Fault] = field(default_factory=list)
    operations: list[dict] = field(default_factory=list)
    runtimes: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    build_jobs: list[dict] = field(default_factory=list)
    build_resources: list[dict] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@contextmanager
def _snapshot(project, report):
    # Opening an existing lock read-only does not bootstrap state. Do not wait
    # behind a mutator or read a misleading mixture of its publication steps.
    try:
        stream = project.lock_file.open('rb')
    except FileNotFoundError:
        yield True
        return
    except OSError as exc:
        report.faults.append(fault_from_exception(exc, evidence=(str(project.lock_file),)))
        report.mutation_status = 'unknown'
        yield False
        return
    try:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            report.snapshot = 'busy'
            report.mutation_status = 'unknown'
            report.faults.append(Fault('inspection-busy', 'A project mutation is in progress.', guidance='retry-inspection'))
            yield False
            return
        except OSError as exc:
            report.mutation_status = 'unknown'
            report.faults.append(fault_from_exception(exc, evidence=(str(project.lock_file),)))
            yield False
            return
        report.snapshot = 'locked'
        yield True
    finally:
        stream.close()


def inspect_recovery(project):
    report = RecoveryInspection()
    missing = object()

    def problem(code, message, path, **details):
        report.faults.append(Fault(code, message, evidence=(str(path),), **details))

    def read(path):
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return missing
        except Exception as exc:
            problem('record-unreadable', str(exc), path, causes=fault_from_exception(exc).causes)
            return missing

    def files(directory):
        try:
            return sorted(p for p in directory.iterdir() if p.suffix == '.json')
        except FileNotFoundError:
            return []
        except OSError as exc:
            problem('directory-unreadable', str(exc), directory)
            return []

    with _snapshot(project, report) as available:
        if not available:
            return report
        store = OperationStore(project)
        records = []
        for path in files(store.directory):
            try:
                record = store.load(path)
                # Check summary fields before including a partially valid record.
                summary = {k: record[k] for k in ('operation_id', 'request_id', 'operation', 'phase', 'runtime_ids', 'finished', 'published', 'error')}
                if not isinstance(record['request_id'], (str, type(None))) or not isinstance(record['runtime_ids'], list):
                    raise ValueError('invalid operation summary identities')
                if record['error'] is not None and not isinstance(record['error'], str):
                    raise ValueError('invalid operation error')
                saved_references = RuntimeReferenceStore.decode(record['references'])
                summary.update(target_runtime_id=record['target_runtime_id'],
                    new_runtime_id=record['new_runtime_id'],
                    start_attempt_recorded=bool(record['attempted_units']),
                    generations=sorted({ref.generation for ref in saved_references.values()}))
                summary['abandonment'] = ('request-if-outcome-remains-unknown'
                    if record['phase'] in {'prepared', 'stopping', 'launching'} else 'not-applicable')
                records.append(record)
                report.operations.append(summary)
                if not record['finished'] or not record['published']:
                    guidance = 'evaluate-for-recovery'
                    if record['phase'] == 'launching' and record['attempted_units']:
                        guidance = 'evaluate-to-resolve-outcome'
                    problem('operation-unfinished', 'Operation requires recovery before new mutations.', path,
                            operation_id=record['operation_id'], request_id=record['request_id'],
                            runtime_ids=tuple(record['runtime_ids']), phase=record['phase'], guidance=guidance)
                    if record['new_runtime_id'] is not None and record['phase'] in {'prepared', 'stopping', 'launching'}:
                        root = Path(record['generation_root'])
                        try:
                            if not stat.S_ISDIR(root.stat().st_mode):
                                raise ValueError('prepared generation root is not a directory')
                        except (OSError, ValueError) as exc:
                            problem('prepared-input-unavailable', str(exc), root,
                                    operation_id=record['operation_id'], request_id=record['request_id'],
                                    phase=record['phase'], guidance='restore-recorded-input')
                elif record['error']:
                    problem('operation-failed' if record['phase'] != 'abandoned' else 'operation-abandoned',
                            record['error'], path, blocking=False, operation_id=record['operation_id'],
                            request_id=record['request_id'], runtime_ids=tuple(record['runtime_ids']), phase=record['phase'])
            except Exception as exc:
                problem('operation-unreadable', str(exc), path, causes=fault_from_exception(exc).causes)
        requests = [r['request_id'] for r in records if r['request_id'] is not None]
        if len(requests) != len(set(requests)):
            problem('operation-conflict', 'Multiple operations claim the same request.', store.directory)
        pending = [r for r in records if not r['finished'] or not r['published']]
        if len(pending) > 1:
            problem('operation-conflict', 'Multiple unfinished operations require investigation.', store.directory)

        marker_path = project.state_dir / 'mutation-incomplete.json'
        marker = read(marker_path)
        if marker is not missing:
            if isinstance(marker, dict) and marker.get('schema') == 3:
                problem('build-mutation-incomplete', 'Build mutation requires retry/refresh using its recorded identity.', marker_path, guidance='refresh-recorded-build')
            elif not isinstance(marker, dict) or marker.get('schema') not in (1, 2):
                problem('mutation-marker-invalid', 'Mutation marker is invalid.', marker_path)
            else:
                identity = marker.get('operation_id') if marker['schema'] == 2 else None
                matching = next((r for r in records if r['operation_id'] == identity), None)
                preparing = marker.get('preparing') is True and isinstance(identity, str) and re.fullmatch('[0-9a-f]{32}', identity)
                guidance = 'evaluate-for-recovery' if matching or preparing else 'manual-investigation'
                problem('mutation-incomplete', 'An interrupted mutation is recorded.', marker_path,
                        operation_id=identity if isinstance(identity, str) else None, guidance=guidance)
        if project.path in _blocked_projects:
            problem('storage-interlock', 'This process retains a storage failure interlock.', project.state_dir,
                    guidance='restore-storage-then-evaluate')

        path = project.runtime_reference_file
        raw = read(path)
        if raw is not missing:
            try:
                if not isinstance(raw, dict) or not isinstance(raw.get('runtimes'), dict):
                    raise ValueError('invalid runtime reference envelope')
                RuntimeReferenceStore.decode({'schema': raw.get('schema'), 'runtimes': {}})
                for identity, item in raw['runtimes'].items():
                    try:
                        ref = RuntimeReferenceStore.decode({'schema': raw.get('schema'), 'runtimes': {identity: item}})[identity]
                        report.runtimes.append({'runtime_id': ref.runtime_id, 'request_id': ref.request_id,
                            'state': ref.state.value, 'cleanup_pending': ref.cleanup_pending, 'generation': ref.generation,
                            'software_view': ref.software_view,
                            'software_binding_digest': ref.software_binding_digest,
                            'software_manifest_digest': ref.software_manifest_digest,
                            'software_artifacts': list(ref.software_artifacts),
                            'software_mounts': [list(pair) for pair in ref.software_mounts],
                            'error': ref.error, 'cleanup_error': ref.cleanup_error})
                        if ref.cleanup_pending and ref.state.value in {'terminating', 'terminated', 'failed'}:
                            problem('cleanup-pending', 'Runtime resources remain protected until cleanup completes.', path,
                                    blocking=False, runtime_ids=(identity,), request_id=ref.request_id, guidance='evaluate-for-cleanup')
                    except Exception as exc:
                        problem('runtime-unreadable', str(exc), path, runtime_ids=(identity,))
            except Exception as exc:
                problem('runtime-store-unreadable', str(exc), path)

        request_store = ApplicationRequestStore(project.application_request_dir, project.application_request_result_dir)
        for directory, result in ((project.application_request_dir, False), (project.application_request_result_dir, True)):
            for path in files(directory):
                try:
                    item = request_store.result(path.stem) if result else request_store.load(path)
                    if item is None:
                        raise ValueError('record disappeared during inspection')
                    summary = {'request_id': item.request_id, 'operation': item.operation.value}
                    if result:
                        summary.update(status=item.status.value, runtime_id=item.runtime_id, error=item.error)
                    (report.results if result else report.requests).append(summary)
                except Exception as exc:
                    problem('request-record-unreadable', str(exc), path)

        path = project.state_dir / 'retention-incomplete.json'
        raw = read(path)
        if raw is not missing:
            try:
                if raw['schema'] != 1 or not isinstance(raw['deletions'], list):
                    raise ValueError('invalid retention journal')
                for kind, identity in raw['deletions']:
                    if kind == 'operation':
                        store.path(identity)
                    elif kind != 'result' or not re.fullmatch('[A-Za-z0-9-]{1,200}', identity):
                        raise ValueError('invalid deletion entry')
                problem('retention-incomplete', 'Record pruning must finish before lifecycle recovery.', path, guidance='evaluate-for-retention-recovery')
            except Exception as exc:
                problem('retention-journal-invalid', str(exc), path)

        for directory, kind in ((project.state_dir/'build-job', 'job'), (project.state_dir/'build-resource', 'resource')):
            for path in files(directory):
                try:
                    raw = json.loads(path.read_text())
                    if raw['schema'] != 1 or raw['entity'] != kind+':'+path.stem:
                        raise ValueError('invalid build record identity')
                    if kind == 'job':
                        summary = {k:raw[k] for k in ('job_id','request_id','runtime_id','state','outcome','process_cleanup_complete','resources_released')}
                        report.build_jobs.append(summary)
                        if raw['state'] in ('starting','unknown','cancelling'):
                            problem('build-recovery-required', 'Build outcome requires refresh or explicit cancellation.',path,
                                    request_id=raw['request_id'],runtime_ids=(raw['runtime_id'],),guidance='refresh-recorded-build')
                    else:
                        report.build_resources.append({'resource_id':raw['resource_id'],'state':raw['state']})
                except Exception as exc:
                    problem('build-record-unreadable',str(exc),path)

        path = project.state_dir/'build-pruning.json'
        raw = read(path)
        if raw is not missing:
            problem('build-pruning-incomplete', 'Build metadata pruning requires recovery before new mutations.',path,guidance='evaluate-for-retention-recovery')

        path = project.state_dir / 'request-identity.json'
        raw = read(path)
        if raw is not missing:
            try:
                if raw['schema'] != 1 or not re.fullmatch('[0-9a-f]{64}', raw['key']) or not math.isfinite(float(raw['clock'])) or float(raw['clock']) < 0:
                    raise ValueError('invalid request identity metadata')
            except Exception:
                # Never include the signing key or malformed payload in output.
                problem('request-identity-invalid', 'Request identity metadata is invalid.', path)
        report.mutation_status = 'recovery-required' if any(f.blocking for f in report.faults) else 'no-recorded-block'
    return report
