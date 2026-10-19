# GitHub-triggered image-build controller — compatibility and recovery review

**Date:** 2026-10-19
**Target:** `box-control/unstable`  
**Reviewed executable source:** `box-control@1e8ff8ace69ef581907fd22ea8b796cc7e72ee80`  
**Privileged companion tested:** `root-control@e6ff84448ec7103af141905749f504b7e5704fc1`  
**Image-build producer inspected:** `image-build/unstable@0fd3f00cafc7138d3bac9695c87a8ece65821868`  
**Host-deploy gateway inspected:** `host-deploy/unstable@1b542081667f0e1d14b973cec26de777379590d9`  
**Source CI:** [controller-security-regression 39255519697](https://github.com/zog144/box-control/actions/runs/39255519697), **560 passed**, no skips or deselections. Corresponding [root-control run 39255526688](https://github.com/zog144/root-control/actions/runs/39255526688) **261 passed**.  
**Disposition:** source compatibility PASS with bounded hardening; real gateway dispatch/systemd recovery NOT RUN here.

## Compatibility with `image-build-remote-operation-v1`

Image-build maintains its own exact repository+commit operation binding, source-pin and recipe snapshot, per-pipeline/attempt checkpoints, producer-owned request semantics and remote inspect/resume/cancel/retry policy. The CLI's controller profile requires the existing local `BoxControl`, `Project`, `BuildJobs` and `RootControlSystemdTransport` interfaces; these are retained by this revision. The controller still does not interpret generic GitHub task names or maintained-worker targets.

The normal pipeline path is:

```text
GitHub Actions issue/workflow request
 -> GitHub OIDC / SQS gateway / host-deploy authorization and durable ledger
 -> exact deployed image-build remote_operations pipeline.submit/resume/inspect/cancel
 -> image-build retained pipeline + recipe snapshot + command checkpoint
 -> box-control issue_build_request_id / register_build_root / submit_build_job
 -> root-control typed build_prepare / build_start (single saved job identity)
 -> systemd transient finite build service (separate nonzero build UID/GID)
 -> box-control inspect/refresh/cancel/release and retained journal reference
 -> image-build-remote-execution-evidence-v1 bounded producer result
```

**Trust:** GitHub is a remote requester, never the local privileged issuer or build UID. Host-deploy chooses a reviewed lane and calls the deployed image-build source; it must never relay raw GitHub issue/workflow shell, argv, environment, UID, mount paths or direct root-control RPCs. The root-control daemon only accepts the exact local controller owner UID, with independently verified OS peer credentials.

**Build admission:** the requested command, explicit environment, registered root/workspaces, numeric UID/GID, deadlines and cgroup limits are frozen in the controller job *before* start. Root-control revalidates immutable request and imported root at the privileged boundary. The per-project lock serializes controller mutations and the privileged root daemon serializes management calls; two **different** registered roots may then run as simultaneous cgroups. A single resource admits only one unfinished writer. Root-control now also rejects the project owner's GID at registration and on later preparation (not just its UID).

## Durable request and unknown outcomes

- Image-build writes resource identity and per-command checkpoint with controller-issued signed request ID **before** submission. Box-control hashes the signed ID to a stable 32-hex job ID and records exact command/resource intent before first privilege. Reusing an ID with changed inputs is rejected.
- A lost submit reply, gateway restart, GitHub runner exit or client poll timeout does not cancel the systemd job or release resources. The producer inspects the same job ID, calls `refresh_build_job` and retains pending state if evidence is ambiguous. Root-control persists `start_attempted` before `StartTransientUnit`, so it cannot accidentally start the same privileged request twice.
- If systemd unit unloads, the configured exit-hook/journal barrier can provide exact terminal PID1 evidence; if neither trusted unit nor barrier can prove the original outcome, state becomes `unknown` and retains cleanup resources. No automatic retry with a new command.
- Cancellation is durable: record `cancel_requested` before requesting stop. Retain original exit/signal/timeout/barrier evidence, then explicitly drain/reset/release the corresponding unit/cgroup. Terminal success is not inferred from an acknowledged start or service exit 0 alone.
- Completed jobs and imported roots remain until the image-build owner explicitly releases their pins. The accepted 48-hour bounded request-retention/pruning rules are preserved, and incomplete cleanup blocks reclamation. Journald has independent retention.

## New read-only machine projection

`BoxControl.build_job_status(job_id)` is a read-only, bounded `zog-controller-build-job-status-v1` projection. It supplies:

```text
schema, observation_kind=persisted-controller-record
job_id, request_id, build_root_id, runtime_id
state, outcome, process_cleanup_complete, resources_released
exit_code, signal, service_result, completed_at, uncertain
evidence_reference: null | {
  kind: systemd-journal-invocation,
  boot_id: <canonical UUID hex>,
  invocation_id: <canonical UUID hex>
}
```

The original command, environment, build host paths, private root, mutable source/output trees, raw log lines, credentials and arbitrary errors **are not projected**. The evidence reference is a locator only, **not a promise** that journald still retains the entry. A persisted `running` state is **not** a current systemd observation: use `refresh_build_job` (mutation/reconciliation) and the existing authenticated bounded `build_job_logs`/process readers when fresh information is required. Contradictory terminal evidence, malformed identity/exit data, or success without a saved valid boot/InvocationID is rejected rather than reported as verified.

The producer's existing `inspect_build_job` response remains unchanged for its **trusted local binding checks**; remote gateway/public summaries should prefer the sanitized projection and the image-build-owned bounded evidence contract. This controller method does not create a new network listener, controller mutation capability, or direct GitHub tool.

## Additional source regressions

- Registrations with root/zero/boolean/oversized build UID/GID or project-owner UID/GID are rejected; privileged prepare also detects project owner/group changes after registration.
- A pending long-running job on one registered root does not prevent a distinct reviewed root's job from reaching its own running state; the same root cannot admit a second writer.
- A lost first-submit reply retains request/job identity and remains `uncertain`; read-only status does not clear the mutation guard or submit a replacement.
- Success/cleanup status is secret-free, retains normalized boot/InvocationID evidence and does not expose argv/environment/host paths. Contradictory completed-success or incomplete cleanup is rejected.
- Existing tests cover unchanged-resource idempotency, wrong-bound request, caller wait timeout and retained image-build checkpoints, cancellation, signal/timeout outcomes, interrupted cleanup, native-unload evidence, reboot ambiguity, journal cursor isolation and 48-hour reclamation.

## Native Work acceptance — required before GO

1. Capture approved source pins, producer preflight/smoke, image-build recipe snapshot/monthly provenance pin, host-deploy gateway request/ledger and reviewed controller UID/GID and build account identity from host-install, not issue inputs. Observe actual root-control socket DAC and SO_PEERCRED denial for other UIDs.
2. Submit a real finite compiler command through GitHub Actions → OIDC/SQS → dispatcher/SSM → image-build remote pipeline → controller/root-control; record SQS/GitHub request, producer operation, pipeline/attempt, controller request/job/resource, unit/cgroup, boot and InvocationID. Verify exact single systemd launch and no fallback execution path.
3. Run longer than gateway/SSM response and terminate the caller; reconnect via the original remote `pipeline.inspect` and, where required, `pipeline.resume`, without creating a duplicate controller job. Repeat with a lost root-control start/observe reply and reboot while cleanup is pending.
4. Demonstrate two independent approved build roots run concurrently after serialized privileged admission, while one root's second writer is rejected. Confirm cgroup memory/tasks/runtime limits, immutable imported root, approved writable source/output binds, no network/device/host executable access, no root capabilities and correct User/Group.
5. Exercise successful exit, nonzero, signal, timeout, request cancellation, interrupted cleanup and eventual explicit release. Verify status/evidence references across reconnects and no accepted read-only status can trigger state mutation. Prove `unknown` blocks a new command rather than silently retrying.
6. Inspect root registration/management ownership and live project/state/mount ACLs, including supplemental group overlap and UID/GID drift. Verify build principal cannot modify root-owned imported image, protected controller state or root-control socket.
7. Check image-build result `image-build-remote-execution-evidence-v1` retains exact source and recipe identities, controller job IDs, journal locators and bounded missing-evidence/uncertainty flags. No fabricated successful generation or publication from a merely completed compiler job.
8. Retain evidence for 48-hour idempotency, interrupted cleanup and request expiration on a **disposable controlled clock** (not production wall-clock manipulation); original terminal IDs cannot allocate a new execution after pruning.

**Native result:** NOT RUN in this implementation chat. Source CI passing does not authorize production GitHub-triggered compilation, public source promotion, increased AWS privileges, nginx/certificate changes or bypassing image-build producer preflight.
