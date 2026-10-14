# Station/archive role lifecycle: production controller access contract

**Date:** 2026-10-13
**Producer:** box-control + root-control
**Status:** source contract staged on `unstable`, CI and host acceptance pending.
**Consumer:** host-deploy (owner); host-discover (role-election caller).
**Invariant:** only host-discover decides when the archive role is selected; box-control does not poll, elect, or independently supervise archive placement.

## Verified existing mechanisms (no replacement subsystem)

- `BoxControl(Project(path))` uses an explicit canonical project.
  `Project.require_state_allowed()` enforces that mutation state is
  beneath **the invoking controller identity's home**. It is not an
  authorization service for an arbitrary service UID.
- `RootControlSystemdTransport(socket_path)` already accepts an explicit
  Unix socket path, or defaults to `/run/zog/root-control.sock`. This is a
  **privileged mechanism socket**: anyone who connects can request other
  root-control mechanisms and must be fully trusted as a project controller.
- `ImageBuildProvider` is read-only and reads an existing active
  published image. `images.published_selection(project,generation)`
  already accepts a verified exact image generation without building.
- `BoxControl.issue_application_request_id()`,
  `launch_application(...,request_id=...)`,
  `cancel_application_launch(...,request_id=...)`,
  `terminate_application_runtime(runtime_id,request_id=...)`,
  `application_request_result(request_id)`, and
  `observe_application_runtime(runtime_id)` are existing durable APIs.
  `request_application_launch` queues work for `evaluate()`;
  **direct** `launch_application` executes in a serialized controller
  mutation. The caller must not submit the same action through both paths.
- Launch/termination operation records bind the immutable runtime ID,
  request identity, rootfs generation, execution grant, prepared
  application definition and software view. Prior-boot and unloaded
  systemd invocations remain distinguishable in observations; uncertain
  operations must be recovered or abandoned, not replayed. The
  48-hour request/idempotency retention is an explicit boundary.
- `start_policy='keep-running'` is suitable for the *station portal*
  under a separate boot-time project `evaluate()`. Archive
  `start_policy='externally-controlled'` must remain nonautomatic and
  launch only on host-discover selection with a valid lease.

## Missing access boundary and implemented v1 solution

In the rehearsal, the project was under `/root`, with a custom
root-control socket. The current host-discover `BoxBridge` directly
constructs `BoxControl(Project(path))`; giving an unprivileged
host-discover service permissions to mutate the controller's state
and access the privileged root-control socket is **not acceptable**.
Changing `Project.require_state_allowed` to grant broad cross-home
writes is likewise not acceptable.

The supported v1 solution is a *local, single-application RPC broker*,
`zog.box_control.role_broker`. It runs as the unprivileged owner of a
dedicated **production** project under that owner's home. It is a
stateless, synchronous authorization and invocation adapter, NOT
another application supervisor or role elector. The broker exposes
only operations explicitly invoked by host-discover:

| RPC operation | Body fields beyond schema/operation | Meaning |
| --- | --- | --- |
| available | none | Confirm the exact named persistent externally-controlled app still exists |
| preparation | none | Read its state/revision/storage ID only |
| preflight | none | Sanitized advisory checks for exact published image, mount targets, preparation, software and systemd connectivity (no messages or secrets) |
| issue | none | Issue one durable application request identity |
| start | request_id | Direct, idempotent launch of exactly the broker-bound app; only if preparation ready |
| cancel | request_id | Supported cancellation / outcome reconciliation of a launch request for that app |
| result | request_id | Read-only result only for that app's launch request; unknown remains unknown |
| terminate | request_id, runtime_id | Terminate one saved runtime belonging to that app |
| observe | runtime_id | Read only an exact matching app runtime's observed programs |

Responses identify the fixed application and never allow a request to
set application, project, image, software view, grant, uid, gid,
command, workspace, systemd unit, host binding, other path or PID.
Oversized, malformed, ambiguous and duplicate-key requests are
rejected. The request/reply schema is
`zog-application-role-broker-v1`, one bounded JSON frame over
`AF_UNIX/SOCK_STREAM`. Every call verifies Linux `SO_PEERCRED`
against the one explicitly allowed, **non-root host-discover UID**.
Authorization occurs before parsing request bytes. Errors are
generic; detailed secrets or absolute paths are not returned.
The broker performs no action autonomously on boot or heartbeat.
Server/client are available in `role_broker.py` and
`role_broker_client.py`.

The production daemon requires:
`--project`, `--application`, `--authorized-uid`,
`--generation` (64 lowercase hex), and
`--software-view` (`sha256:<64-hex>`).
`--root-control-socket` selects an explicit local controller
mechanism socket when the reviewed default is not used.

