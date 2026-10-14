# Lifecycle, operations and policies

## Specifications, runtime identities and observation

Box-control is the trusted local single-shot controller for native program services. A **program runtime** is one command line under a transient systemd service; an **application runtime** groups one or more programs under the application's slice. Immutable runtime/instance/Invocation IDs identify what was launched, while descriptive labels are presentation metadata. A new launch or replacement receives a new runtime identity; existing identity and historical software binding are not rewritten when the declaration changes.

The running system is not the desired specification. The controller persists **expected-launched** configuration separately from the current desired declaration and the current systemd observation. Unexpected observed-versus-expected differences require integrity inspection; desired-versus-expected differences can be legitimate drift, not automatic restart instructions.

## Start policy is an application admission rule

The documented application policies distinguish `START_ONCE_PER_BOOT`, `KEEP_RUNNING` and `EXTERNALLY_CONTROLLED`. Start-once policy counts a launch attempt against the boot ID, even if a short-lived service exits; keep-running can request a new application launch during evaluation according to its own admission rules, but does **not** translate to systemd `Restart=always`. Externally controlled applications require an explicit caller request. See the historical [implementation-status policy](../catalogue/implementation-status.md); evaluate it against current source rather than relying on its old pass-number claims.

Multi-instance applications receive separate instance identities; singletons preserve one stable instance identity. A runtime superseded by another runtime of that instance cannot be treated as the current target for a restart. Program launch, service acceptance, unit Invocation ID and application completion are different milestones.

## Durable requests and exact-target actions

The public API supports launching by application name and restart/termination by **immutable runtime ID**. Station-access should issue and persist a controller request ID **before** attempting the call, then reuse the same ID after lost acceptance. Reusing one ID with a different intent is rejected. A completed result, pending durable operation, busy lock, blocked recovery state and failed call have different meanings; failure does not prove all possible external effects were reversed.

Station-access owns browser authorization, owner lookup, CSRF and polling. Box-control remains synchronous local Python code; it is not an independent network authentication layer, background scheduler or UI. Disconnecting a browser does not cancel an already accepted operation. See [station-access controller contract](../catalogue/station-access.md).

## Preparation, launch and cleanup

A prepared operation durably binds the resolved application definition, selected generation and replacement targets before a service side effect. During launch, systemd must record the expected per-program start/invocation evidence before the application is committed. A partial multi-program launch cannot be silently promoted to a healthy application: cleanup and failed-launch evidence must remain durable.

Application **preparation** is distinct from launch. Persistent storage preparation and generation upgrades are explicit, bounded command sequences; they may update data and thus cannot be automatically replayed when the outcome is unknown. Running programs are not restarted merely because a build completes or the desired configuration changes. For exact recovery rules see [recovery, integrity and retention](recovery-integrity-and-retention.md).

Cleanup is part of the transaction's safety boundary. Pending cleanup protects runtime references and immutable inputs even when a service is no longer running. Termination of an exact runtime does not implicitly delete user storage or journal history. The normal runtime-history display floor is separate from longer request and operation retention.

## Finite build commands are a different facility

Build commands execute with a registered, finalized build root, bounded source/output workspaces, explicit nonzero execution UID/GID and finite deadlines. They use the privileged host controller/systemd, **not** an application generation, Docker fallback or automatic application restart policy. Image-build owns package attempts and output artifacts; box-control only executes and records controlled commands.

A caller registers the same resource ID once, persists a build request ID and observes the exact job rather than inventing another on lost replies. Read-only build lists/logs are scoped to stored identities and do not claim an arbitrary process is a build output. See [finite build jobs](../catalogue/build-jobs.md) and [build visibility](../catalogue/build-visibility.md).

## Controller diagnostics are not authority

Structured `zog_event` records correlate requests, operations, runtime IDs, unit names and invocation IDs. They are best-effort logs, not a second authoritative operation ledger or exactly-once event stream. Detailed fault and environment contents remain sensitive. See [lifecycle events](../catalogue/lifecycle-events.md) and [read-only recovery inspection](../catalogue/recovery-inspection.md).

## Reference map

- [Application and workspace runtime layout](applications-workspaces-and-mounts.md)
- [Synchronous controls and station-access integration](../catalogue/station-access.md)
- [Request issuance and 48-hour bounded retention](../catalogue/request-retention.md)
- [Operation recovery and reboot sequencing](../catalogue/operation-recovery.md)
- [Build registration and finite jobs](../catalogue/build-jobs.md)

The above is a synthesis of owner contracts. It does not authorize calls outside their normal guards or claim production deployment from source-only tests.
