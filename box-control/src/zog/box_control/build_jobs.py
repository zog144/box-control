"""Durable finite build jobs. Mutation callers hold the project lock."""
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time

from .boot import current_boot_id
from .durability import replace_json, remove_file, _blocked_projects
from .errors import RecoveryRequired, RuntimeOperationError, PersistenceError
from .retention import Retention, TOKEN, WINDOW_SECONDS
from .build_protocol import canonical_request, identity


class BuildJobs:
    def __init__(self, control):
        self.control = control
        self.project = control.project
        self.directory = self.project.state_dir / 'build-job'
        self.resources = self.project.state_dir / 'build-resource'
        self.transport = control.systemd_transport
        self.marker = self.project.state_dir / 'mutation-incomplete.json'

    def path(self, entity):
        kind, key = entity.split(':')
        return (self.resources if kind == 'resource' else self.directory) / (identity(key)+'.json')

    def load(self, entity):
        try:
            raw = json.loads(self.path(entity).read_text())
            if raw['schema'] != 1 or raw['entity'] != entity: raise ValueError('invalid build record')
            return raw
        except FileNotFoundError as exc:
            raise RuntimeOperationError(f'unknown build identity: {entity}') from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryRequired(f'cannot read build record {entity}: {exc}') from exc

    def save(self, raw):
        replace_json(self.path(raw['entity']), raw)

    def point(self, entity, *, preparing=False):
        replace_json(self.marker, {'schema': 3, 'build_entity': entity, 'preparing': preparing})

    def admission(self, entity=None):
        if self.marker.exists():
            marker = json.loads(self.marker.read_text())
            if marker.get('schema') == 3:
                target = marker['build_entity']
                absent = marker.get('preparing') is True and not self.path(target).exists()
                record = None if absent else self.load(target)
                if target != entity and record is not None:
                    if target.startswith('job:'):
                        record = self.refresh(record['job_id'])
                        if record['state'] == 'unknown':
                            raise RecoveryRequired('build outcome remains unknown; explicit cancellation required')
                    elif record['state'] == 'releasing':
                        self.release_root(record['resource_id'])
                    elif record['state'] != 'released':
                        self.register(record['resource_id'], **record['intent'])
                remove_file(self.marker)
                _blocked_projects.discard(self.project.path)
        for path in sorted(self.directory.glob('*.json')):
            raw = self.load('job:'+path.stem)
            if raw['state'] in {'starting', 'unknown', 'cancelling'} and raw['entity'] != entity:
                raw = self.refresh(raw['job_id'])
                if raw['state']=='unknown':
                    raise RecoveryRequired(f"build job {path.stem} has an unknown outcome")
                remove_file(self.marker)
                _blocked_projects.discard(self.project.path)

    def call(self, operation, **kwargs):
        try:
            return self.transport.build_call(operation, project_root=self.project.path, **kwargs)
        except PersistenceError:
            raise
        except Exception as exc:
            raise RecoveryRequired(f'build mechanism failed; retain resources and retry observation: {exc}') from exc

    def clock(self):
        # Separate signing domain while reusing durable clock/identity helpers.
        retention = Retention(self.project)
        retention.identity = self.project.state_dir / 'build-request-identity.json'
        return retention

    def issue(self):
        return self.clock().issue()

    @staticmethod
    def job_id(request_id):
        if not isinstance(request_id, str) or not TOKEN.fullmatch(request_id):
            raise RuntimeOperationError('controller-issued build request identity required')
        return hashlib.sha256(request_id.encode()).hexdigest()[:32]

    def validate_id(self, request_id):
        raw = self.clock().clock()
        body, signature = request_id.rsplit('-',1)
        match = TOKEN.fullmatch(request_id)
        expected = hmac.new(bytes.fromhex(raw['key']),body.encode(),hashlib.sha256).hexdigest()
        issued = int(match[1],16)/1000
        if not hmac.compare_digest(signature,expected) or not issued <= raw['clock'] < issued+WINDOW_SECONDS:
            raise RuntimeOperationError('unknown or expired build request identity')

    def register(self, resource_id, **intent):
        entity = 'resource:'+identity(resource_id)
        intent = json.loads(json.dumps(intent, default=str, allow_nan=False))
        path = self.path(entity)
        if path.exists():
            raw = self.load(entity)
            if raw['intent'] != intent: raise RuntimeOperationError('resource identity belongs to different inputs')
            if raw['state'] == 'ready': return raw
            if raw['state'] == 'released': raise RuntimeOperationError('resource registration already released')
        else:
            raw = {'schema':1, 'entity':entity, 'resource_id':resource_id, 'intent':intent, 'state':'registering'}
            self.point(entity, preparing=True)
            self.save(raw)
        self.point(entity)
        self.call('register', resource_id=resource_id, **intent)
        raw['state']='ready'
        self.save(raw)
        return raw

    def submit(self, request_id, request):
        job_id = self.job_id(request_id)
        entity = 'job:'+job_id
        try:
            request = canonical_request(request)
        except (ValueError, RuntimeError, TypeError) as exc:
            raise RuntimeOperationError(str(exc)) from exc
        if self.path(entity).exists():
            raw = self.load(entity)
            if raw['request'] != request: raise RuntimeOperationError('request identity belongs to different build intent')
            if raw.get('completed_at') is not None and self.clock().clock()['clock'] >= Retention.deadline(request_id, raw['completed_at']):
                raise RuntimeOperationError('build request identity expired')
            return raw
        self.validate_id(request_id)
        resource = self.load('resource:'+request['build_root_id'])
        if resource['state'] != 'ready': raise RecoveryRequired('build resource is not ready')
        for path in self.directory.glob('*.json'):
            prior = self.load('job:'+path.stem)
            if prior['request']['build_root_id'] == request['build_root_id'] and not prior['process_cleanup_complete']:
                raise RuntimeOperationError('build workspaces already have an unfinished writer')
        raw = dict(schema=1, entity=entity, job_id=job_id, request_id=request_id, request=request,
                   runtime_id='build-'+job_id, state='prepared', outcome=None, error=None,
                   invocation_id=None, journal_reference=None, exit_code=None, signal=None,
                   process_cleanup_complete=False, resources_released=False, completed_at=None,
                   boot_id=self.control.boot_id_provider(), cancel_requested=False)
        self.point(entity, preparing=True)
        self.save(raw)
        self.point(entity)
        info = self.call('prepare',job_id=job_id,request=request)
        raw.update(unit_name=info['unit_name'],slice_name=info['slice_name'],resolved_executable=info['resolved_executable'],state='starting')
        self.save(raw)
        try:
            self.call('start',job_id=job_id,request=request)
        except PersistenceError:
            # The durable 'starting' record prevents a retry from starting twice.
            raise
        return self.refresh(job_id)

    def refresh(self, job_id):
        raw = self.load('job:'+identity(job_id))
        if raw['process_cleanup_complete']: return raw
        self.point(raw['entity'])
        if raw['state'] == 'prepared':
            # No start intent has been saved. Resume the same bound preparation.
            info = self.call('prepare',job_id=job_id,request=raw['request'])
            raw.update(unit_name=info['unit_name'],slice_name=info['slice_name'],resolved_executable=info['resolved_executable'],state='starting',boot_id=self.control.boot_id_provider())
            self.save(raw)
            self.call('start',job_id=job_id,request=raw['request'])
        if raw['cancel_requested']:
            self.call('cancel',job_id=job_id,build_root_id=raw['request']['build_root_id'],expected_invocation_id=raw['invocation_id'])
        observed = self.call('observe', job_id=job_id,build_root_id=raw['request']['build_root_id'])
        if 'journal_barrier' in observed:
            raw['journal_barrier'] = observed['journal_barrier']
        evidence = observed.get('terminal_evidence')
        if not observed.get('exists') and evidence is not None:
            # The privileged broker recorded PID1's terminal evidence before the
            # hook returned. Absence now proves that this incarnation has drained.
            if evidence.get('boot_id') != str(raw['boot_id']).replace('-','') or raw['boot_id'] != self.control.boot_id_provider():
                raise RecoveryRequired('exit evidence boot differs')
            invocation=evidence.get('invocation_id')
            if not invocation or (raw['invocation_id'] and raw['invocation_id'] != invocation):
                raise RecoveryRequired('exit evidence Invocation identity differs')
            raw['exit_evidence_source']='exit-hook'
            observed=dict(evidence,exists=True,transient=True,active_state='inactive',cgroup_empty=True)
        if not observed.get('exists'):
            if raw['outcome'] is None:
                raw.update(state='unknown', outcome='unknown', error='attempted build unit absent; original outcome unknown')
                self.save(raw)
                return raw
            empty = True
        else:
            invocation = observed.get('invocation_id')
            if not observed.get('transient') or observed.get('fragment_path','') not in ('', '/run/systemd/transient/'+raw['unit_name']) or observed.get('drop_in_paths'):
                raise RecoveryRequired('build unit ownership evidence differs')
            if raw['boot_id'] != self.control.boot_id_provider():
                raise RecoveryRequired('build unit exists in a different boot; refusing to adopt')
            if raw['invocation_id'] and raw['invocation_id'] != invocation:
                raise RecoveryRequired('build Invocation identity differs')
            if invocation:
                raw.update(invocation_id=invocation,journal_reference=f"_BOOT_ID={raw['boot_id']} _SYSTEMD_INVOCATION_ID={invocation}")
            empty = observed.get('cgroup_empty') is True
            code, status = observed.get('ExecMainCode'), observed.get('ExecMainStatus')
            ended = bool(observed.get('ExecMainExitTimestampMonotonic'))
            if ended and code == 1:
                raw['exit_code']=status
            elif ended and code in (2,3):
                raw['signal']=status
            if raw['outcome'] in (None,'unknown') and empty and observed.get('active_state') in ('inactive','failed'):
                result = observed.get('result')
                # Cancellation can itself trigger systemd's stop timeout.
                # Preserve service_result while reporting requested cancellation.
                if raw['cancel_requested']: outcome='cancelled'
                elif result == 'timeout': outcome='timeout'
                elif not invocation or not ended: outcome='fault'
                elif code in (2,3): outcome='signal'
                elif code == 1 and status == 0 and result == 'success': outcome='success'
                elif code == 1 and status != 0 and result == 'exit-code': outcome='nonzero-exit'
                else: outcome='fault'
                raw.update(outcome=outcome,service_result=result)
            if raw['outcome'] is None:
                raw['state']='running'
        # Save all available evidence BEFORE stop/reset/unload.
        self.save(raw)
        if raw['outcome'] is not None and empty:
            if raw['outcome']=='unknown' and not raw['cancel_requested']:
                raw['state']='unknown'
                self.save(raw)
                return raw
            raw['state']='cleanup'
            self.save(raw)
            done = self.call('cleanup',job_id=job_id,build_root_id=raw['request']['build_root_id'])
            if done:
                raw.update(process_cleanup_complete=True,state='completed',completed_at=self.clock().clock()['clock'])
            self.save(raw)
        return raw

    def cancel(self, job_id):
        raw = self.load('job:'+identity(job_id))
        if raw['process_cleanup_complete']: return raw
        self.point(raw['entity'])
        raw.update(cancel_requested=True,state='cancelling')
        self.save(raw)
        self.call('cancel',job_id=job_id,build_root_id=raw['request']['build_root_id'],expected_invocation_id=raw['invocation_id'])
        return self.refresh(job_id)

    def release_job(self, job_id):
        raw = self.load('job:'+identity(job_id))
        if not raw['process_cleanup_complete']: raise RuntimeOperationError('cannot release unfinished build job')
        self.point(raw['entity'])
        raw['resources_released']=True
        self.save(raw)
        return raw

    def release_root(self, resource_id):
        raw = self.load('resource:'+identity(resource_id))
        if raw['state']=='released': return raw
        for path in self.directory.glob('*.json'):
            job=self.load('job:'+path.stem)
            if job['request']['build_root_id']==resource_id and not job['resources_released']:
                raise RuntimeOperationError('build resource still retained by a job')
        self.point(raw['entity'])
        raw['state']='releasing'
        self.save(raw)
        self.call('release',build_root_id=resource_id)
        raw['state']='released'
        raw['released_at']=self.clock().clock()['clock']
        self.save(raw)
        return raw

    def prune(self):
        journal = self.project.state_dir/'build-pruning.json'
        if not journal.exists():
            resources = [self.load('resource:'+p.stem) for p in self.resources.glob('*.json')]
            if not any(r['state']=='released' for r in resources): return
            now = self.clock().clock()['clock']
            selected=[]
            jobs=[self.load('job:'+p.stem) for p in self.directory.glob('*.json')]
            for resource in resources:
                if resource['state']!='released' or now < resource.get('released_at',now)+WINDOW_SECONDS: continue
                related=[j for j in jobs if j['request']['build_root_id']==resource['resource_id']]
                if any(not j['resources_released'] or not j['process_cleanup_complete'] or j['completed_at'] is None
                       or now < Retention.deadline(j['request_id'],j['completed_at']) for j in related): continue
                selected.append({'resource_id':resource['resource_id'],'job_ids':[j['job_id'] for j in related]})
            if not selected: return
            replace_json(journal,{'schema':1,'deletions':selected})
        raw=json.loads(journal.read_text())
        if raw['schema']!=1: raise RecoveryRequired('invalid build pruning journal')
        for item in raw['deletions']:
            identity(item['resource_id'])
            for job_id in item['job_ids']: identity(job_id)
        for item in raw['deletions']:
            self.call('forget',build_root_id=item['resource_id'])
            for job_id in item['job_ids']: remove_file(self.path('job:'+job_id))
            remove_file(self.path('resource:'+item['resource_id']))
        remove_file(journal)
