# Workspace application controller contract — pass 18

Baseline: box-control `b49b9d7370234cc1272e5816a6aa254687d21996`.
Consumer handoff: station-access `f4a0486978400b41379bf3645331f20f77ba26b8`.
Capability/schema identifier: **`zog-workspace-v1`**.

This pass implements controller-owned durable workspace attachment. It preserves
persistent application preparation, explicit upgrades, request cancellation,
immutable runtime identities, and journald invocation identities.
It requires a station-access adapter update; it is not a drop-in implementation
of the older bundled controller's arbitrary parameter substitution.

## Supported networking and ownership

V1 supports only `host-shared`: every workspace and the host share the initial
host network namespace. This is explicitly **not network isolation**. Namespace
lifetime belongs to host init, not an application or desktop process. Stopping
one client cannot destroy the namespace. Isolated network selections fail with
`unsupported-network`; there is no silent fallback. Services use
`NetworkNamespacePath=/proc/1/ns/net`; prepared bindings retain the namespace
inode/device and boot identity.

Workspace UUIDs must be canonical lowercase UUID strings. Numbers are integers
1–9999, unique even across deleted registrations. Names remain mutable
station-access presentation metadata. Registration is limited to 512 records,
including tombstones. Deletion retains the identity and number. Old desktop
resource epochs are retained for diagnostics; garbage collection is deferred.

## Public Python API

Methods on `BoxControl` (existing constructor is unchanged):

```python
workspace_capabilities()
register_workspace(workspace_id, number, *, network='host-shared')
change_workspace_network(workspace_id, network)
delete_workspace(workspace_id)
workspace_membership(workspace_id, *, after=None, limit=50)
workspace_status(workspace_id)
workspace_desktop_access(workspace_id)
workspace_application_catalogue(*, after=None, limit=50)

launch_application(application_name, *, request_id=None,
                   workspace_id=None, parameters=None)
request_application_launch(application_name, *, request_id=None,
                           workspace_id=None, parameters=None)
cancel_application_launch(application_name, *, request_id,
                          workspace_id=None, parameters=None)
preflight_application_launch(application_name, *, workspace_id=None,
                             parameters=None)

# Existing exact-identity operations and status/log interfaces:
application_request_result(request_id)
restart_application_runtime(runtime_id, *, request_id=None)
terminate_application_runtime(runtime_id, *, request_id=None)
application_logs(runtime_id, program, *, cursor=None, limit=50)
```

`parameters` accepts only `{'workspace-number': integer}` as a compatibility
alias for a registered UUID. If both selectors are supplied they must agree.
Arbitrary paths, credentials, DISPLAY, network values, old desktop port/password
parameters and other keys fail `unsupported-parameters`. Prefer UUIDs in new code.
Cancellation without either selector preserves the existing request-ID lookup;
when supplied, a selector must agree with recorded intent before any recovery.

## Application declarations and desktop startup

An application opts in with `workspace_role='client'`. The controller injects
DISPLAY and XAUTHORITY, the X11 socket directory, authorization directory and
network binding. Definitions cannot override these environment variables or
reserved mount targets. All workspace programs run unprivileged and must use
externally controlled lifecycle. Existing rootfs generation and preparation
requirements still apply. A single-instance application cannot replace a runtime
attached to another workspace, including an unbound runtime.

The administrator supplies a `workspace_role='desktop'` application, with one
program, `multiple_instances=True`, and nonpersistent storage. Station-access
registers the workspace, launches this desktop application on first use, polls
`workspace_desktop_access`, and then submits client launches. The controller does
not choose an arbitrary desktop recipe or automatically launch one on a client's
behalf. Leave the desktop running when clients stop. Do not restart it while
clients or pending operations remain attached.

Desktop command arguments may contain these exact standalone tokens:

| Token | Controller-resolved value |
| --- | --- |
| `{workspace-display}` | `:<workspace number>` |
| `{workspace-xauthority}` | `/run/zog-workspace/Xauthority` |
| `{workspace-vnc-socket}` | `/run/zog-workspace/vnc.sock` |

