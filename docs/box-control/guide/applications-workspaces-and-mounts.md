# Applications, workspaces and mounts

## Runtime identities and responsibility

An application is a declared composition of one or more program services. A launch gives that composition an immutable runtime identity, with program services managed by systemd; human-facing labels may change without changing that identity. A persistent application retains an independent **storage identity** through runtime replacement. The configured application name is not a data-migration operation, and historical per-runtime data is not silently adopted.

A prepared launch binds the resolved declaration, selected immutable software generation and affected runtime identities. Controller expectations are persisted independently from current desired configuration and the observed systemd state. Changing a specification after preparation does not retarget recovery.

## Persistent data preparation and upgrades

Persistent applications explicitly declare `persistent=True`, named `writable_mounts`, a `preparation_revision` and preparation programs. First preparation obtains dedicated durable storage and executes finite, ordered commands as the declared unprivileged account. The user must explicitly call `prepare_application(name)` and poll `refresh_application_preparation(name)` to terminal `ready`, `failed` or `uncertain` state; ordinary evaluation does not start or retry preparation on its own. Each preparation program retains its runtime identity and invocation-scoped logs.

A new software generation or preparation signature calls for an explicit upgrade, after stopping the prior application and completing cleanup. Data migrations belong to the declared preparation recipe. A failed/unknown step is **not automatically replayed**, because partially executed database changes cannot be undone by a controller restart. Operators inspect evidence and authorize retry; missing storage is not silently reconstructed. Consult [persistent application environments](../PERSISTENT-APPLICATIONS.md).

## Read-only software and controlled writable mounts

The selected rootfs generation is read-only and is not modified by application preparation. Box-control's `zog-mounts-v1` contract keeps explicit writable data destinations separate from immutable software. It rejects protected root and system paths, overlapping/canonicalization hazards, symlink redirection and absent image-side mount directories. `/etc`, `/var` and `/run` cannot be broadly overlaid even though narrow permitted data subdirectories may be allowed. Executable entry points cannot reside under application writable mounts.

Program runtime references retain a mount inventory including source, destination, access, kind, lifetime and owner identity. `application_runtime_mounts(runtime_id)` reports the **expected-launched** inventory with `observed_mounts: not-probed`; it does not observe the kernel live mount table. Legacy references can truthfully report `legacy-unrecorded`. See [mount policy](../APPLICATION-MOUNTS.md).

## Workspace attachment and lifecycle

The `zog-workspace-v1` contract binds a stable canonical workspace UUID and never-reused integer number. Host-shared networking is the supported v1 profile; **it does not provide network isolation**. Desktop/application membership, display credentials, endpoint lifetime and runtime identities are controller-owned. An application opts in as `workspace_role='client'` and may not override reserved display/authentication/mount variables. A desktop is a separate explicit `workspace_role='desktop'` application, launched by its authorized consumer on first use; stopping a client does not stop or replace that desktop.

The controller supplies bounded workspace operations and exact request/runtime identity interfaces; the UI does not gain permission to synthesize arbitrary paths, credentials or DISPLAY values. The desktop recipe's X11/TurboVNC readiness and actual-host acceptance are separate from syntax validation. Read [workspace controller contract](../WORKSPACE-APPLICATIONS.md).

## Software views and lifecycle guarantees

Image-build publishes immutable application-software artifacts and symlink-composed views; box-control consumes a verified, exact read-only binding without rebuilding software during launch. Persistent data references, prepared operations and unfinished cleanup protect corresponding generations from premature reclamation. New application software is introduced by selecting a new publication and deliberately preparing/upgrading compatible data, never by writing packages into a live generation.

### Authoritative interfaces

- [Persistent application preparation and failure handling](../PERSISTENT-APPLICATIONS.md)
- [Immutable software and data mount policy](../APPLICATION-MOUNTS.md)
- [Workspace application, identity and networking contract](../WORKSPACE-APPLICATIONS.md)
- [Launch preflight](../catalogue/launch-preflight.md)

This chapter describes owning components and safety rules, not an instruction to operate or replace a production desktop without its verified environment.
