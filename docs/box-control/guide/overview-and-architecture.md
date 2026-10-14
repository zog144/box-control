# Overview and architecture

## Single-shot controller for native application runtimes

Box-control evaluates an explicit application declaration and controls **native systemd program services** through the separate privileged root-control daemon. It has a trusted local Python API; it is not a browser-facing host management endpoint, not a compiler, and not a replacement for an application's own service-level logic. One invocation evaluates or performs a bounded operation and returns; it does not install a new all-purpose process supervisor.

An **application** is one or more declared programs under an application slice. A **program runtime** is one command with a transient `.service` and saved Invocation ID; an **application runtime** is that composition under a child slice of Zog's parent slice. Services use `Type=exec`, `ExitType=cgroup`, `Restart=no` and journald. Program descendants remain in the service cgroup; box-control is responsible for bounded lifecycle and cleanup, not deciding internal application health from a child's command line.

## Three levels of authority

| Boundary | May decide | Must not decide |
|---|---|---|
| station-access consumer | Browser authentication, workspace ownership, explicit user intent | Arbitrary systemd/host paths, controller identity or compiler owner |
| box-control local library | Parse requested application, freeze launch selection, reconcile expected and observed, track history/recovery | Build/replace immutable images, bypass privileged checks |
| root-control privileged service | Validate and perform the permitted native systemd/mount operations | Interpret arbitrary browser user authority or publish image-build generations |

**Image-build** publishes immutable rootfs generations and application views. Box-control consumes exact selected and compatible immutable identities and binds allowed writable STATE mount sources, but cannot build a missing image. **Host-deploy** owns remote/cloud execution and gateway admission, not application lifecycle policy.

## Desired, expected-launched and observed

The application specification expresses **desired** configuration. A prepared launch freezes **expected-launched** configuration: application/runtime identity, selected generation and software view, commands, mounts and program/service identities. Actual systemd state is **observed** only through the authorized inspection interface.

- Desired differing from expected-launched is configuration drift, **not** a licence to rewrite the running runtime.
- Observed differing unexpectedly from expected-launched can be an integrity fault, not something silently "fixed" by another launch.
- Display names may change; immutable runtime/instance identity and the generation selected at launch do not.
- Runtime-history retention, failed launches, prepared operations and pending cleanup are distinct persisted records with different protection rules.

The [lifecycle chapter](lifecycle-operations-and-policies.md) explains start policies and exact-target operations; [inspection](inspection-and-events.md) distinguishes stored from live evidence.

## Durable transaction and recovery model

Before a service side effect, box-control saves a **prepared** operation binding the resolved launch configuration, rootfs generation and affected runtime identities. Persisted request IDs make an accepted operation idempotent; replay after a lost response must use the same request and bound materials. A different request cannot claim the result of the first. Storage writes use durable atomic publication and fault-preserving interlocks.

At startup/reboot, recovery distinguishes an unattempted prepared start from a service start whose acceptance is uncertain. It may continue a safely bound operation, but never infer that the service did **not** start because the transport reply was lost. Unexpected observation or missing original inputs blocks instead of substituting a newer configuration. Pending cleanup protects history and generations through completion. The [recovery chapter](recovery-integrity-and-retention.md) covers uncertainty, 48-hour completed-request retention and guarded pruning.

## Isolation and persistence

Program services run within Zog-owned systemd cgroups and mount namespaces with `pivot_root` on a read-only generation. Writable `/home`, `/tmp` and explicitly allowed persistent data are supplied through controlled mounts rather than modifying software. No user namespace or general container runtime is implied. Named application persistent STATE has its **own storage identity**, separate from each immutable application runtime; preparation/upgrade is explicit and can include non-repeatable data migration actions.

Workspace desktop and clients have separately owned runtime identities, reserved DISPLAY/Xauthority/VNC endpoint bindings and a durable workspace UUID/non-reused number. The documented v1 network profile shares the host network namespace; that is **not network isolation**. The desktop is not automatically stopped when a viewer disconnects or an attached application exits. See [applications and workspaces](applications-workspaces-and-mounts.md).

## Finite build jobs and read-only inspection

Box-control also supports registered finite build roots and commands for image-build. The owner saves a resource/request identity, and each finite command runs under restricted UID/GID/deadlines with source/output staging and journald. This is **not** a normal application runtime or an alternative rootfs publisher.

Station-access can request bounded read-only lifecycle observations, process snapshots, invocations, logs, build-job summaries and recovery explanations. Reading must not trigger evaluation, launch, repair or a privileged port opening. A ready preflight is advisory, and missing observations remain unknown rather than healthy. See [interface integration](interfaces-and-integration.md).

## Reference and acceptance boundary

- [Component technical manual and integration boundaries](../README.md)
- [Workspace/persistent mount and service contract](applications-workspaces-and-mounts.md)
- [Policy, lifecycle and finite jobs](lifecycle-operations-and-policies.md)
- [Durable state and request recovery](recovery-integrity-and-retention.md)
- [Consumer and privileged API boundaries](interfaces-and-integration.md)
- [systemd, reboot and controller verification tiers](verification.md)

The historical [implementation-status record](../catalogue/implementation-status.md) documents earlier passes and acceptance gaps. Specific old pass numbers and API omissions are historical evidence; use the current repository source and compatible pinned companion revisions to determine current behavior. An installed package or unit-test result alone does not establish acceptance on a real systemd host.
