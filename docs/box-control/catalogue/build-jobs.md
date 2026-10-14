# Finite build jobs — first implementation

Build jobs use systemd through root-control. They do not use application
generations, restart policies, Docker, Podman, or a native execution fallback.
Compilation uses a configured nonzero UID/GID; the UID must differ from the
project owner. The existing application-generation validation is unchanged.

## Register once per package attempt

`register_build_root(resource_id=..., prepared_root=..., source_directory=...,
output_directory=..., input_manifest_id=..., execution_user_id=...,
execution_group_id=...)` registers a finalized root and two exclusive workspaces.
The caller saves a random 32-hex resource identity before first registration and
reuses it after lost replies. Different intent under that identity is rejected.

All supplied paths must be disjoint directories beneath the project's
`state/image-build/attempts`. The input manifest identity is an explicit caller
assertion about verified inputs, not a new verification format. Stop modifying
the prepared root before registration. The controller imports a separate copy
under `/var/lib/zog/build-resources/<project-path-hash>/<resource-id>/root`.
Application-generation manifests are never fabricated. API filesystem directories
(`/proc`, `/sys`, `/dev`, `/run`, `/tmp`) are created privately, not imported from
the seed. Registered roots are root-owned and nonwritable by the build user.

Source/output remain at their original caller paths and are mapped writable at
`/image-build/source` and `/image-build/output`. Ownership is assigned to the
explicit build identity. Configure a group and file-access policy appropriate
for image-build's caller; this pass does not provide arbitrary ACL management.
Configure/build/test/install share these directories and one registration, with
only one unfinished command writer permitted at a time.

## Submit and observe

Obtain `issue_build_request_id()` and persist it with the command attempt before
`submit_build_job(request_id=..., **request)`. Required fields follow the supplied
IMAGE-BUILD-EXECUTION-TYPES-AND-MAPPING.md vocabulary. Resource limits currently
require positive `thread-count-maximum` and `memory-maximum-bytes` values; the
caller supplies all three deadlines and the execution UID/GID explicitly.

The root identity plus `-source` and `-output` suffixes identify its workspaces.
The command and environment are copied into the durable intent. Bare executables
resolve using explicit PATH in the guest root; relative executables use the
in-root working directory. Absolute symlink targets resolve against that guest
root, never the host root. The resolved executable is recorded independently. ExecStartEx disables systemd
environment expansion so argv is delivered literally, including dollar signs.

`inspect_build_job(job_id)` reads persisted state. `refresh_build_job(job_id)`
observes systemd, saves exit evidence and progresses cleanup. `cancel_build_job`
records cancellation before requesting stop. Existing mutation entry points
recover interrupted build preparation/observation when evidence permits; unknown
outcomes still block mutations until explicitly addressed. No attempted command
is automatically restarted. Use `recovery_status()` for partial build summaries.

systemd enforces startup timeout, RuntimeMaxUSec, stop grace with final SIGKILL,
cgroup lifetime, memory/task limits, private networking/devices, strict filesystem
protection and NoNewPrivileges with no capabilities. Unsupported required
properties reject start. Finite compilation is distinct from application launch:
a start acknowledgment is not a successful build result.

Records distinguish progress state from outcome, numeric exit/signal evidence,
service result, Invocation and journal identities, process cleanup, and resource
release. A zero exit alone does not override timeout/cancellation/fault evidence.
Missing observations preserve uncertainty. Evidence is saved before reset/unload.
Journal references do not promise retention of journal entries.

## Cleanup and retention

`process_cleanup_complete` means processes and required units/mount namespaces
are gone. Outputs can remain retained. `release_build_job(job_id)` relinquishes
that job's resource pin only after cleanup. Once all jobs release their pins,
`release_build_root(resource_id)` deletes the imported root and releases the
registration. It does not delete the caller's original source/output trees;
image-build can then dispose of its staging directories according to its policy.

Build request IDs use a separate signing domain and the accepted 48-hour expiry
rules. Completed/unreleased jobs and roots remain explicitly retained. After a
root is released and the applicable 48-hour windows have elapsed, mutating calls
prune controller and privileged metadata using `build-pruning.json`. Interrupted
pruning resumes before accepting more work. A retired request cannot execute again.
Unused registered roots require explicit release; there is no silent root expiry.

## image-build adapter

`image_build.box_control_adapter.BoxControlExecutionAdapter` is supplied with a
controller, explicit identity/limits/deadlines and an input_manifest_id value or
resolver. Pass it as `BoxControlRunner(execute=adapter)`. The runner supplies a
unique per-command log/checkpoint path. Resource and command bindings are saved
durably before controller submission. The original terminal result fields are
preserved. Other outcomes raise `BuildExecutionFailed` retaining the full record;
wait expiry/uncertainty raises `BuildExecutionPending` with resumable identities.

Replay `execute_for_attempt(request, checkpoint)` with the same binding to resume
that command. A caller wait timeout does not cancel it. The adapter releases its
command lock on return and releases the project lock between observations.

The image-build engine now assigns distinct command checkpoint names and retains
pending status for controller storage/recovery errors. It refuses to silently
create another pipeline attempt while an earlier one is running/pending. Full
automatic continuation of the entire image/bootstrap pipeline is not added in
this pass; the command adapter is resumable, and an orchestrating caller must
continue the existing attempt explicitly. The CLI still requires deliberate
controller configuration; no guessed UID, limits or manifest identity is wired in.

## Acceptance

`acceptance/build_host.sh`, `build_live.py`, and `build_recipe.json` provide the
Amazon Linux host-deploy fixture. The fixture installs a host seed using the
installed Amazon Linux release version, exercises isolation and result cases,
and drives the supplied library/executable recipes through the controller. It
uses internal image-build composition helpers for a host-seeded integration test;
it does not fabricate a promoted second-stage toolchain or claim self-hosting.

Systemd controls reference: https://github.com/systemd/systemd/blob/main/man/systemd.service.xml
See HANDOFF.md (retained historical verification; not distributed with the public manual) and validation-summary.json (retained historical verification; not distributed with the public manual) for the actual tested revision/results.
