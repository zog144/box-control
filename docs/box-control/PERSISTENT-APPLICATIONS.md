# Persistent application environments (pass 17)

Applications remain collections of services with a new immutable runtime ID on
launch/replacement. Persistent data belongs to a separately generated storage ID.
Application `name` is the stable configuration identity; `description` is the
mutable display label. Renaming that configuration identity is not a storage
migration API. There is no automatic adoption of old per-runtime data.

```python
application(
    name="station-access",
    description="Station portal",
    persistent=True,
    multiple_instances=False,
    writable_mounts=("data",),
    preparation_revision="2",
    preparation=(
        program(
            name="prepare",
            command=("/usr/bin/python3", "/opt/station-access/prepare.py"),
            user="regular",
            execution_timeout_seconds=600,
            mounts={"data": "/var/lib/station-access"},
        ),
    ),
    programs=(
        program(
            name="web",
            command=("/usr/bin/python3", "/opt/station-access/web.py"),
            user="regular",
            mounts={"data": "/var/lib/station-access"},
        ),
        program(
            name="scheduler",
            command=("/usr/bin/python3", "/opt/station-access/scheduler.py"),
            user="regular",
            mounts={"data": "/var/lib/station-access"},
        ),
    ),
)
```

This is an interface example, not a supplied station-access installation script.
The preparation executable and native prerequisites, including Python SSL and
SQLite support, must already be in the published image. Preparation may initialize
SQLite and
create application subdirectories. All services/preparation commands use the same
unprivileged host account and group. The first preparation command must mount all
named writable directories; root-control creates these with that account's
ownership. It refuses to repair existing ownership silently. Commands execute
without an implicit shell, in declaration order, with journald output and an
explicit finite runtime limit (1–86400 seconds each). Preparation requires at
least one command; use `/usr/bin/true` when only directory provisioning is needed.

## Lifecycle and upgrades

Call `prepare_application(name)` once, then short-poll
`refresh_application_preparation(name)` until `state` is `ready`, `failed`, or
`uncertain`. A refresh may start the next *unattempted* command after observing
and recording its predecessor's completion and finishing cleanup. Neither launch
nor evaluation starts/retries preparation automatically.

`application_preparation(name)` is read-only, including while mutations are
blocked. Its steps contain program names and runtime IDs; pass these to the
existing `application_logs(runtime_id, program, cursor=..., limit=...)` API.
Preparation runtime references have `purpose="preparation"`, remain inspectable,
and do not count as a normal application instance or a start-once-per-boot launch.
The prepared tree's regular files and directories are synchronized before the
controller durably marks the environment ready. Symlinks are not followed.
Subsequent application data durability remains the application's responsibility.

For a new published rootfs generation, stop the existing application and finish
cleanup, then call `prepare_application(name, upgrade=True)` and poll refresh.
A new generation always requires this operation; application data compatibility
must be checked against the newly packaged software. Changing preparation commands, mounts, or account
configuration also requires a new `preparation_revision`. Mount-name changes are
rejected in this first pass. The preparation recipe owns data migration steps. Python dependencies belong
in the new immutable generation, installed by image-build before publication.

Preparation binds the exact resolved definition, rootfs generation and storage ID.
Changing the active image or application file while preparation runs does not
retarget it. Readiness must match the current launch selection, revision and
preparation signature. Stored data is not recreated if missing. The selected
storage generation remains protected from controller reclamation until explicit
storage deletion or a later upgrade takes its place.

## Failure and cleanup

A failed or interrupted migration does not undo database changes. An absent unit,
changed boot, missing runtime evidence or observation failure cannot prove command
success. No attempted command is replayed automatically. Inspect the preparation
record and logs, repair/restore application data as appropriate, and call
`abandon_application_preparation(name)` to stop an unresolved attempt and drain
cleanup. This retains data and the recorded uncertainty. After inspection, an
explicit `prepare_application(name, upgrade=True, retry=True)` authorizes a new
attempt. Previous attempt summaries and preparation references are retained.
Journal retention remains independent, so references do not guarantee logs exist
forever. Dedicated preparation-history reclamation is deferred.

The initial admission policy is conservative: unfinished/uncertain preparation
blocks ordinary project mutations, including unrelated applications, until
refresh or abandonment resolves it. Inspection/log reads remain available. The
project lock and durable preparation record enforce exclusivity across callers
and controller restarts. Preparation refuses live normal runtimes, pending
cleanup and unfinished lifecycle operations. Storage mutation APIs retain an
interruption marker on unexpected/storage errors; refresh resumes only their
matching preparation marker and never clears an unrelated recovery block.

Stopping/replacing applications and pruning normal runtime history never deletes
persistent storage. Deletion is explicit:
`delete_application_storage(name, storage_id=inspected_storage_id)`.
It requires an idle application and completed preparation cleanup, journals a
`deleting` state, and asks root-control to remove only that owned storage tree.
Retry the same deletion after interruption. The record becomes a tombstone;
subsequent preparation allocates a new storage ID. This action deletes the data,
including any database/archive store; it is not a stop operation or backup.

## Launch cancellation

`cancel_application_launch(application_name, request_id=...)` never invokes
lifecycle recovery. It returns a dictionary with `request_id`, `application`,
`status`, `runtime_id` and `operation_id`:

- `cancelled`: a queued launch was durably withdrawn. A cancellation journal lets
  evaluation finish publication/removal after interruption without launching it.
- `accepted`: committed launch; `runtime_id` is the exact associated runtime.
  Cancellation does not terminate it. Use the normal termination API explicitly.
- `pending` or `uncertain`: an authoritative operation already exists and remains
  unresolved. No cancellation success is claimed and its bindings are unchanged.
- `failed` or `abandoned`: returns the recorded terminal outcome and any runtime ID.

Retries preserve recorded outcomes while evidence is retained. Wrong application,
wrong operation and missing/expired evidence raise an error. The existing 48-hour
request rules apply; callers must not guess a runtime from an application name.
Archive-role removal remains a station-access role/service concern and does not
use whole-application cancellation to stop the portal.

## Validation boundary

This pass has deterministic transport, filesystem, crash-boundary, backend and
API tests. Live systemd acceptance of persistent preparation, RuntimeMaxUSec,
shared ownership and deletion is a separate gate. No compiler host was changed.
Real power-loss validation remains deferred. Deploy matching controller and
root-control versions: the synchronization/deletion RPCs are new in this pass.


## Immutable software transition (pass 19)

Previously documented writable virtual environments are now a legacy layout.
Published generations remain read-only for their entire lifetime: image-build
installs/upgrades software into a new unpublished generation. Preparation runs
packaged commands against writable data; it does not install runtime software.

Existing data is never deleted or moved automatically by this change. Existing
runtime references remain inspectable/stoppable, and old prepared work that no
longer meets policy blocks for explicit recovery rather than being substituted.
For an upgrade, publish an image containing the interpreter/dependencies and all
empty mount-target directories; stop/drain the application; update its commands,
mount destinations and preparation revision; explicitly prepare/upgrade. The
storage ID is retained. Persistent mount-name changes remain unsupported: retain
an old `python` storage object if present, mounting it only for controlled
migration under a permitted data path (for example `/var/lib/legacy-python`).
Do not point normal executable commands or Python import paths at it. New
applications should use the data-only declaration above.

The controller rejects executable entry points inside writable mounts and mounts
covering protected software paths. It cannot infer all behavior of shell commands,
Python import paths, plugins or generated files. Correct software packaging and
migration recipes remain required; this is not a content-classification scanner.
