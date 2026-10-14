# Interfaces and integration

## The controller boundary

Box-control is a **trusted, synchronous local Python library**, not a network API or a general privileged command interface. It resolves application composition, accepts exact lifecycle requests and reconciles durable controller state against systemd. Root-control owns the privileged mechanics and validates requested operations independently. Image-build owns the published read-only generations and application software; box-control consumes their exact identities and never compiles a missing rootfs during a launch.

Station-access owns HTTP authentication, CSRF, user/workspace authorization, browser polling and the presentation of controller diagnostics. It does not use a client-supplied systemd unit name, PID, host path or arbitrary command string to bypass the library. Requests may be long-running; a disconnected browser does not cancel their durable intent.

## Lifecycle request mapping

The [station-access integration contract](../catalogue/station-access.md) describes `issue_application_request_id()` and `execute_application_operation(operation, target, request_id=...)` for launch by application name and restart/termination by immutable runtime ID. The caller saves the issued ID **before** the mutation and reuses it after an acceptance/transport loss. Reusing one request ID for different parameters is an integrity violation.

A result can be completed, pending, busy, blocked or failed. **Pending** requires recovering/observing the recorded operation, not a new request ID. **Blocked** can represent a storage or recovery interlock; **failed** does not prove there were no side effects. The default mutation-lock acquisition budget is separate from total operation duration; a web worker must avoid blocking an async event loop while invoking a synchronous controller.

The controller's 48-hour bounded request retention applies to completed outcomes and unused identities under their documented rules, while accepted unresolved work and cleanup protection survive the ordinary timeout. Read [request retention](../catalogue/request-retention.md) and [recovery policy](recovery-integrity-and-retention.md) before designing retries.

## Read-only launch, runtime and diagnostic APIs

`preflight_application_launch(application_name)` is read-only, advisory and does not reserve an image, service or replacement target. A `ready-to-attempt` response is **not authorization or a guarantee of launch**; the actual mutation rechecks resolved inputs and expected runtime state. Unknown preview or busy lock must report `unable-to-verify`.

`observe_application_runtime(runtime_id)` separates persisted expected configuration from current systemd observations. `application_runtime_mounts(runtime_id)` provides the saved launch mount inventory, explicitly not a live kernel mount probe. `application_processes(runtime_id, program)` and `build_job_processes(job_id)` inspect bounded existing identities rather than arbitrary host processes. Recovery inspection/explanation reads recorded interlocks without applying repairs or clearing markers.

A caller must preserve each source's evidence basis and timestamp and distinguish `unknown`, `unavailable`, `previous-boot`, `absent`, `partial` and `integrity-fault` where applicable. Read [preflight](../catalogue/launch-preflight.md), [structured process inspection](../catalogue/process-inspection.md), [recovery explanations](../catalogue/recovery-explanations.md), and [recovery inspection](../catalogue/recovery-inspection.md).

## Controller-owned workspace integration

The [workspace API contract](../WORKSPACE-APPLICATIONS.md) uses a stable UUID, non-reused number and explicit desktop/client application roles. Desktop DISPLAY, Xauthority, socket and mount identities are controller-owned; the browser/client must not choose them. The documented v1 shared-host network profile **is not network isolation**. A desktop launches under its own durable runtime identity and is distinct from its client applications; a browser viewer's lifetime is not the desktop's lifetime.

Station-access uses the controller's admission and exact runtime history to reconcile an interrupted desktop launch and to authorize observation/log queries. Additional public read-only Workspace Agent API/MCP access belongs to station-access, not this trusted Python boundary.

## Finite build-job integration

The [image-build mapping](../IMAGE-BUILD-EXECUTION-TYPES-AND-MAPPING.md) records an early implementation interface: the small execution request/result type is **not** a complete current controller job record. Current [finite build jobs](../catalogue/build-jobs.md) register an immutable prepared root and separate source/output writable locations, run unprivileged finite commands through systemd, and retain request/job IDs and cleanup evidence.

A reply timeout while root-control is importing a build root has an uncertain outcome. `inspect_build_root(resource_id)` can read the exact registration without mutation or clearing the guard; an `importing`/absent result cannot authorize re-registering with a different resource. The original caller must reconcile the same identity once `ready` is authoritative. See [registration progress](../REGISTRATION-RECOVERY.md). Image-build owns the attempt, phase and output graph; box-control owns each finite service, invocation and cleanup result.

## Privileged host-service capabilities

Application specifications declare only permitted named application data mounts; they **cannot** nominate host filesystem sources. The privileged administrator may configure a narrowly scoped, root-owned host directory capability policy and root-control revalidates project/application/program, source identity, read-only access and immutable prepared binding. This supports selected host-discover export directories, not arbitrary host sockets, secrets or writable host-store mounts. See [host capabilities](../catalogue/host-capabilities.md).

## Read-only publication to user interfaces

Invocation-scoped log and build visibility adapters expose bounded controller facts to station-access after its own authorization. Lifecycle events are diagnostic, not durable source-of-truth replacements; neither polling nor logging constitutes mutation permission. When presenting errors, redact raw credentials and present stored fault evidence as text. See [logs/visibility](../catalogue/build-visibility.md) and [lifecycle events](../catalogue/lifecycle-events.md).

### Integration order

1. Install the exact compatible image-build and root-control contracts for a deliberately selected controller project.
2. Verify immutable published software/generation availability and exact application declarations.
3. Authenticate/authorize at the consumer boundary; create and persist request IDs before mutation.
4. Use controller read-only preflight/inspection as advisory evidence, then submit the exact authorized lifecycle action.
5. Resolve lost responses with the **same** identities; preserve unknown outcomes and pending cleanup.
6. Validate actual systemd mounts, invocation identities, workspace sockets and reboot behavior separately on an accepted host.

This chapter is an interface overview, not a second privileged API or a deployment authorization.