The broker's `FixedPublishedImage` adapter calls the existing
`published_selection` for the exact generation, **not** the mutable
active symlink. Admission verifies the configured immutable software
view against the current parsed application specification.
The normal controller's authoritative operation record then binds
the exact effective spec/software/grant to the request. Missing or
changed selection and preparation remain blocked. Broker readiness
is not a claim that an external lease is valid: host-discover must
check its own signed lease before every role-triggered action.

## Host permissions and systemd socket activation

Root-control now accepts optional `ROOT_CONTROL_SOCKET_GID=<gid>`
in its **privileged deployment environment**. Default remains
root-owned socket mode 0660. When set, only the group of a **trusted,
dedicated project-controller account** gets access to the privileged
root-control Unix socket; it must never be the host-discover group,
station/archive application groups, general service-user group or
any non-controller role. This change is a socket file ownership option,
not a new public root-control RPC.

The role broker requires **one systemd socket-activated listener**
(inherited FD 3, `LISTEN_PID` and `LISTEN_FDS=1`), not a
user-selected address or locally created socket. A representative
host-deploy-managed configuration, with placeholders requiring an
actual owner/group/security review:

```ini
# /etc/systemd/system/zog-archive-role.socket
[Unit]
Description=Zog archive lifecycle role broker
[Socket]
ListenStream=/run/zog/archive-role.sock
SocketUser=<trusted-project-controller-user>
SocketGroup=<dedicated-host-discover-group>
SocketMode=0660
RemoveOnStop=yes
[Install]
WantedBy=sockets.target
```

```ini
# /etc/systemd/system/zog-archive-role.service
[Unit]
Description=Zog archive role lifecycle broker
Requires=zog-archive-role.socket
[Service]
Type=simple
User=<trusted-project-controller-user>
Group=<trusted-project-controller-group>
ExecStart=/usr/bin/python3 -m zog.box_control.role_broker \
 --project <canonical-project-under-that-user-home> \
 --application archive-mirror --authorized-uid <host-discover-uid> \
 --generation <approved-image-generation-hex> \
 --software-view sha256:<approved-software-view-hex>
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=<canonical-project-state-directory>
```

Host-deploy must validate exact package paths, Python environment,
socket path parent modes, controller ownership, root-control socket
group, systemd credentials and import namespace before installation.
Do not copy the rehearsal's `/root/zog-station-composition-oct13`
project or grant IDs into production. No root grant/user may be
created by host-discover. Do not start or enable this unit before
the project, image, software, storage and admin grants are staged.

## Required host-discover consumer adjustments (owner C)

`BoxBridge` currently assumes it can open the project directly.
Host-discover should add a typed, optional broker-socket selection
and instantiate
`RoleBrokerClient(socket_path,application='archive-mirror')`.
When using the broker, `available()` must call
`client.application_available()`, not read the protected project
filesystem; and stop must persist an explicit **termination request
identity before invoking** `terminate_application_runtime(...,
request_id=...)`. Keep launch-request identity across
retry/lost-reply/restart; use the broker's `cancel` and read-only
`result` to resolve ambiguous acceptance. Do not synthesize
a new launch identity while an old outcome remains unknown.
Keep existing role/lease checks and unknown-observation semantics.
Do not bypass broker scoping by adding a generic execution RPC.
The broker client requires the fixed application name and checks
the response echo to defend against connecting to the wrong socket.

A root-owned `/run/zog` socket selected by host-deploy can be
used for the trusted project controller, but the host-discover
process itself never receives that socket's group.

## Boot, storage, migration, recovery and withdrawal order

1. Host-deploy inventories and pins **source commits, project owner,
   project path, immutable generation, software views and stored
   grant IDs**, backs up protected registry/account records, and
   stages correct trusted root-control group/socket properties.
   The station and archive applications are distinct persistent
   singletons with different controller-issued UID/GID/storage IDs.
2. A trusted project owner verifies image and immutable software
   selection, and explicitly prepares each application using the
   existing `prepare_application` / `refresh_application_preparation`
   contract. Preserve the prepared grant/storage binding. Do not
   launch a process or migration until preparation is ready.
3. The **host-deploy migration transaction** copies a fresh
   writer-fenced checkpoint into empty destinations and uses the
   supported two-destination storage migration. Explicitly advance
   the archive preparation revision and run the documented
   `zog.archive_mirror.storage_ownership bind` with actual granted
   values. Preserve v2 marker and evidence; do not recursively chown
   or repair destination ownership on an ordinary launch.
4. The station portal is launched independently via a separate
   boot-time controller `evaluate()` with `keep-running`
   policy, or a reviewed equivalently durable station-only boot
   unit. **Never** evaluate an archive role as `keep-running`.
5. The archive role broker remains passive. Only host-discover's
   selected role *and signed fresh lease* may generate an archive
   launch request. The synchronous broker call uses that durable
   request ID and saves exact runtime evidence. Repeated heartbeats
   reconcile the original request/result/runtime. Do not conflate
   launch acceptance with serving readiness.
