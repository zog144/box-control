# Verification

## Different test environments prove different facts

Box-control's contract spans pure Python preparation and durable state, mock/transport simulation, privileged root-control/systemd execution, host reboot, and station-access interaction. Passing one test tier is not evidence for the others. Each result must identify the exact source revision, selected immutable rootfs, controller project/transport, boot ID, retained request/job identities and the tests actually performed.

Historical catalogue documents preserve specific pass numbers and commands, some from stages before later fixes and API revisions. Do not present their dates as a current deployed-service acceptance report. In particular, a mocked service cannot establish real mount, rootfs or journal behavior, and an isolated EC2 systemd acceptance cannot by itself prove a React/Django browser workflow.

## Local and simulated verification

Unit tests can exercise parsing/dependency resolution, deterministic transient service names, durable request identities, accepted-operation journaling, prepared-input binding, cleanup protection, idempotency, retention and serialized recovery. Fault injections should cover storage write/fsync errors, lost submission replies, unobserved execution outcomes, corrupted references, wrong current boot and contradictory systemd observation.

`tests/reboot_phases.py` has simulated cases for completed, prepared, uncertain and failed operations with fresh processes/fake transports. This tests the recorded recovery logic, **not** an actual kernel reboot or sudden power loss. No source test may auto-clear an uncertain prior launch or substitute today's generation for a prepared operation.

Read [reboot recovery phases](../catalogue/reboot-testing.md) and [recovery inspection](../catalogue/recovery-inspection.md).

## Privileged systemd acceptance

The [systemd VM procedure](../catalogue/systemd-vm-testing.md) requires a dedicated disposable Linux machine with systemd PID 1, cgroup v2, a real root-control service, an isolated project and a deliberately prepared minimal rootfs fixture. It must **never** test against the VM's `/` as an ad hoc application image or reuse a production Zog project.

Acceptance observes real `Type=exec` services, Invocation ID, cgroup membership, mount/rootfs policy, service exit, cleanup, signal propagation and retained runtime evidence. Use the tested distro/systemd version and controller/rootfs revision, not hypothetical compatibility. Root-control's independent validation, reserved paths and host capability policy must be tested both for an accepted narrow grant and for denied mismatches/overbroad grants.

Live tests may mutate privileged state on the disposable host; they remain opt-in and require appropriate root access. Store outputs and journal excerpts as bounded evidence, not only a summary count.

## Reboot, uncertainty and cleanup

Two-phase actual reboot tests prepare durable controller and fixture state in a **persistent** directory, capture a checkpoint, undergo one externally coordinated normal reboot, then reconnect in a fresh process and prove boot ID changed. They inspect old units, prepared versus attempted service starts, exact request deduplication, protected generations, pending cleanup, and explicit handling of an uncertain result.

An abrupt power failure, SSD write-cache corruption or arbitrary storage-controller loss is **not** implied by a successful normal reboot test. Explicit abandonment in an acceptance fixture validates operator control but is not an automatic production recovery policy. See [reboot cases and preparation](../catalogue/reboot-testing.md).

## Integration with image-build, station-access and host deploy

Finite build-job acceptance checks that registered roots are immutable after publication, writable source/output mappings are scoped, execution UID/GID and deadlines are enforced, journald and cleanup evidence stay tied to exact request/job IDs, and registration reply timeouts do not cause duplicate imports. Image-build retains the source/attempt/output provenance; box-control does not call a compiler or release a new rootfs generation on its own.

Station-access integration requires browser authorization and correctly scoped runtimes/logs, without exposing trusted paths or controller credentials. A UI preflight's ready state cannot promise successful launch; read-only inspection must not create files or systemd units. Workspace acceptance must include the actual X11/VNC listener, authorized client attachment and the host-shared network caveat; source-only preparation is not a working graphical seat.

Host-deploy is the normal owner of EC2 provisioning and normal-reboot coordination. Do not infer production activation from installation into a disposable compiler host.

## Readiness checklist

| Verification tier | Required evidence | Limits |
|---|---|---|
| Static/code and namespace | Tests, import paths, exact pinned companions | No live service |
| Durable controller simulation | Stored operation identities, fault injection, recovery decisions | No real mount or kernel reboot |
| Privileged VM/systemd | Real unit/cgroup/invocation/mount inspection and cleanup | Disposable host and selected distro only |
| Normal reboot | Changed boot ID, exact preserved operations, uncertainty handling | Not abrupt power loss |
| Graphics and application integration | Real X11 credentials, VNC endpoint, application data mounts and browser authorization | Platform/image-specific |
| Production cutover | Host-owner approval, current snapshots, backup/rollback and single-writer proof | Separate operator authorization |

## Original acceptance material

- [Systemd VM fixture and scenarios](../catalogue/systemd-vm-testing.md)
- [Two-phase reboot cases](../catalogue/reboot-testing.md)
- [Build registration uncertainty](../REGISTRATION-RECOVERY.md)
- [Controller and station-access integration](../catalogue/station-access.md)
- [Immutable software and writable data mounts](../APPLICATION-MOUNTS.md)
- [Workspace desktop/runtime contract](../WORKSPACE-APPLICATIONS.md)
- [Structured fault inspection](../catalogue/recovery-inspection.md)

Retain all historical acceptance and failures as evidence. Do not suppress or rewrite failed results to make a particular chapter look finished.
