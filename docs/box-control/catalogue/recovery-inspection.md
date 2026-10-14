# Recovery inspection and structured faults

`BoxControl.recovery_status()` returns a `RecoveryInspection`. Use
`status.to_dict()` for a JSON-serializable response to station-access. The result
contains operation, request, result and runtime summaries plus `Fault` values.
It does not return application environment variables, full launch snapshots or
the request signing key. Error messages may contain paths and underlying system
error details; this is a trusted local library interface, not a public endpoint.

```python
status = control.recovery_status()
for fault in status.faults:
    print(fault.code, fault.message, fault.guidance, fault.evidence)
```

`mutation_status` is `recovery-required`, `no-recorded-block`, or `unknown`.
**No-recorded-block is not permission to mutate.** It means this inspection found
no blocking evidence within its scope. Configuration, storage writability, live
systemd state and all lifecycle preconditions are checked by the mutating API.
This pass preserves the project-wide interlock and all existing recovery rules.

Inspection opens an existing project lock read-only and takes a nonblocking
shared lock. `snapshot=locked` means cooperating mutators were excluded during
inspection. A busy lock returns `snapshot=busy`, `mutation_status=unknown`, and
`inspection-busy`, without reading partially published records. If no lock file
exists, inspection returns an `unlocked` best-effort snapshot; concurrent
bootstrap is possible. Inspection never creates the lock or state directory.
External writers that ignore the project lock can invalidate any snapshot.

Inspection does not call systemd, recover operations, save observations, prune,
or clear markers. It works without a systemd connection and can be used when
writes fail. An unreadable directory or record becomes a fault while independent
readable records remain available. Runtime entries are decoded independently
when their shared JSON envelope can be read. Invalid JSON prevents recovery of
entries within that one file; the report does not guess missing data.

Faults contain stable `code`, readable `message`, `blocking`, `guidance`, optional
operation/request/runtime identities and phase, evidence paths, and available
exception causes (type/message, outermost first, maximum 16). No traceback locals
or new persistent error archive are collected. Historical records retain their
existing error strings; this pass does not reconstruct lost exception chains.

Guidance values include:

- `evaluate-for-recovery`: run normal evaluation to attempt safe recovery.
- `evaluate-to-resolve-outcome`: normal recovery must establish the launch result.
- `restore-recorded-input`: restore the exact bound input; do not substitute one.
- `evaluate-for-cleanup`: cleanup remains pending and resources stay protected.
- `evaluate-for-retention-recovery`: resume the existing durable deletion list.
- `restore-storage-then-evaluate`: restore persistence before recovery can proceed.
- `manual-investigation` / `inspect-evidence`: no safe automatic resolution is promised.
- `retry-inspection`: a mutation is currently in progress.

Unfinished nonterminal operation summaries expose
`abandonment=request-if-outcome-remains-unknown`. This describes the existing
`abandon_application_operation(operation_id)` API, not an assertion that it will
accept the request now. It rechecks records and interlocks, requires explicit
caller intent, preserves unknown outcome, and retains required cleanup.
Terminal operations report `not-applicable`.

`ReconcileReport.faults` accompanies the existing `errors` strings and unchanged
`summary()` output. Library exceptions offer `exception.diagnostic()`. A fault
in an evaluation describes why that attempt failed; only a fresh recovery
inspection describes recorded project interlocks. Cleanup-pending and historical
failed/abandoned outcomes are nonblocking inspection facts on their own. An
unfinished operation or damaged recovery evidence is blocking. Live services
normally retain cleanup protection without being reported as awaiting cleanup.

Scope excludes a generic application health metric, live process inspection,
automatic repair of corrupt data, a force-unblock API, and per-application fault
containment. There is no new persistent diagnostic log or retention policy.