Only entire argument tokens are substituted; there is no implicit shell.
The image/recipe must supply the X server and configure it to consume this
Xauthority file, serve its X11 filesystem socket in `/tmp/.X11-unix`, and use the
private Unix VNC endpoint without a public TCP listener. TurboVNC documents
`-rfbunixpath` and `-rfbunixmode` in its upstream Xvnc manual. Include the mount
target directories `/tmp/.X11-unix` and `/run/zog-workspace` in the rootfs.
This recipe has **not** yet passed actual TurboVNC acceptance in this pass.

The controller generates a per-desktop MIT-MAGIC-COOKIE-1 authorization file.
Desktop/client programs must use the same configured user/group. Clients get
read-only mounts; the desktop gets writable mounts. Host endpoint directories
are private to that account. Station-access's authenticated VNC proxy needs
appropriate host account access; browser clients must never receive authority
files or supply host socket paths. Desktop access returns a Unix endpoint for
that trusted proxy, not a directly browser-reachable TCP endpoint. Configure the
VNC authentication policy in the administrator-owned recipe.

Readiness verifies the recorded desktop invocation, socket peer UID/cgroup and
network namespace, successful X11 cookie authentication, and a VNC RFB banner.
Probes have bounded socket/RPC deadlines. This is advisory readiness, not a
promise that the desktop cannot exit immediately afterward.

## Durable intent, recovery and teardown

Queued requests durably bind application and workspace. Preparation freezes the
resolved application configuration, generation, workspace revision, boot/network
identity, desktop incarnation, exact desktop runtime ID, display and mount
bindings. These are saved in the authoritative operation and runtime reference
before any lifecycle action. Request identity checks precede recovery: retrying
with another application/workspace cannot accidentally execute the old intent.
Later definition changes do not change an already prepared operation.

Project locking serializes admission against desktop stop, deletion and network
changes. Membership is derived from queued requests, authoritative operations
and runtime references, including uncertainty and terminal runtimes with pending
cleanup. No display parameter is used as membership evidence. Deletion or network
change requires this ownership set to be empty, including the desktop. Stops
use immutable runtime IDs. Client exit/stop leaves the desktop running.

Completed retries return recorded results after a lost reply. Cancellation of an
unstarted queued request is durable; accepted cancellation returns the exact
runtime identity for an explicit stop. Prepared or attempted uncertain work is
not silently abandoned. An interrupted prepared launch cannot switch to a new
desktop incarnation after reboot: missing/stale required bindings block recovery
for inspection and the existing explicit recovery/abandonment procedures.
A new desktop and new requests can be admitted only after old ownership and
cleanup have been resolved. Existing persistent application preparation and
rootfs upgrade gates are not bypassed.

## Response schemas

Python runtime/request/result returns remain their existing dataclasses. Runtime
references add nullable `workspace_binding`; request/result records add nullable
`workspace_id`. Old records load with `None`. Binding fields are:

```
schema, workspace_id, number, network, revision, incarnation, role,
boot_id, desktop_runtime_id, display, namespace_path,
namespace_device, namespace_inode, x11_directory, access_directory,
vnc_endpoint
```

Workspace APIs return JSON-compatible dictionaries:

| Method | Response fields |
| --- | --- |
| capabilities | `schema`, `supported`, `networks`, `desktop_transport`, `parameters`, `arbitrary_parameters`, `live_acceptance`, `cancellation_response`, `launch_preconditions` |
| register/change/delete | `schema`, `workspace_id`, `number`, `network`, `revision`, `incarnation`, `state`, `created_at` |
| membership | `schema`, `workspace_id`, `complete`, `items`, `next`, `total` |
| status | registration fields plus `membership`, `observed_at`, `readiness='not-probed'` |
| desktop access | `schema`, `workspace_id`, `desktop_runtime_id`, `display`, `vnc_endpoint`, `ready`, `advisory`, `observed_at` |
| catalogue | `schema`, `items`, `next`, `advisory`; each item: `name`, `description`, `role`, `eligible`, `reason` |

