# Station boot evaluator — queued launch precedence and explicit interrupted-session recovery

**Date:** 2026-10-14  
**Owner:** box-control Regular implementation  
**Context:** [host-deploy diagnosis](https://github.com/zog144/know-file/blob/e906ab2b8814f99150dab32a412bf23aa6e07f3d/host-deploy/verification/station-boot-evaluator-2026-10-14/diagnosis.md)  
**Production status:** source-tested only; host-owned recovery/boot acceptance remains NO-GO.

## Failure modes

The host-deploy station boot evaluator was killed by its native
90-second systemd deadline after a completed launch/replacement, leaving
`state/mutation-incomplete.json` with
`{"schema":1,"status":"mutation-incomplete"}`. The last operation
records were committed/finished/published, yet that fact alone cannot
prove the encompassing evaluation completed. No existing operation ID
pointed to the incomplete session, and automatic
`OperationManager.recover()` rightly refused to delete the guard.

The original evaluation also starts a `keep-running` station when
there is no live runtime **before** processing an already queued
explicit launch request for that same application. This caused a
policy-started runtime and a subsequent unneeded replacement.

## Source contract — boot ordering

`Reconciler.reconcile()` now discovers pending valid `launch`
requests for singleton `keep-running` and `start-once-per-boot`
applications before applying their automatic policy starts. If a
suitable explicit launch is already queued, the automatic start is
deferred and the queued request is handled by the existing normal
durable operation machinery *within the same evaluation*. This
prevents a new auto-start followed by replacement.

To handle the older interrupted ordering case, the request processor
can also recognize a **previously committed policy start** created
*after* the queued request was submitted. This requires an exact
same-boot, same-generation, same application-fingerprint, still
active, nonstale singleton with a valid authoritative committed
`operation=launch` record whose `request_id` was absent and which
did not stop another runtime. It is used only when the saved
request has **no previous pending result or execution attempt**.
The original launch request ID is retained and a satisfied result
is durably recorded against that existing runtime, rather than
creating another runtime identity. A later explicitly issued
request does not meet the timestamp condition and continues to
produce an ordinary replacement, subject to termination/cleanup.

No special handling is applied to multi-instance workloads,
workspace-specific requests, malformed queued intents,
`externally-controlled` roles, restarted runtime requests or
unknown prior request outcomes. Host-deploy must not submit both
a timer policy start and a fresh launch ID for the same desired
boot state. A successful launch is not HTTP readiness.

## Source contract — explicit orphaned schema-1 session repair

The new public method is:

```python
control.recover_interrupted_session()
```

It is **explicit**, runs under the existing project lock and does
not call systemd, start or terminate services, modify request/operation
results, run software reclamation or infer serving. Automatic
`evaluate()` and ordinary mutation requests **still refuse** an
unpointed schema-1 marker.

The audited repair verifies:
- Exact, nonsymlinked standalone schema-1 mutation marker and no
  process-local durable storage fault.
- At least one complete and published authoritative lifecycle
  operation; every retained operation is terminal/finished/published.
- Runtime references exist for nonterminal/protected operation
  members and preserve operation-issued project/application,
  boot, generation, request, software and grant identities and
  immutable per-program systemd unit names.
- Every current active runtime has a committed launch operation
  in the current boot, intact nonempty invocation IDs, no
  conflicting instance and no unresolved lifecycle/cleanup status.
- Known request outcomes agree with committed operations; pending
  request outcomes or contradictory request identities block.
- Saved evaluation state is parseable and does not contradict
  immutable identities for overlapping recorded runtimes.
- Application preparation is not in a preparing/uncertain/deleting
  state; no unresolved build job or retention/reclamation journal
  is accepted.

Only after verification does it fsync an immutable audit under
`state/session-recovery/<id>.json` with the original marker digest,
saved operation IDs, runtime IDs, boot identity and retained
queued-request IDs. A two-phase durable pointer
`state/session-recovery-pending.json` protects recovery if
the marker unlink or its durability barrier is interrupted. All
ordinary mutation APIs and the `recovery_status()` inspector
recognize this pending pointer as a blocking condition. A repeat
explicit recovery can finalize the *same* approved audit; it cannot
silently replace it with a newly invented authority. Any uncertain
evidence keeps the original guard.

A completed repair returns `kernel_readiness="not-established"`.
It only means a controller mutator is now permitted to perform the
usual guarded reconcile. It says nothing about network service
health, credentials or port-443 cutover.

## Host-owned acceptance runbook

Do not manually delete or rename the marker. Keep the
station evaluator timer disabled and retain the previous 29
historical ledger files and original deployment ownership epoch.
Use the exact currently approved project owner, installed package,
immutable image software sources and root-control peer group.
Acquire the normal project lock through the public API, never
run the method concurrently with an evaluator.

1. Preserve an evidence snapshot of the marker, operation journal,
   runtime-reference store, request results, preparation records,
   state and systemd observations. Check `recovery_status()`
   read-only first. Confirm the host owns this deployment epoch
   and that no old evaluator process remains.
2. Have the trusted non-root project owner call
   `BoxControl(Project(<approved-canonical-project>)).recover_interrupted_session()`
   once. Do not modify the marker yourself. If it returns a fault,
   retain the original interlock and investigate the named
   inconsistency rather than rerunning installation or retries.
3. Confirm the returned recovery audit exists on disk, the original
   marker and pending pointer are absent, and
   `recovery_status()` no longer reports their blockers.
   Inspect all actual systemd invocation IDs and process/mount
   identity evidence again; there is **no automatic serving claim**.
4. Update the candidate to the tested box-control revision,
   explicitly coordinate service ordering so the boot request is
   queued once and evaluation is called once. Run the evaluator
   under a *measured* bounded systemd deadline that accounts for
   cold image/software validation and recovery. The original
   90-second deadline is independently unsafe: these changes
   do not remove its expensive immutable producer verification.
5. Confirm one desired station runtime, no immediate replacement,
   preserved request result and immutable image/grant identities,
   no new session marker, stable portal/scheduler observations,
   and subsequent repeated/reboot evaluation behavior.
   Root-control launch/cleanup evidence and portal HTTP acceptance
   remain separate host-deploy responsibilities.

The controller implementation makes no production host changes,
does not re-adopt any Work host ownership, does not tune a systemd
timeout, disable trusted immutable verification, prune evidence,
change nginx or switch ingress. The original 90-second timeout and
repeated image-build/root-control traversals need a separate
measured host-deploy/image-build optimization/acceptance decision.

## Source regression evidence

`box-control/tests/test_reconcile_application_model.py`:
one boot queued request; interrupted previous policy start;
later explicit replacement (fake systemd keeps teardown pending);
interrupted audit/guard recovery; reject mismatched runtime,
uncommitted authority, stale boot, unfinished operation and
wrong request result; crash after unlink prior to audit-pointer
cleanup; read-only status reports pending recovery.

`box-control/tests/test_durability.py` keeps the existing fsync,
fault-latch and guard semantics. The pinned
`.github/workflows/boot-evaluator-recovery.yml` also runs launch
cancellation tests. The CI gate uses precisely the current
box-control source plus pinned root-control and public aggregate
dependency fixture; it is not an actual station reboot, live
systemd, or native 90-second deadline acceptance.
