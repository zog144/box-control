# Recovery explanations for station-access

Call `control.recovery_explanation(operation_id=None, limit=50)` for a JSON-safe,
read-only projection of the existing recovery inspector. Optional operation_id
selects one application's lifecycle operation; global reasons remain visible.
No systemd calls, state writes, repair, reconciliation or marker deletion occur.
No EC2 validation is needed for this persisted-state projection.

## Response and presentation

The response includes schema, observed_at, snapshot, mutation_status, selection_status,
operations, project_reasons, next_actions, queued requests, runtimes, build jobs and
build resources. Per-section truncation is explicit; limits are 1–100. This bounds
returned row counts, not the inspector's filesystem scan or a total JSON byte size.
For full evidence use the existing recovery_status().to_dict().

Snapshot is locked, unlocked, or busy. An existing lock is opened read-only and a
nonblocking shared lock is attempted. Busy yields unknown mutation status and a
retry-inspection action, without reading a mutator's partially published state.
An absent lock is not created: an unlocked response is only best-effort evidence.
No-recorded-block is not a guarantee that a subsequent mutation will succeed.

Each operation separates:

- recorded_phase: the authoritative stored phase;
- progress: prepared, terminating, launching, awaiting-cleanup,
  awaiting-publication, completed;
- outcome: not-recorded, committed, failed, abandoned;
- request, target runtime and affected runtime IDs;
- generation identities, with operation_protects_generations true while the
  operation is unfinished or unpublished. False does NOT imply reclaimability:
  a running runtime or pending runtime cleanup can independently retain a generation;
- recorded_error, structured reasons and advisory next_actions.

Queued requests without prepared operations appear as accepted-unprepared.
If a result exists while its request remains queued, the projection says
result-recorded-request-retained; it does not advise launching again.

Missing input, storage interlock, uncertain attempted launch, interrupted pruning,
corrupt/conflicting records and cleanup are distinct reasons. An attempted launch
without a terminal phase is unresolved evidence, not proof that the service is
absent or should be started. Inspection cannot know whether recovery will resolve it.

## Actions and uncertainty

Every action is advisory. Use existing methods such as evaluate(),
abandon_application_operation(operation_id), refresh_build_job(job_id), and
cancel_build_job(job_id); they revalidate state and permissions at mutation time.
Restore-storage and restore-recorded-input are prerequisites, not executable API
commands. Never automatically invoke abandonment or delete a marker from this view.
Abandonment is offered conditionally only for nonterminal application operations;
it can be refused by the mutation API and may require subsequent cleanup.

Historical free-text errors are displayed as recorded_error, not parsed into invented
fault types. In particular, old records may not distinguish a runtime-integrity fault
from another failure structurally. The API reports the recorded failure without
claiming that it is still a current blocker. Use existing live runtime observation
for current configuration/invocation integrity. The process-local storage interlock
has an explicit evidence basis, distinct from durable records and lock observations.

Authorize project/operation access in station-access, render messages as text, and
keep paths/stack traces in an expandable technical-details view. This pass adds no
GUI, recovery policy, automatic repair, or persistent fault history.
