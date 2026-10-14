# Structured process inspection

`control.application_processes(runtime_id, program, limit=50)` and
`control.build_job_processes(job_id, limit=50)` return read-only snapshots.
Station-access should authorize the target before calling these trusted local APIs.
No caller-supplied PID, unit name, proc path or cgroup selector is accepted.

The response separates `expected_command` (saved launch argv) from `processes`
(current cgroup members, including nested cgroups). Each process contains host PID,
parent PID, kernel state letter, real user ID, start time in clock ticks since boot,
observed argv and a command-truncated flag. Pair boot ID, PID and start time when
matching rows between polls. Parent PID alone is not a stable process identity.
A child running a different command is normal; this API does not classify health
or infer tampering from descendant command lines. Observed argv is process-controlled.

Saved boot and invocation IDs bind the query. The service must remain transient
without drop-ins. Invocation and cgroup are rechecked after traversal; rows are
discarded if the unit changes. This is identity verification, not the full launch
property comparison provided by `observe_application_runtime()`.

Snapshots are explicitly non-atomic. Processes may fork, exec, move or exit during
inspection. Proc-directory descriptors, start-time checks and repeated membership
checks exclude detected races; they cannot freeze execution. `skipped_processes`
counts omitted reads/membership checks and vanished cgroup directories, not an exact
count of exited processes. Empty command lines are valid. Environments are not read.

Statuses: observed, partial, absent, no-cgroup, previous-boot, identity-unavailable,
changed-during-read, integrity-fault, unavailable. Unknown/pruned records and invalid
arguments raise errors. Only observed/partial carry rows; never render unavailable
as proof that an application has stopped. After cleanup, expected argv can remain
available while live inspection reports absent. No process history is persisted.

Bounds: 1–100 rows (default 50), five-second inspection deadline, 4096-byte argv per
process, 256 KiB serialized process rows, 256 cgroup directories, 4096 unique PID
candidates. Truncation is explicit. There is no pagination cursor for rapidly changing
process membership; poll again for another snapshot. Inspection and logs share two
bounded read workers independent of lifecycle operations. Overload may return the
existing retryable lane-busy transport error. Each inspection uses a private read-only
systemd backend, never the lifecycle job connection.

This requires Linux cgroup v2 and procfs. Station-access owns polling, authorization,
text escaping, and display policy for potentially sensitive command arguments. This
pass does not add CPU/memory telemetry, process signals, GUI components or a scheduler.
