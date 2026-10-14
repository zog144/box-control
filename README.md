# box-control

**Documentation:** [Technical manual](docs/box-control/README.md) · [Source traceability](docs/box-control/TRACEABILITY.md). The manual organizes current concepts; linked detailed sources preserve exact contracts and historical evidence.

Box-control is Zog's single-shot application evaluator and Python control library.
It reads application declarations, selects an immutable root filesystem built by
image-build, and manages application runtimes through the privileged root-control
daemon. Station-access uses the same library for browser-facing controls and logs.

An **application** defines one or more commands. Each **program runtime** is a
systemd transient service; an **application runtime** groups those services under
its own child slice beneath the Zog slice. Services use `Type=exec`,
`ExitType=cgroup`, `Restart=no` and journald. Descendants remain part of the service
until its cgroup is empty. Box-control does not build images or supervise the
application's internal business logic.

## Source layout and prerequisites

The installable package is in `box-control/`, not the repository root.

| Path | Purpose |
| --- | --- |
| `box-control/src/zog/box_control/` | Unprivileged evaluator, public API, declarations and durable state |
| `box-control/tests/` | Unit, fault-injection and opt-in live tests |
| `box-control/catalogue/` | Detailed controller and VM-testing documentation |
| `acceptance/` | Disposable-host acceptance scripts and historical evidence |
| `docs/box-control/PERSISTENT-APPLICATIONS.md` | Preparation, upgrades, storage deletion and cancellation contract |

Use Linux, Python 3.11+, `dbus-next==0.2.3`, and a compatible image-build checkout.
Real execution needs a systemd host supporting `ExitType=cgroup` (systemd 250+),
the system bus, `busctl`, `journalctl`, root-control running as root, and an
unprivileged caller allowed to access its socket. Unit tests do not require a
running systemd manager. Deploy matching box-control and root-control code.

## Install the pinned companion revisions

For standalone public-source validation, install box-control without trying
to acquire its not-yet-public root-control and image-build companions:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install 'dbus-next==0.2.3' 'pytest>=8,<10'
python -m pip install --no-deps -e './box-control'
python -m pytest -q box-control/tests/test_namespace_migration.py
```

Historical private companion pins remain separate provenance records. For full-stack acceptance, use newly approved public source identities once all companion components have been migrated. Root-control now lives in the separate `zog144/root-control` repository. Tests use installed companion packages; no legacy bundled privileged package is retained.

## Configure and evaluate a project

A deployment project is separate from the source checkout. It contains an
`evaluate` entry point, `package/`, `application/`, optional `catalogue/`, and
persisted `state/`. Only image-build reads package build metadata. Box-control
reads `application/<name>/application.py`. State-changing operations require the
project to be beneath the caller's home directory.

For example, an application file can contain:

```python
application(
    name="example",
    start_policy="externally-controlled",
    programs=(program(name="worker", command=("/usr/bin/example-worker",)),),
)
```

The executable must exist in the selected image; this example does not install
it. Declarations use the parser's `application(...)` and `program(...)` syntax,
not arbitrary imported Python. The executable `box-control/evaluate` can be
copied into a deployment project. Run `box-control` or `python -m zog.box_control`
from that project's root to perform one evaluation; exit status is 0 on success
and 1 on a failed report. The CLI has no subcommand-based lifecycle interface.

Start `root-control` as a separately supervised privileged daemon. Its default
socket is `/run/zog/root-control.sock`, created with mode 0660; the deployment
must arrange the parent directory and socket group access. `ROOT_CONTROL_SOCKET`
changes the daemon path; clients using another path must explicitly construct
`RootControlSystemdTransport(socket_path=...)`. Installing the package does not
install or configure a production system service.

New launches consume `state/image-build/active` and validated generations under
`state/image-build/generations/`. A missing or invalid image blocks launch. There
is no fallback to the host root or on-demand image construction.

## Python control interface

```python
from pathlib import Path
from zog.box_control import BoxControl, Project

