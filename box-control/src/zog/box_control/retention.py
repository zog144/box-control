"""Bounded request identity and resumable reclamation; caller holds ProjectLock."""
import hashlib
import hmac
import json
import math
import re
import secrets
import time

from .durability import replace_json, remove_file
from .errors import RecoveryRequired, RuntimeOperationError
from .operations import OperationStore
from .requests import ApplicationRequestStore, ApplicationRequestStatus
from .runtime.reference import RuntimeReferenceStore

WINDOW_SECONDS = 48 * 60 * 60
TOKEN = re.compile(r'^r1-([0-9a-f]+)-([0-9a-f]{32})-([0-9a-f]{64})$')


class Retention:
    def __init__(self, project):
        self.project = project
        self.identity = project.state_dir / 'request-identity.json'
        self.journal = project.state_dir / 'retention-incomplete.json'
        self.requests = ApplicationRequestStore(project.application_request_dir, project.application_request_result_dir)
        self.operations = OperationStore(project)

    def clock(self):
        try:
            raw = json.loads(self.identity.read_text()) if self.identity.exists() else {
                'schema': 1, 'key': secrets.token_hex(32), 'clock': 0.0}
            if raw['schema'] != 1 or not re.fullmatch('[0-9a-f]{64}', raw['key']):
                raise ValueError('invalid identity metadata')
            now = max(float(raw['clock']), time.time())
            if not math.isfinite(now) or now < 0:
                raise ValueError('invalid retention clock')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryRequired(f'cannot read request identity metadata: {exc}') from exc
        raw['clock'] = now
        # Persist the high-water mark before any expiry or acknowledgement.
        replace_json(self.identity, raw)
        return raw

    def issue(self):
        raw = self.clock()
        body = f"r1-{int(raw['clock'] * 1000):x}-{secrets.token_hex(16)}"
        signature = hmac.new(bytes.fromhex(raw['key']), body.encode(), hashlib.sha256).hexdigest()
        return body + '-' + signature

    @staticmethod
    def deadline(identity, completed):
        match = TOKEN.fullmatch(identity or '')
        issued = int(match[1], 16) / 1000 if match else 0
        return max(completed, issued) + WINDOW_SECONDS

    def validate(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[A-Za-z0-9-]{1,200}', identity):
            raise RuntimeOperationError('invalid request identity')
        raw = self.clock()
        # Existing legacy identities remain retryable until their records expire.
        result = self.requests.result(identity)
        records = [r for r in self.operations.records() if r['request_id'] == identity]
        unresolved = any(not r['finished'] or not r['published'] for r in records)
        queued = (self.project.application_request_dir / (identity + '.json')).exists()
        if unresolved or queued or (result and result.status == ApplicationRequestStatus.PENDING):
            return
        completed = result.updated_at if result else (records[0]['completed_at'] if records else None)
        if completed is not None:
            if raw['clock'] < self.deadline(identity, completed):
                return
            raise RuntimeOperationError('request identity expired; obtain a new identity')
        match = TOKEN.fullmatch(identity)
        if match:
            body, signature = identity.rsplit('-', 1)
            expected = hmac.new(bytes.fromhex(raw['key']), body.encode(), hashlib.sha256).hexdigest()
            issued = int(match[1], 16) / 1000
            if hmac.compare_digest(signature, expected) and issued <= raw['clock'] < issued + WINDOW_SECONDS:
                return
        raise RuntimeOperationError('unknown or expired request identity; obtain a controller-issued identity')

    def resume(self):
        if not self.journal.exists():
            return
        try:
            record = json.loads(self.journal.read_text())
            if record['schema'] != 1 or not isinstance(record['deletions'], list):
                raise ValueError('invalid retention journal')
            paths = []
            for kind, identity in record['deletions']:
                if kind == 'operation':
                    paths.append(self.operations.path(identity))
                elif kind == 'result' and re.fullmatch('[A-Za-z0-9-]{1,200}', identity):
                    paths.append(self.project.application_request_result_dir / (identity + '.json'))
                else:
                    raise ValueError('invalid retention deletion')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryRequired(f'cannot read retention journal: {exc}') from exc
        for path in paths:
            remove_file(path)
        remove_file(self.journal)

    def prune(self):
        self.resume()
        now = self.clock()['clock']
        records = self.operations.records()
        references = RuntimeReferenceStore(self.project.runtime_reference_file).load()
        pending_requests = {self.requests.load(path).request_id for path in self.requests.paths()}
        protected = {r['request_id'] for r in records if not r['finished'] or not r['published']}
        protected |= {r.request_id for r in references.values() if r.cleanup_pending}
        protected |= pending_requests
        protected.discard(None)
        deletions = []
        for record in records:
            complete = record['completed_at']
            result = self.requests.result(record['request_id']) if record['request_id'] else None
            if result is not None:
                complete = max(complete or 0, result.updated_at)
            if (record['finished'] and record['published'] and complete is not None
                and now >= self.deadline(record['request_id'], complete) and record['request_id'] not in protected
                and not any(references[i].cleanup_pending for i in record['runtime_ids'] if i in references)):
                deletions.append(['operation', record['operation_id']])
        for path in sorted(self.project.application_request_result_dir.glob('*.json')):
            result = self.requests.result(path.stem)
            associated = [r for r in records if r['request_id'] == result.request_id]
            scheduled = {identity for kind, identity in deletions if kind == 'operation'}
            if associated and any(r['operation_id'] not in scheduled for r in associated):
                continue
            if (result.status != ApplicationRequestStatus.PENDING and result.request_id not in protected
                and now >= self.deadline(result.request_id, result.updated_at)):
                deletions.append(['result', result.request_id])
        if deletions:
            # Freeze the deletion set before its first unlink. Recovery finishes
            # this set before lifecycle recovery, so no deleted result is rebuilt.
            replace_json(self.journal, {'schema': 1, 'deletions': deletions})
            self.resume()
