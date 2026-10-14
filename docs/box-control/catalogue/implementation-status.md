# box-control Current Implementation Status

Status: pass 7 revision 1 preserves failed-launch evidence; VM acceptance pending
Input archive: `zog-application-handling-pass6.zip`
Output archive: `zog-systemd-lifecycle-pass7-revision1.zip`

## Canonical ontology and layout

- `package/` is the image-build input root.
- `application/` is the box-control runtime-composition root.
- a program is one systemd service command inside an application.
- `state/` remains controller-owned mutable state.
- `catalogue/` contains compact architecture and implementation records.
- the historical physical `program/` directory is removed.

## Application policy

`ApplicationSpec` now contains `start_policy` and `multiple_instances`.

- `START_ONCE_PER_BOOT` uses the Linux boot ID and counts every launch attempt.
- `KEEP_RUNNING` starts only when evaluation observes no live runtime; terminal
  state during launch is reported as a fault and is not retried in the same
  evaluation.
- `EXTERNALLY_CONTROLLED` leaves creation to explicit library/request actions.

Automatic launch remains a box-control decision. systemd `Restart=` remains
`no`.

## Instance and runtime identity

An application instance is distinct from an application runtime. Singleton
applications use the application name as the stable instance identity.
Multi-instance applications receive a short suffixed instance identity.

Every launch/restart receives a new runtime identity. Replacement records the
previous runtime identity. Requests against an identity superseded by a newer
runtime of the same instance are rejected. Multiple simultaneous instances are
created only when the application explicitly enables them.

## Launch transaction and history

Runtime-reference schema 3 introduced application-instance, application-fingerprint,
request, replacement, boot, command, and description fields. Schema 2 remains
readable and migrates as singleton state on save.

The runtime identity is saved before systemd mutation. Every required program
must execute and acquire an Invocation ID before launch commit. Recovery no
longer accepts a partially launched multi-program application as RUNNING: live
members are terminated and the attempt remains in unified failure history.

Successful and failed completed attempts share the fixed 25-entry history.
Current and history query APIs expose separate views without introducing a
second error-history subsystem.

## Lifecycle request protocol

Atomic JSON request files support launch, restart, and terminate. Results are
`pending`, `satisfied`, or `failed`. A request ID is persisted into newly
created runtime state, which prevents a completed multi-instance launch from
being repeated if box-control stops before clearing its request file.

Asynchronous singleton replacement preserves the target replaced-runtime
identity while termination drains. Later evaluation resumes and creates the
replacement with that relationship intact.

## Public library surface

The station-access/bootstrap-safe `BoxControl` library now exposes:

- direct launch, restart, and exact-runtime termination;
- exact runtime lookup;
- filtered current-runtime and unified-history views;
- queued lifecycle request creation and result lookup.

No operation depends on Django ORM state. Human-readable names remain
presentation metadata outside core identity.

## Pass 6 launch acceptance

Every member is checked after start and again in the final pre-commit observation.
Acceptance requires observable invocation identity, matching transient/material
properties, no failure result, and active/reloading state or inactive success.
Activating/deactivating state does not prove completed exec. The same predicate
applies to STARTING recovery. A failed member cannot be masked by an active peer.
Rollback targets every attempted unit, including a start whose reply was lost.
Failure remains attached through TERMINATING to FAILED; uncertain observation
keeps generation protection. After commit, active peers still keep the runtime
RUNNING even when another member exits.

Failed queued launch/restart results include the attempted runtime identity.
Successful short-lived launches can satisfy requests; failed recovered runtimes
cannot. Request IDs continue to prevent duplicate launch attempts.

## Writable data isolation

New writable bind sources are
`state/mounts/<application>/<runtime-id>/<program>/<mount-name>`.
The scope is the per-launch runtime identity, including on restart. Stable
instance identity does not imply data reuse. Existing references preserve old
bind paths; no automatic migration or copying is performed. Retained runtime
mount namespaces cannot be reused after history pruning. Data reclamation is
not added by this pass.

## Dependency relationship

Application `dependencies` retains its current ordering-only semantics:
validate referenced declarations and order evaluation. Start policy determines
whether each application launches. There is no hard live/readiness prerequisite
or recursive explicit launch. Stronger dependency semantics remain a future
architecture decision, rather than an implicit behavior change in this fix.

## Pass 7 systemd lifecycle

A persistent D-Bus connection subscribes before queuing jobs, correlates completion
by returned job path and unit, checks the manager sender, and rejects non-done
results. Early completion is buffered. Local timeouts and lost replies remain
uncertain outcomes and are never replayed automatically by root-control.
AddRef retains transient units on that connection until evidence has been saved
and cleanup explicitly releases them. An event-loop thread drains bus traffic
while the serial daemon waits for clients.

Start/stop jobs, busctl commands, aggregate unit observations, framed socket
messages, and controller lifecycle operations have explicit deadlines. Nested
rollback shares the outer operation budget. The daemon survives a client that
has disconnected before its reply is sent.

Schema 4 adds cleanup_pending and cleanup_error; schemas 2 and 3 still load.
Terminal records with pending cleanup are revisited, protected from pruning and
generation reclamation, and block replacement until cleanup is resolved. Program
exit evidence precedes reset/unref, and services must unload before their slice.
Missing units are idempotent cleanup outcomes; errors remain visible and retryable.
Writable mount data and external journal/core-dump evidence are not reclaimed.

## Validation boundary

103 local tests pass. Five opt-in real-systemd tests are skipped until a VM is
available. One real private-D-Bus protocol test is skipped here because the
container prohibits socket creation. Fake-bus tests cover early/late completion,
unrelated signals, failure results, rejection, disconnect, lost replies, and
deadlines. No real D-Bus or systemd acceptance is claimed.

See HANDOFF.md (retained historical verification; not distributed with the public manual) and catalogue/systemd-vm-testing.md for the exact test command,
prerequisites, remaining manual crash scenarios, and the next review boundary.

## Review revision 1

Failed-launch rollback retains prior failure results across unavailable, absent,
and reset observations. A three-case persistence regression fails on pass 7 and
passes here. The VM exec-failure test now proves the peer acquired an invocation
identity and the deliberately broken member supplied exit-code failure evidence.
