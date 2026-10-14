# Application runtime execution credentials — design review and controller contract

Date: 2026-10-12. Owners: box-control (contract), root-control (privileged identity lifecycle).
Consumers: station-access, archive-mirror, host-deploy.
Status: **First-pass administrator-provisioned identity contract implemented in controller source; production not accepted.**
**Production cutover remains NO-GO.** This document explicitly distinguishes
today's supported static host credentials from the missing controller capability.

## Historical origin of `regular`

The earliest published box-control import, commit
`6b45209d7db5d9d2ecd9e29844da1e1807d56cc0` (2026-09-25,
"Import approved launch-preflight pass15 revision1 handoff bundle with
provenance"), already contained `user="regular"` in
`ProgramSpec`, `SystemdServiceDefinition`, and the specification parser.
The October 7 namespace migration moved those same defaults without
documenting a provisioner. Earlier pre-publication handoff bundles may
predate this history. The Git evidence establishes **when this default
entered this repository**, not the original author's reason. No host account
provisioner or accepted architectural requirement for `regular` was found.
Treat it as a legacy implicit placeholder, not a supported globally shared
Zog identity. Do not create a `regular` account to mask missing capability.

## Current implementation, precisely

- `specification._parse_program` uses `values.get("user", "regular")`
  and optional `group`. `ProgramSpec` and `SystemdServiceDefinition`
  both default to `regular`; `service_definition` carries the strings into
  `expected_properties` and `to_transport_dict`.
- The privileged `root-control` `start_service` builds D-Bus
  `User=<supplied string>` and, if present, `Group=<supplied string>`.
  It currently **does not bind a controller-issued UID/GID pair**.
  Systemd resolves named users/groups using the system manager's user
  database; numeric IDs are supported by systemd. Application NSS inside
  the `RootDirectory` may differ from the host database. The controller
  does not provision guest passwd/group records.
- For persistent data, `root-control._persistent_directories` explicitly
  looks up named users with host Python `pwd.getpwnam`, numeric user
  strings with host `pwd.getpwuid(int(user))`, named groups with
  `grp.getgrnam`, and numeric groups directly. Thus **even a numeric
  persistent UID currently needs a host passwd record**. The privileged
  service rejects resolved UID=0 or GID=0, creates new mount directories
  at `state/mounts/persistent/<storage_id>/<name>` and calls
  `chown(uid,gid)` only on newly created directories. On existing
  directories, mismatched UID/GID is a blocking error, not an auto-chown.
  The October 12 hardening makes missing account/group failures explicitly
  reported before mutating storage.
- Persistent specification validation requires a uniform user/group
  **string tuple** among preparation and service programs and forbids
  the literal `root`/`0`. It does not establish that two distinct names
  resolve to the same UID, or that an innocuous name maps to UID 0;
  resolution is performed by the privileged daemon.
- Existing historical program expected properties include `User` and
  optional `Group`, but there is no dedicated, stable host identity
  allocation/provisioning record bound to prepared operations. Rootfs
  `/etc/passwd` is immutable image software and is not currently
  synchronized to execution identities. Namespace/rootfs isolation does
  **not** substitute for distinct kernel UIDs.

## Proposed target contract: `zog-execution-identity-v1`

1. **Controller-owned grant**: the application specification declares
   `execution_identity="application"` (symbolic), never a privileged
   user name or numerical UID. root-control creates or resolves a
   root-owned durable identity record keyed by stable project identity,
   application identity, and application storage lifetime. Provisioning
   is a privileged administrative operation; ordinary application calls
   cannot select an ID, change grants or replace the record.
2. **Stable non-root numbers**: reserve a distinct numeric UID and GID
   per identity. Allocation checks all host NSS users/groups, existing
   Zog grants, pre-existing files/state ownership, and excluded/reserved
   UID/GID ranges. Avoid systemd DynamicUser recycling for durable
   application state. **No particular UID/GID range is prescribed before
   host inventory is checked.** Explicit root and supplementary-group
   exclusions, no ambient privileged groups. Persist an immutable grant
   ID/version and provisioning proof; never reassign an identity while
   any state, runtime, prepared operation or cleanup can reference it.
3. **Durable preparation binding**: before any preparation/launch/stop
   operation is accepted, bind the exact (grant ID, version, UID, GID,
   project, application, storage ID and executable policy) into its
   authoritative operation record and launched runtime reference.
   Recovery must use those original numbers, even if defaults,
   membership or external policy have changed. Missing or replaced grants
   block; there is no silent reallocation or substitute.
4. **Privileged enforcement**: root-control independently validates
   the protected grant and numeric IDs before any new state directory is
   created or service starts. It passes explicit numeric `User` and
   `Group` to systemd, verifies observed properties, and never accepts
   untrusted caller-selected UID/GID. All persistent preparers and services
   use the same bound grant and have ownership/DAC checks. Prefer strictly
   bounded group membership. If app libraries need NSS identity records,
   expose verified minimal controller-owned guest passwd/group data
   without modifying the immutable generation.
5. **Storage ownership and migration**: persistent mounts belong to
   that grant, not a mutable user label. New mounts are created owned by
   the bound UID/GID and a private mode. Existing mounts are audited
   before adoption; mismatches require a separate explicit migration
   (with backups, exclusive writer fencing, evidence and rollback), not
   recursive automatic chown. Archive content, SQLite WAL/SHM, secrets,
   home/cache paths and host-export DAC all require a reviewed mapping.
6. **Lifecycle**: identity is reusable across preparation/restarts/
   upgrades/reboot as long as the persistent storage identity survives.
   Reclamation requires no active/recoverable runtime or host-owned
   references; do not delete external host users, keys, or data during
   ordinary application history pruning. For nonpersistent multiple
   instances, isolation policy/instance-scoped identity must be chosen
   explicitly, rather than inherited from this persistent contract.
7. **Authorization/verification**: verify two applications cannot
   select one another's grants or persistent storage; reject wrong
   user/group, named aliases of root, stale grants, ownership changes,
   missing credentials and partial recovery. Exercise real systemd,
   host NSS, guest NSS, DAC, encrypted secrets and reboot.
   As with all Zog kernel credentials, the absence of user namespaces
   means host UID/GID separation must be checked across applications.
8. **Compatibility**: legacy `user`/`group` fields can remain
   temporarily in legacy, explicitly provisioned deployments, with a
   preflight diagnostic and deprecation plan; *new production persistent*
   applications must not inherit the `regular` default. Do not
   unconditionally rewrite old persisted expected properties.

## Dependency and rollout order

1. root-control and box-control implement the protected allocator/grant
   provisioning, binding, stable numeric systemd properties and recovery.
2. host-deploy installs production identity records before persistent
   preparation, audits the **actual existing legacy station UID/GID** from protected host records and host export permissions;
   provision group memberships and application view guest identity
   compatibility as needed.
3. station-access removes literal `user="regular"` from preparation,
   web and scheduler definitions, switches to the symbolic application
   identity, and tests the exact same grant in all programs and across
   restart. The first preparation step must not invent new credentials.
   Do not assume any legacy station UID/GID may be reused or
   automatically changed; preserve its exact host-local recorded value. Verify database migration owner, WAL/SHM, secrets and
   login, host gateway and host-discover export DAC.
4. archive-mirror uses its *own* independent identity/grant for any
   archive service, and explicitly approves on-disk migration of
   `/var/lib/archive-mirror/store` and pin metadata. Withdrawal of
   the mirror role must not reclaim archive data. Never bind raw host
   enrollment credentials to the web application.
5. Host-deploy/Work carries out host-privileged identity provisioning,
   isolated rehearsal, one-writer maintenance window, kernel UID/GID/
   DAC observations, production readiness and controlled nginx cutover.

## Current source-hardening deliverable and unresolved blocker

This review adds a clear error when a persistent `User` or named
`Group` is not provisioned in the host database; no data directory is
created in those cases, and no automatic account provisioning or
chown/reallocation is attempted. That is deliberate **fail closed**
behavior, not the v1 allocator.

**Do not cut over production** until controller-managed identity binding
and provisioning, consumer migrations and live kernel/DAC acceptance
are implemented. Do not infer readiness from the tests of the old
`regular`-based string forwarding path.

## October 12 implementation pass — precise contract and remaining gates

The following first-pass parts of `zog-execution-identity-v1` have now
landed on `unstable`:

- `root-control.identities` provides an **offline, root-only** grant
  provisioner and a protected registry at
  `/etc/zog/execution-identities.json`. Its daemon exposes a *read-only*
  `execution_identity_resolve` RPC but **no** grant-creation RPC.
  Provisioning requires an administrator-reviewed numeric UID/GID range.
  The provisioner selects an unused UID=GID pair, rejecting host NSS
  user/group collisions and reuses neither issued ID nor grant ID.
  Concurrent provisions serialize under a root-only flock and publish
  registry data with file and directory fsync. Directory and registry
  protection, scope and duplicate checks are fail-closed.
- A persistent singleton application can declare
  `execution_identity="application"`. Explicit per-program
  `user`/`group` declarations are forbidden with this option. Legacy
  specifications remain readable and behave as before, including their
  historical `regular` default until migrated.
- `ApplicationStorage.begin` resolves the protected grant **before**
  the persistent preparation record is made durable and binds the exact
  ID/version/numeric UID/GID to both preparation and runtime programs.
  `ApplicationStorage.bind` reconstitutes the original bound numbers
  from that saved record, not from the current default. An explicit
  upgrade refusing a changed binding prevents silent storage adoption.
  The runtime preparation path additionally checks the current
  protected grant before recording a launch intent.
- `SystemdServiceDefinition` stores and transports the binding and
  materializes numeric `User` and `Group` expected properties. These
  values also persist inside the authoritative operation and reference
  snapshots. On start, root-control independently reads and compares
  the protected grant, including application/storage scopes and exact
  numeric properties. For managed identities, new persistent
  directories are created/chowned by number without a host passwd entry;
  existing directories with mismatched ownership block. root-control
  also validates the bound grant before declaring preparation storage
  ready.

### Provisioning protocol

Provisioning requires a root-owned `/etc/zog` with a trusted ancestry
and a **reviewed UID/GID allocation range** determined from host
inventory; there is no default range. For example (placeholder values,
not a production-ready invocation):

```text
python -m zog.root_control.identities provision <project-path> <application-name> \
    --range-start <approved-first-uid> --range-end <approved-last-uid>
```

The grant is a JSON record with exactly `schema, grant_id, version,
project, application, uid, gid`. Grants live across application
preparation/restart/upgrade and are never removed through application
history pruning or persistent storage deletion. An administrator must
treat the file and its fsynced backups as part of recovery-critical host
state. Missing, malformed, changed or revoked grants block new starts
and prepared-storage readiness. The CLI is a privileged host-deploy
procedure, **never** an end-user application capability.

### Remaining production acceptance gates

This source pass does **not** claim that all v1 security requirements
have been proven on a live host. In particular:

- Host-deploy must inventory **existing file owners, ACLs, reserved
  allocations and active processes** in the chosen numeric range;
  the allocator automatically checks NSS and other Zog grants, but
  cannot assert filesystem-wide collision freedom on its own.
- Live systemd numeric User/Group acceptance, process effective/real
  UID/GID and supplementary groups, filesystem DAC, guest NSS needs,
  rootfs read-only preservation and reboot/recovery must be tested.
- The first implementation uses a stable grant scoped to canonical
  project/application and carries the storage ID in the prepared
  service/storage evidence. It does **not** recycle grants on new
  storage generations; explicit reassignment/retirement and storage
  ownership migration remain separate high-risk operations.
- Existing production storage ownership, SQLite WAL/SHM and protected
  secrets need a deliberately reviewed migration using the **actual
  recorded legacy UID/GID**, not a hard-coded example value. Templates in station-access
  and archive-mirror must replace `regular` with the symbolic identity
  **as a coordinated consumer change**. Do not do so until an approved
  grant and migration plan are available.

**Production cutover remains NO-GO** pending host verification,
consumer identity adoption, archive data migration and owner-aware
compiler acceptance.

## October 12 live host acceptance correction — systemd 217/USER

A historical isolated host-deploy test of two already-issued,
application-scoped canary grants (one for station-access and one for
archive-mirror) confirmed durable binding and the persistent directory
owner/mode, but native service execution was blocked with `217/USER`.
The canary service did not execute in that test. Exact assigned UID/GID
values and original host/project identity are retained only in protected
operator evidence; these are not portable installation defaults.
The retained image also lacked `/var/lib/station-access` and
`/var/lib/archive-mirror` writable targets; these are separate
image-build blockers and **must not** be worked around by mutating the
published image.

On systemd v252, the system service-manager's fixed-user execution
setup requests full NSS user credentials even when `User=` is a
numeric string, and its missing-numeric-UID fast path does not apply
when GID/home/shell information is also requested. Consequently
the previous assertion that a numeric UID requires no host NSS
account *for systemd execution* was incorrect. Numeric storage
ownership operations themselves still do not require NSS.

**Revised supported mechanism:** two separate privileged, offline
administrator actions:

1. **Reserve** a protected immutable grant with reviewed numeric
   UID/GID (no default range and no automatic application allocation).
2. **Activate** the *existing* grant's account and group in host NSS
   under a stable non-login name `zog-app-<first-16-hex-of-grant-id>`.
   This is not a shared `regular` user, and it never changes the
   assigned numbers or project/application scope. Activation runs
   `groupadd` and `useradd` under the root-only CLI; it is
   idempotent across partial completion. It creates no home directory,
   requires `/nonexistent` home, a nologin shell, a locked password,
   correct account/group numeric matching and *zero additional
   supplementary groups*. Neither box-control nor the root-control
   daemon's read-only identity lookup may create or repair these accounts.

Examples of the official administrative CLI, with a pre-approved
range and canonical project path:

```sh
python -m zog.root_control.identities provision /path/to/canary-project station-access \
    --range-start APPROVED_MIN --range-end APPROVED_MAX
python -m zog.root_control.identities activate /path/to/canary-project station-access
```

**For previously issued canary grants, do not provision again.**
An authorized host owner must retrieve and independently verify the
**original protected project scope, full grant IDs, grant versions, UID/GID
assignments and durable registry identity** from that host's retained
records before running the separate activation operation. Activate only
the existing grants at their exact original scope, with no reallocation,
migration to a lookalike path or substitution of example IDs. Keep the
same root-controlled registry canonical. The generic CLI example above
is for a separately authorized *new* fixture, not a rerun against an
existing canary.
Any missing, altered, unlocked or supplementary-group-bearing NSS
record blocks resolution/preparation/recovery. Account lifecycle is
not coupled to application deletion or 25-entry runtime history
pruning. Host-deploy is responsible for restricting account-management
rights and preserving both registry and host NSS identity state
across reboot and backup.

An explicit opt-in real-systemd regression lives at
`root-control/tests/test_execution_identity_systemd_live.py`.
Run it only through the adopted host-deploy owner after setting
`ZOG_LIVE_EXECUTION_IDENTITY_PROJECT` and
`ZOG_LIVE_EXECUTION_IDENTITY_APPLICATION` to the exact isolated canary
scope; it creates a uniquely named systemd transient service,
executes `/usr/bin/id -a` under the numeric User/Group and checks
UID, GID and absence of supplementary groups. The CI suite does
**not** run this privileged real-host probe. After that probe,
repeat the original Zog application preparation using its own
saved IDs and explicit supported retry semantics. Do not erase or
replay uncertain history and do not reuse the temporary controller's
stopped services.

The revised contract supports *distinct per-application host NSS
principals*, rather than globally shared usernames. The fact that
host NSS records now exist is an intentional privilege-controlled
systemd compatibility provision, not an application-provided username,
not permission to substitute another UID, and not a request to edit
the immutable application's guest `/etc/passwd`.

**Production station/archive cutover remains NO-GO** until both
the refreshed image-build data targets and the systemd canary,
full mount/DAC/readiness/reboot tests pass.