6. On withdrawal or lease expiry, use
   `cancel_application_launch` first. A pending/uncertain status
   is **not stopped**. If an accepted runtime ID is proven, issue
   and retain a separate termination request ID, terminate precisely
   that runtime, and confirm both archive programs absent/inactive.
   The station portal is unaffected.
7. After a controller or broker restart, replay no new action
   until saved requests, runtime references, operation records and
   `recovery_status()` have been inspected. A failed persistence
   barrier blocks mutations; automatic recovery resumes only proven
   safe steps and preserves the original request/runtime IDs.
   A reboot changes the host boot identity and runtime process
   identities; never claim serving on stale previous-boot records.
8. Host-deploy Work must test fresh boot placement, loss of controller,
   role selection/withdrawal races, repeated apply and completion,
   missing image/grant/preparation/data mount, lost reply, retained
   request after restart, prior-boot observation and pending
   recovery. Only production-matched **native systemd + real reboot**
   evidence can close these gates. Neither the broker nor its
   tests perform host migration or port-443 traffic switching.

## Boundaries and acceptance

No host-discover-specific role algorithm was added to box-control.
No privileged root-control control operation was extended for the
role producer. No new app account or persistent storage was allocated
by broker source. The existing process/mount/credential observer
interfaces remain read-only.

Source tests: `box-control/tests/test_role_broker.py` and
`root-control/tests/test_daemon_socket_access.py`, plus existing
request/operation/reboot/preparation/credential/mount tests.
Full GitHub Actions or live host acceptance must be reported
separately. Production cutover remains **NO-GO**.

## Redaction and stable blocked-state reporting

The broker **never forwards unfiltered** `BoxControl.observe_application_runtime`
results. In particular, the normal controller's systemd
`Environment`, `ExecStart`, `BindPaths`, `BindReadOnlyPaths`,
`RootDirectory` and other host paths are excluded from the broker
response. Host-discover receives only a bounded runtime status,
persisted state, cleanup flag, nonsecret invocation IDs, and each
program's `active_state`, `sub_state` and `result`.
A wrong runtime, incomplete service observation or duplicate program
is rejected rather than returned with an implied healthy state.

Unsuccessful requests are `{"ok":false,"error":"blocked","code":"..."}`
and never echo raw exceptions, secrets, commands or source paths.
Recognized codes include `preparation-not-ready`,
`application-unavailable`, `definition-mismatch`,
`scope-rejected`, `image-selection-invalid`,
`image-unavailable`, `storage-fault`,
`recovery-required`, `controller-busy` and
`controller-blocked`. Errors returned to the broker client
are structured as `RoleBrokerError.code`; a client must treat every
code as not-serving and must not infer success from a previous
accepted launch. The trusted project-controller owner retains
authoritative internal diagnostics without exposing them through
the role producer.

## Source verification status

Exact focused source-test commands, when a CI runner is allocated:

```sh
python -m pytest -q box-control/tests/test_role_broker.py
python -m pytest -q root-control/tests/test_daemon_socket_access.py
```

Also execute the pinned `cross-repo-pytest.yml` workflow, which
covers original controller request cancellation, durable operation
recovery, root-control systemd, mount and managed-identity tests.
The October 13 Actions runs following this implementation ended
before any runner was allocated (`runner_id=0`, `steps=[]`, zero
pytest results). Source publication **does not** mean CI acceptance.
The host owner must run native role lifecycle, read-only observation,
request replay/withdrawal and boot tests before production cutover.

## Concurrent role requests and safety

The broker uses at most four simultaneous RPC workers with eight bounded
admission slots. This allows an owner-authorized cancellation or
read-only observation to be served while a previously authorized
launch is blocked waiting for systemd. **Box-control's original
ProjectLock and durable operation record remain the sole lifecycle
serialization and recovery authorities**. A worker thread is not a
background supervisor and never generates its own lifecycle request.
An overloaded broker closes surplus connections; consumers must
retry/reconcile the *same* persisted request identity and must not
infer a launch or stop result from connection loss.

The `preflight` operation reuses
`BoxControl.preflight_application_launch` against the broker's
immutable pinned-image provider, which exposes both `ensure` and
`preview` with identical verified generation selection. This is
**advisory only**: it does not admit an operation, prepare state, grant
identities, validate a current lease or start a process. Response
checks contain only stable `code`/`status` fields, with all
operator messages and paths withheld. A `ready-to-attempt` response
cannot be treated as serving or substitute for the actual launch,
kernel observation and active lease.

When the caller is a role producer, retain the request ID *before*
sending a mutating RPC. A missing reply/timeout is an unknown result,
not permission to submit a new request ID. Request status, durable
operation records and the precise runtime ID determine whether
cancellation or termination is safe. A previous-boot runtime can
never prove post-boot serving. On unavailable image, invalid
preparation, missing grant, durable storage fault or unresolved
recovery, return blocked/not-serving with a typed code; do not
recreate authority from current application files or service state.
