# Acceptance payloads and historical evidence

The September 14–19 archives were compacted and removed from the working tree.
See the dated private acceptance summary and recovery manifest for results, limitations, hashes and historical archive links.
For a new run, package the current source outside the checkout, with no enclosing directory.
An exact historical rerun requires the matching historical recipe and payload; it must not silently use current sources.

## Historical host-deploy 0.5 reboot workflow

Use an independently initialized host-deploy workspace and its own disposable
Amazon Linux test instance. Provision and start it with host-deploy first.
The following illustrates the historical workflow. Supply an explicitly prepared
external payload and a matching recipe; no tested-source ZIP is bundled here:

```sh
host-deploy reboot-submit --workspace /path/to/workspace --name box-control-reboot \
  --source /work/acceptance-source.zip --recipe acceptance/reboot-recipe.json
host-deploy reboot-resume --workspace /path/to/workspace --name box-control-reboot --wait-seconds 900
host-deploy reboot-status --workspace /path/to/workspace --name box-control-reboot
host-deploy reboot-collect --workspace /path/to/workspace --name box-control-reboot --output /work/acceptance-results.tar.gz
```

Only observed.phase=succeeded is success; a resume polling timeout is not a test
result. Collect evidence before terminating the dedicated instance with its exact
instance ID. The adapter leaves services running where required by assertions;
host disposal is the final cleanup step. The payload has no enclosing archive
directory, as required for recipe paths to resolve. Credentials are never part of
this payload. Host-deploy owns reboot and shutdown protection; do not issue a
separate reboot command from the phase scripts.

## Short-job journal attribution diagnostic

The diagnostic implementation now belongs to `zog144/root-control`. From a matching root-control checkout, run `python3 acceptance/journal_attribution.py --disposable-host --output /var/tmp/journal-attribution.json` on a disposable systemd host with Python 3.11+, dbus-next 0.2.3 and a `nobody`
account. Root is required to create transient services and inspect the system
journal. The payloads run unprivileged. `--concurrency 4` exercises overlapping
jobs; `--retain-after-exit` is a diagnostic control, not a supported repair.

The probe emits unique stdout/stderr markers, including failed exits and child
output, checks actual exit results, and retains the unit until inspection. It
synchronizes journald after execution to distinguish queued output from output
permanently lacking attribution. It then stops, resets and releases its units,
checks unloading, and exits nonzero for incomplete attribution or cleanup.
There is no sleep in the payload. The output report is diagnostic evidence and
must be stored outside the repository. This probes systemd/journald lifecycle
behavior; it does not exercise build-root sandboxing or the public build API.

Production log reads still require both the recorded boot identity and
`_SYSTEMD_INVOCATION_ID`. A matching unit, timestamp, marker, or systemd manager's
`INVOCATION_ID` is not a substitute. `empty-history` does not establish whether
output was absent, expired, delayed, or written without invocation metadata.

On affected systemd versions, retaining a unit with AddRef does not retain all
journald attribution metadata after it becomes inactive. RemainAfterExit does
not cover failed exits. An unprivileged ExecStopPost running journalctl --sync
cannot access the root-only synchronization socket; its apparent improvement
can merely be a timing change. Do not adopt these as proven fixes or inject an
arbitrary delay. A production remedy requires validated lifecycle synchronization
or a supported systemd fix, preserving sandboxing and program exit evidence.

The attribution diagnostic also accepts `--barrier sync` and `--barrier control`.
These install a fixed privileged host-side `ExecStopPostEx` command: respectively
`/usr/bin/journalctl --sync` and `/usr/bin/true`. The payload still runs as nobody.
The control distinguishes synchronization from the timing added by any stop hook.
Both hooks must exit successfully; payload exit codes are checked independently.
Use only on a disposable host. This diagnostic has no application root filesystem
or caller-supplied environment and is **not** a production build-job configuration.
The stop timeout is two seconds. An error or timeout is a failed experiment, not
permission to drop strict boot/invocation matching. Default behavior has no hook.

For a deterministic fault-injection comparison, add `--pause-journal` with serial
execution. Each job briefly suspends the disposable host's journald; an independent
process sends SIGCONT after 0.5 seconds, with an additional resume in probe cleanup.
This is test-only scheduling interference, not a proposed payload delay or fix.
Do not use on a shared or production host. A paused-journal run with the no-op hook
must not be interpreted as equivalent to the real synchronization barrier.


## Immutable application software live acceptance

The current immutable-application-software consumer has a dedicated disposable-host
acceptance payload:

```sh
python3 acceptance/application_software_host.py
```

When used through host-deploy, package the current box-control source together with
matching `zog144/root-control` and `zog144/image-build` dependencies and use
`acceptance/application_software_recipe.json`. The probe is intentionally
opt-in because it starts real transient systemd services and privileged mount
namespaces.

The acceptance launches a two-artifact `application-software-view-v1` through the
real root-control D-Bus backend. The running unprivileged process must be unable to
modify either the composed view or the referenced artifact reached through the view
symlink, while its explicit persistent data bind remains writable. It then stops the
first runtime, publishes/selects a second immutable view, launches a new runtime with
the same storage identity, and requires the saved data to contain both view markers.

The same payload now also exercises the runtime-scaffold composition boundary that
blocked the October 10 station-access preparation retry. An exact top-level writable
mount replaces that one rootfs projection destination rather than being emitted
alongside a conflicting read-only bind. A nested writable mount remains an overlay
beneath its read-only rootfs parent. The acceptance verifies both forms, persistent
state across relaunch, and unchanged generation inventory. Runtime facilities such as
`/run`, `/proc`, `/sys`, `/dev`, and `/applications` are scaffold-owned;
they are never projected from the immutable image. This prevents systemd namespace
setup (including `/run/systemd/incoming`) from writing through a generation bind.

Evidence is written to `evidence/application-software-live.json` plus JUnit XML.
The JSON records both immutable view identities, referenced artifact identities,
the **observed** systemd `BindReadOnlyPaths` values, persisted controller mount
inspection, and the independent
`zog-runtime-mount-observation-v1` kernel-namespace observation while each
runtime is actually running. The live kernel result must verify the read-only view
and artifact backing identities, verify the writable data bind, and report the
stopped first runtime as absent before the replacement is launched. Runtime
identities, persistent data after replacement, OS release and systemd version are
also retained. The test invokes the software reclaimer while the first runtime is
live and requires all protected objects to remain present.

This acceptance does not synthesize a production station-access publication. Final
station-access integration additionally requires an image-build rootfs generation
whose manifest publishes the canonical `application_software_compatibility`
descriptor and a published station-access application-software view.
