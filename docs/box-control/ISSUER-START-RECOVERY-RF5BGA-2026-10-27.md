# Issuer preparation RF5BGA: recovered invocation, rollback, and supported recovery

Date: 2026-10-27  
Ownership: box-control / root-control Regular; host-install Work owns retained native execution.  
Scope: **one original** `certify-manage/prepare`; no ACME issue/renew, Cloudflare transfer, DNS, nginx or source installation is authorized here.

## Native evidence and conclusion

Read-only native evidence: [host-install Work retained timeout inspection](https://github.com/zog144/know-file/blob/aec29a4f3ba715fd6f417a304a1d02787d778a7a/host-install/verification/issuer-timeout-readonly-2026-10-27/README.md).
These facts come from host-install Work's preserved observations, **not** from a new host action by Regular.

- Original Project `/state/issuer-controller`, attempt `c1cd483c3d9a8efdbaa12ed6872bb39f`, runtime `RF5BGA`, unit `zog-issuer-controller-RF5BGA-prepare.service`, same boot `58b1a043-76bb-4dc6-9e5c-98701758af40`.
- Saved runtime program InvocationID `79ab358ffadf491e84f87e1bba645d4a` matches retained PID1 journal start job `170452`. PID1 records Started at 2026-10-27 17:51:58.706536Z, SIGTERM requested by client at 17:51:58.717120Z, deactivated successfully 17:51:58.718049Z, and slice removed 17:51:59.347617Z. Root-control logs an untagged undelivered response at 17:51:58.707588Z, temporally associated but not conclusively correlated to the exact RPC.
- Persistent preparation remains `uncertain`, `cleanup_pending=true`, step outcome and invocation null; saved runtime is `failed` with reply timeout 45.011 seconds and `cleanup_pending=false`, program invocation non-null and `result=success`. The result reflects **systemd deactivation**, not authenticated completion of `certify_manage prepare`; runtime-level error makes that distinction authoritative.
- PID1 unit currently absent and cgroup/PID empty, issuer certificate-data remains original rootfs-backed 0700 grant-owned storage, zero entries observed (not proof no partial execution). Frozen issuer generation, software view, issuer grant UID/GID and storage ID unchanged. Original records and bounded raw journal were preserved root-only; do not print or publish secrets.

**Disposition: ORIGINAL START CONFIRMED; PREPARATION COMPLETION UNPROVEN; TRANSACTION FAILED AFTER TIMEOUT/ROLLBACK; NO REPLAY.** The InvocationID is valuable provenance but cannot change the step to `success`. The existing `ApplicationStorage._durable_preparation_completion` intentionally rejects the saved runtime because its state is FAILED with a non-null error, not TERMINATED with clean terminal success. It also rejects absence of the unit alone. Do not edit the step or runtime records to bypass that check.

## Source-level timing/rollback defect and narrow correction

The native sequence is consistent with root-control spending most of the 45-second client deadline in the service preparation phase (validation/scaffold/mounts/persistent-dir checks) and then successfully starting PID1. **This phase attribution is a hypothesis**, not timestamp-proven: the previous root-control daemon did not retain per-phase timing. PID1 start and client rollback occurred roughly 11 ms apart, while `RuntimeMaxUSec=120000000`; runtime max is not the observed cause.

Previous direct `SystemdServiceRuntime.launch`:
1. save original STARTING runtime and pending preparation step;
2. issue `root-control systemd_start_service`; response may be lost at the client's 45s deadline;
3. generic `except Exception` sends SIGTERM, then stops/releases/unloads the same unit while retaining a FAILED runtime; captured `Result=success` from deactivation is **not** an application success.

**Fixed in source for future attempts only:** box-control now catches typed `RootControlReplyTimeout` at `start_service`, raises `ServiceStartOutcomeUnknown`, preserves the original STARTING runtime/boot/definition and preparation's `uncertain` state, and bypasses generic kill/stop/release. It neither retries nor declares success. A later *exact frozen* PID1 observation may save the original InvocationID if the same process terminates normally; absence/mismatch/reboot remains uncertain. Other genuine launch failures and storage-barrier failures retain their existing fail-closed behavior. Do not increase the client timeout blindly or replace installed source on the retained host based on source tests.

root-control now emits protected diagnostic events `systemd-start-service-dispatched`, `-validated`, `-job-submitted` (elapsed preparation time), `-job-completed` / `-job-unconfirmed`, `-handler-finished`, and operation-specific `root-control-response-undelivered`. Only validated unit IDs, timing, and booleans are logged. These events are diagnostic, **not** durable terminal program evidence and not a changed controller RPC protocol.

## Supported procedure for this retained incident

**Phase A — read-only confirmation (already performed by Work; refresh immediately before mutation):**

1. Reverify exact retained installation/STATE/boot, installed source/RECORD and frozen generation/view/policy/grant/storage. Compare current preparation and runtime reference file bytes to root-only preserved snapshot; inspect runtime and operation/request journals/markers.
2. Read-only PID1 `GetUnit`, cgroup/process inventory and unit-bound journal, all tied to saved RF5BGA and InvocationID. Check that original unit is absent and no descendants have the issuer UID/cgroup. Preserve journal and program evidence *before* any controller method that might reset/unload.
3. Confirm no original preparation completion marker, no authenticated stdout, and no clean TERMINATED runtime reference. Keep status `uncertain`. **Do not invoke `refresh` to manufacture successful completion.** Existing refresh may be used later only through the normal supported reconciler, never an ad-hoc record edit.

**Phase B — optional explicit disposition after separate operator approval:**

4. If the independently reviewed original UID/boot/STATE and no-live-process conditions remain exact, an authorized owner may invoke the existing controller API `BoxControl.abandon_application_preparation("certify-manage")` **once on the retained existing Project**, under its normal mutation lock. This is abandonment of the *original* attempt, not an attempt to adopt the recovered invocation as successful. It retains storage/grant, records `outcome="abandoned-unknown"` and `state="failed"`, and leaves the attempt for history. Abort if any unit is active, installed identity has drifted, state has changed, or lock/mutation admission fails. Snapshot before/after and verify no unexpected issuer data changes.
5. After abandonment, **do not automatically retry**. `prepare_application(..., upgrade=True)` without `retry=True` must remain refused. Independently review whether exact installed `zog.certify_manage.manager.CertificateManager.prepare` only checks/creates protected directories and is safe to repeat on current storage; no assumption from public source alone substitutes for installed view attestation. A separate explicit authorization is required before ever using `prepare_application("certify-manage", upgrade=True, retry=True)` on the *same* grant/storage and frozen policy/view. Preserve failed attempt history and assign a new attempt/runtime only through supported controller API. No reallocation of identity and no ACME side effects.
6. Even after a future prepared=ready success, host-install must independently attest exactly one **new** successful preparation step with its **new** saved boot/InvocationID/terminal result and cleaned cgroup, then selected inspect and reboot acceptance. The historical RF5BGA record must never become the new success.

**Without Phase B approval, current safe terminal action is no mutation; leave original preparation `uncertain` and block further issuer operations.** This document is not approval to abandon, rerun or install a changed controller.

## Verification and boundaries

Source regressions cover: root-control job-phase telemetry/error uncertainty; bounded secret-free RPC diagnostics; box-control timeout before/after PID1 acceptance with one original attempt, no automatic kill/release, later exact completion, missing unit stays uncertain; retained-native analogue where saved InvocationID plus `Result=success` and failed timeout cannot promote the original step; supported explicit abandonment transitions to failed without replay, and default retry remains denied.

Native retest of the *new source* is NOT RUN. Original host-install #2 native preparation and selected inspect/reboot remain blocked. Historical retained records must never be rewritten by source-only CI.
