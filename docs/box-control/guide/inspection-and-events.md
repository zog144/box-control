# Inspection and lifecycle events

## Persisted expected state versus observed processes

A read-only inspection must identify its evidence basis. The persisted runtime tells what command line and generation box-control **expected to launch**. An actual systemd observation reports current unit state and invocation identity at an observation instant. Neither is an application health monitor or proof of kernel mounts unless that specific source was queried.

`observe_application_runtime(runtime_id)` provides fresh read-only service observation without triggering reconciliation, cleanup or new requests. `application_runtime_mounts(runtime_id)` reports the recorded expected mount inventory with an explicit `observed_mounts: not-probed`. Distinguish it from the separately authorized kernel mount observation surface.

## Process inspection

`application_processes(runtime_id, program, limit=50)` and `build_job_processes(job_id, limit=50)` are bounded cgroup inspections based on an **existing stored identity**. They do not accept raw user-supplied PIDs, unit names, cgroup selectors or host paths. Observed PID and argv belong to the host process and can change or be truncated; parent PID alone is not a stable identity. Use boot ID, PID and process start ticks to correlate observations. The reader rechecks invocation/cgroup identity, but a live process list is inherently non-atomic.

Only `observed` or `partial` may include process rows. Empty rows under `unavailable`, `previous-boot`, `identity-unavailable` or `changed-during-read` are **not** proof of a stopped service. Inspection does not persist a process history or read process environments. See [structured process inspection](../catalogue/process-inspection.md).

## Journald logs and finite build visibility

Runtime logs and build logs are queried using recorded boot/invocation identity and opaque cursors. A cursor unavailable through journald retention, no recorded identity, an empty successful result and a failed reader are distinct states. Build-job summaries are persisted controller facts; asking for them does not necessarily refresh systemd. Station-access applies authorization before invoking the trusted local API, and it must not accept user-crafted journal match expressions.

Read [build visibility](../catalogue/build-visibility.md) and [station-access integration](../catalogue/station-access.md) for response shapes and reader limits. A record can outlive available journal logs; retention of one is not retention of the other.

## Structured events and recovery diagnostics

The lifecycle logger emits correlated `zog_event` fields for operation writes, recovery entry/exit, service-start intent, acknowledgements and cleanup. A returned method is **not necessarily a committed launch**; event delivery is best effort and can repeat across retries. The serialized durable operation files and request results remain the source of truth. See [lifecycle event families](../catalogue/lifecycle-events.md).

`recovery_status()` provides a read-only bounded snapshot of recorded faults and an interlock classification without recovering, mutating or connecting to systemd. If the project lock is busy or storage is unreadable, it expresses unknown or partial evidence rather than claiming readiness. A result of `no-recorded-block` is not privilege to mutate. See [recovery inspection](../catalogue/recovery-inspection.md).

## Ownership of host directory capabilities

Application specifications cannot select arbitrary host source paths. A privileged root-owned policy grants a narrow **directory**, for a fixed project/application/program, and root-control revalidates actual host identity and inode/device against the prepared binding. A mutable lease file must be opened anew through its stable parent directory; broad credentials, host sockets and unreviewed writable binds are not implied by the read-only host capability. See [host-service directory capability](../catalogue/host-capabilities.md).

This chapter joins observational concepts only. It does not weaken the privileged controller policy or publish host-private paths to browser clients.