control = BoxControl(Project(Path.home() / "zog-project"))
runtime = control.launch_application("example", request_id="example-launch-1")
control.application_runtime(runtime.runtime_id)
control.terminate_application_runtime(runtime.runtime_id)
control.application_runtime_history("example", limit=5)
```

`restart_application_runtime(runtime_id)` replaces that exact runtime with a new
identity. `current_application_runtimes(name)` lists current instances.
`request_application_launch(name, request_id=...)` queues a durable request for a
later evaluation; `application_request_result(request_id)` reads its outcome.
Request IDs use letters, digits and hyphens, with a 48-hour deduplication window.
Use immutable runtime IDs for restart/termination instead of guessing the latest
instance from its display name.

Start policies are `externally-controlled`, `start-once-per-boot`, and
`keep-running`. Evaluation is one pass: arrange subsequent evaluations yourself.
Application dependencies establish validation/evaluation order, not readiness,
recursive startup or cascading stops. Non-multiple-instance applications replace
the prior instance on launch; multiple instances receive distinct identities.

The API also exposes runtime inspection, `application_logs`, `list_build_jobs`,
`inspect_build_job`, and `build_job_logs`. Logs are invocation-scoped journal
pages with opaque cursors, subject to journald retention. These local APIs are
trusted interfaces; station-access owns authorization at its network boundary.

## State, persistence and recovery

| State | Meaning |
| --- | --- |
| Current desired state | What the application declaration requests now |
| Expected launched state | Configuration actually bound and persisted at launch |
| Observed state | What systemd reports for that runtime |

Observed differences from the expected launched configuration are integrity
faults. A changed declaration is legitimate configuration drift and does not by
itself restart the running application.

Runtime references live in `state/runtime/references.json`; queued requests and
results live in `state/application-request/` and
`state/application-request-result/`. Keep the whole controller state together:
prepared operations bind the exact configuration, generation and identities
before lifecycle changes. Uncertain starts or storage faults can block mutations
while inspection remains available. Do not delete state to clear a recovery block.

Nonpersistent writable mounts are private to each runtime under
`state/mounts/<application>/<runtime-id>/<program>/<mount-name>`. Persistent
applications declare `persistent=True`, named `writable_mounts`, a preparation
revision and finite preparation commands. They share application-owned storage
across services and replacement runtimes, while every launch still gets a new
runtime ID. Explicit preparation is required before launch; a changed rootfs
requires an explicit upgrade. Stop and history pruning do not delete persistent
data. See [persistent applications](docs/box-control/PERSISTENT-APPLICATIONS.md) for complete
examples and the identity-checked deletion API.

Completed runtime history defaults to 25 entries, including successful and failed
launches. Pending cleanup protects its runtime and image inputs. Journald and core
dump retention are separate from controller history.

## Testing and current limits

`python -m pytest` runs the default local suite. Live systemd and reboot tests
are opt-in and require a disposable host; see
[VM testing](docs/box-control/catalogue/systemd-vm-testing.md) and
[acceptance instructions](acceptance/README.md). Passing transport fixtures does
not establish live-host compatibility.

Persistent preparation/storage RPCs still need their live acceptance gate.
Persistent multiple instances, automatic migration of old writable data, and
mount-name changes are not supported. Unfinished preparation conservatively
blocks ordinary project mutations. Preparation recipes own database/virtual
environment upgrades; a failed migration is not automatically rolled back.

Legacy `state/rootfs` remains readable for old runtime recovery. Reclamation of
new image-build generations is deferred pending a shared retention contract;
capacity limits still apply. Preserve active and unfinished build inputs.
Historical `HANDOFF-*.md` files record earlier passes and are not installation
instructions for the current tree.

### Workspace applications (pass 18)

The `zog-workspace-v1` controller contract adds durable workspace attachment,
controller-resolved desktop access, authoritative membership, and safe teardown.
See [WORKSPACE-APPLICATIONS.md](docs/box-control/WORKSPACE-APPLICATIONS.md) for APIs, station-access
migration, explicit host-shared networking, and outstanding live acceptance.

## Collaboration

Zog is developed by its maintainers in collaboration with ChatGPT, an AI system
from OpenAI. This collaboration includes architecture, implementation, testing,
documentation and code review.

### Application software and data mounts

[APPLICATION-MOUNTS.md](docs/box-control/APPLICATION-MOUNTS.md) documents `zog-mounts-v1`, durable
mount inventory, protected software paths, read-only inspection and the explicit
transition from writable Python environments to image-build packaged software.

## Restricted host-discover inspection gateway

`zog.box_control.host_gateway` supplies a read-only v1 receiver for trusted service
composition. Supply an existing BoxControl object and connected Unix socket to
`serve_one`; it requires server UID 971 and kernel peer UID 970. Requests can only
inspect an immutable runtime UUID, returning persisted state and cleanup status.
They cannot select a project, invoke a shell, launch/stop runtimes or call systemd.
No listener or service is automatically installed. Production host-discover
transport remains disabled; service composition and live acceptance are pending.


Historical operational evidence remains in maintainers' restricted records; no private evidence archive is included in this source.

Optional build-job journal barrier in the separately maintained root-control documentation: unprivileged exit hook and dedicated host synchronization broker. Disabled by default; live acceptance remains pending.

## Public licensing and provenance

First-party source and documentation use `BSD-3-Clause OR GPL-3.0-only`;
see [LICENSE](LICENSE), [LICENSES](LICENSES/) and
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). This public Git history
omits prior private commits, tags and handoffs. The source-level public CI
is distinct from root-control, image-build and live systemd acceptance.
