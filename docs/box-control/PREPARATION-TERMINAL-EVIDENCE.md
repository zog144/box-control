# Durable preparation terminal completion — fast-exit recovery

Date: 2026-10-13. Producer: box-control. Origin: the 2026-10-12
host-deploy application identity NSS rerun.
Scope: persistent single-instance application preparation. This source
change does not authorize host changes, replay, or production cutover.

## Problem

A short-lived preparation process can execute and exit successfully
before the next `ApplicationStorage.refresh()`. The runtime launch
pipeline saves an authoritative terminal application-runtime reference
with its invocation ID, result and completed time, then immediately
releases the transient unit and runtime scaffold. Refresh previously
required the systemd unit still to exist, marking preparation
`uncertain` even though successful completion had already been
durably saved by Zog before cleanup.

The isolated host-deploy canary demonstrated this after fixing
systemd's host NSS setup: `/usr/bin/true` completed successfully,
but preparation stayed `uncertain`. This is not a reason to
restart or repeat the migration.

## Source contract

`ApplicationStorage.refresh` continues to inspect any **existing**
service through `runtime._observe_program`. If its unit is absent,
it may use only the persisted, controller-owned terminal runtime
reference for the exact outstanding preparation step. Required proof:

- Reference `runtime_id` matches the step; `purpose='preparation'`;
  application, storage-derived preparation instance and generation
  match the immutable preparation snapshot.
- Exactly one program, with the expected program identity and
  nonempty systemd unit name and invocation ID. If the step already
  has an invocation or unit name, each must match.
- Runtime state `terminated`, non-null completion time,
  `cleanup_pending=false`, no runtime error.
- Program `result='success'`, `active_state='inactive'`,
  `sub_state='dead'`. Merely saving a successful initial
  StartTransientUnit reply or observing an absent service does
  **not** satisfy this proof.
- A nonempty saved boot ID. New preparation steps persist their
  boot ID alongside their unit name and attempt ID **before launch**;
  the runtime proof must agree with these bindings. Older steps
  without this separate boot binding may use terminal evidence
  only if the host is still on the reference's original boot.
- The current attempt ID must match if the step carries one.

If any proof is absent, altered, or inconsistent, readiness remains
blocked as `uncertain`; no migration command is repeated. When
proven, the step's outcome, invocation ID and source
`durable-terminal-runtime` are synchronously persisted into the
preparation record before acknowledging readiness. The controller
does not re-create or terminate a runtime already known to have
completed cleanup. Persistent tree fsync and existing storage
identity validation still precede `state='ready'`.

## Recovery of a retained historical preparation attempt

The October 12 canary completed a short-lived preparation command and retained
an authoritative terminal reference after its transient unit had been unloaded.
The originally retained attempt predated separate step-level boot-ID binding,
so accepting that saved terminal evidence remained conditioned on observing
the **same boot** and matching all immutable preparation, application, runtime,
generation, invocation, and owner identities. A changed boot or missing
identity evidence must remain `uncertain`—never force readiness, replay the
program, or edit the saved record by hand.

The exact host-local project path, runtime identifier, request/attempt ID and
invocation ID are private historical operating evidence, not reusable
installation parameters. To reproduce this recovery, an authorized owner must
use the real retained record and matching box-control/root-control revisions in
an isolated controller, read the reference **before** mutation, and invoke
`refresh_application_preparation` only for the original application. Verify
the result was durably persisted and that no process or unit restarted. This
procedure does not authorize station production traffic or database migration.

## Regression and outstanding acceptance

Regular tests include a fake systemd transport that completes a
preparation before runtime launch returns, then immediately cleans
the transient unit, and verify that refresh and a controller reopen
consume the saved result without replay. Additional regressions
reject incomplete, forged, boot-mismatched, unit-mismatched,
invocation-mismatched, wrong-attempt, wrong-program, wrong-generation
and wrong-instance terminal references. A bound terminal reference
can remain sufficient after a controller reboot; old unbound steps
fail closed across boots.

A real short-lived preparation on the existing host is still
required to accept kernel/systemd behavior. The separately
published image-build generation supplies production data targets,
but full station-access/archive-mirror application composition,
owner-aware compiler access, archive storage migration and reboot
readiness remain production cutover gates.

**Production cutover remains NO-GO.**

## Opt-in native systemd regression

`box-control/tests/test_systemd_integration.py` includes
`test_live_fast_exit_preparation_completion_survives_unit_unload`.
It requires the existing `--run-systemd-integration` option, a
dedicated disposable `--systemd-rootfs`, and two environment values
identifying an **already provisioned, non-root host test principal**:
`ZOG_VM_PREPARATION_USER` and `ZOG_VM_PREPARATION_GROUP`.
The test provisions no account or grant; it uses an isolated
temporary Zog project, runs `/bin/true` as that principal, waits
until the runtime reference shows fully completed systemd cleanup,
then verifies preparation uses exactly the durable invocation
without executing the command again. Never point this test at
production state, the active production controller or a shared
project; run it only through an explicitly authorized host owner.
Normal CI skips this privileged native test.