`networks` contains `{id:'host-shared', isolation:'none',
sharing:'all-workspaces-and-host', owner:'host-init'}`.
`vnc_endpoint` is `{kind:'unix', path:string, browser_direct:false}`.
`live_acceptance:false` deliberately advertises this pass's uncompleted live gate.

Membership items are discriminated by `kind`:

- runtime: `key`, `runtime_id`, `application`, `state`, `cleanup_pending`,
  `role`, `desktop_runtime_id`;
- operation: `key`, `operation_id`, `request_id`, `state`, `runtime_id`;
- request: `key`, `request_id`, `application`, `state='queued'`.

Use `next` as the next call's `after` cursor. `complete:true` means all ownership
sources were inspected, not that one page contains all members. `total` is the
whole snapshot count; pages are not a transaction across calls. Page size is
1–100. Reads fail `inspection-busy` instead of waiting behind mutations.
Inspection caps queue/operation files at 512 combined and their aggregate bytes
plus runtime references at 8 MiB; exceeding bounds fails `inspection-capacity`,
never a misleading empty membership. Catalogue source count/bytes are bounded
similarly. Status/membership do not reconcile or mutate state. Desktop probing
releases the shared snapshot lock before contacting root-control.

Cancellation retains the **structured dict** contract from pass 17:
`{request_id, application, runtime_id, operation_id, status}`. Status may be
`cancelled`, `accepted`, `pending`, `uncertain`, `failed`, or `abandoned`.
Station-access must not treat this as the older runtime-or-None API. Missing or
expired evidence raises an error. `pending`/`uncertain` is not successful
cancellation and must not cause an automatic replacement request.

Workspace-specific failures raise `WorkspaceError`; `.code` is machine readable
and `.response()` returns
`{schema:'zog-workspace-v1', ok:false, reason:{code, message}}`.
Examples include `unsupported-network`, `unsupported-parameters`,
`application-ineligible`, `desktop-not-ready`, `workspace-in-use`,
`workspace-deleted`, `workspace-mismatch`, `cross-workspace-singleton`,
`workspace-binding-stale`, `inspection-busy`, and `inspection-capacity`.
Existing persistence/recovery/lock exceptions retain their existing semantics.
Preflight retains its advisory schema and adds workspace eligibility/readiness
checks; it never prepares an image or starts a desktop.

## Acceptance and integration checklist

Fixture coverage includes duplicate/concurrent launches, changed intent, lost
replies, old-record decoding, reboot uncertainty, cancellation, deletion/network
change races, cleanup protection, read-only pagination, cross-workspace singleton
conflicts and X11 authentication protocol success/rejection.

No actual systemd, namespace attachment, X server, TurboVNC, browser or EC2
acceptance was performed for this pass. One Unix socket fixture is skipped where
the execution environment prohibits AF_UNIX sockets. Synthetic protocol tests
are not substitutes for this live gate.

Before enabling launch, station-access must adapt selectors and cancellation,
register existing workspace UUIDs/numbers, use authoritative membership, configure
a desktop recipe/account and proxy access, and pass a two-workspace live test:
launch desktops and clients; verify GUI/log access and invocation IDs; stop one
client without affecting peers; reject desktop teardown/network changes while
owned; exercise lost replies and reboot recovery; verify retained preparation
and persistent data. Treat the host-shared network as a deliberate deployment
choice. Isolated networks require a subsequent contract extension.

## Pass 18 revision 1 — mount payload correction

The original pass encoded the client `BindReadOnlyPaths` property incorrectly
as an empty array. Revision 1 supplies the flattened arguments expected by the
property encoder. A regression inspects the typed `StartTransientUnit` payload,
including both read-only mounts and exclusion from writable mounts. The API and
`zog-workspace-v1` capability version are unchanged. This correction is required
before workspace client acceptance; it does not itself constitute live acceptance.
