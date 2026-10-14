# Request identities and bounded retention — pass 6

Box-control issues request IDs before submission. Issuance accepts no work:

```python
identity = control.issue_application_request_id()
request = control.request_application_launch('desktop', request_id=identity)
result = control.application_request_result(identity)
```

Omitting request_id on a queue helper issues an ID automatically. Callers that
need to retry after losing the acceptance response should obtain and keep an ID
first. Direct launch/restart APIs accept an issued ID too; they durably bind its
intent in the queue before preparing the lifecycle operation. A failed direct
call may therefore leave accepted intent for the next evaluation to resolve.

An unused ID expires 48 hours after issuance. Accepted queued/unresolved work
never expires merely because it waited more than 48 hours. Completed results and
operation records remain for at least 48 hours after completion. Pending cleanup
can extend retention; finishing cleanup does not reset the outcome's completion
time. When legacy result publication has a later timestamp than its operation,
both records are retained until the later deadline. Recorded live/cleanup-pending
runtime references also protect the associated operation records.

Retries within the window return the existing outcome; different intent under
the same identity is rejected. After expiry, execution under that identity is
rejected. A status query may return None once the record has been reclaimed;
that does not authorize resubmission. Obtain a new ID for genuinely new work.

## Identity format and migration

New IDs carry an issuance timestamp, a random nonce, and an HMAC authenticated by
a project-specific key in state/request-identity.json. There is no per-unused-ID
file and no permanent list of deleted IDs. After completed records are deleted,
the ID's issuance window has necessarily elapsed, so it cannot look like a new
valid request. The key is controller state and must remain with the project.
This mechanism validates issuance; it is not network-user authorization.

Already-recorded legacy IDs remain supported until their records expire. New
caller-chosen arbitrary IDs are rejected. Existing queued legacy work continues
normally. Application requests are still files; manually dropping new files into
the queue bypasses public admission checks and is not a supported issuance API.

## Time and host unavailability

A persisted wall-clock high-water mark prevents backward clock adjustments from
resurrecting expired identities. Clock rollback can delay expiry; large forward
jumps can expire work's retry window early. Operation/result deletion waits for
both its completion deadline and the ID's issuance deadline, including during
clock adjustments. This assumes a reasonably correct host clock and storage that
honors synchronization requests. Protect and back up controller state together;
restoring an old entire-project backup is a separate recovery problem.

Host downtime counts toward completed-result and unused-ID expiry. Accepted
unresolved requests remain protected throughout downtime. Whether long host
unavailability should extend completed retry windows remains a future policy
question, as previously noted; this pass does not silently add such an extension.

## Pruning and crash recovery

Under the project lock, public mutations first resume any retention journal,
recover lifecycle operations, then prune eligible records before new mutations.
Before deleting anything, box-control durably freezes its deletion list in
state/retention-incomplete.json. Interrupted pruning repeats only those unlinks
and their directory barriers, then removes the journal. It completes before
lifecycle recovery so partially removed results cannot be reconstructed from an
operation scheduled for deletion. Storage failures prevent subsequent lifecycle
actions; no whole-project mutation marker is cleared by retention recovery.

The existing 25-entry runtime-history policy is separate and unchanged.
Compact request/operation records remain available for their retry window even
when old runtime history is pruned. Pending cleanup is never pruned. Application
mount data, journald, core dumps and image-build-owned creation are unaffected.

Pruning is opportunistic during mutating API calls and evaluation, with no timer
or daemon. Files can remain longer on an idle host. Retained storage is bounded
by the recent operation volume plus unresolved/live work and the history floor,
not by a fixed number of records.
