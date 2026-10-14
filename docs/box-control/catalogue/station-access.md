# Station-access integration contract — pass 10

Box-control remains a synchronous, single-shot Python library. Station-access
owns HTTP authentication, application/runtime ownership checks, and browser
polling. No scheduler, queue worker, WebSocket service, or user database is added.
For development from **private component source checkouts**, select the
matching image-build and box-control commits through the reviewed source
manifest and verify their declared companion dependencies. A
`version-share-handoff.json` in a trusted private checkout may describe
those exact revisions; it is not itself a public installation source and is
not included as an implicit file in the aggregate distribution. For the
public interface and supported composition, use the
[interfaces and integration chapter](../guide/interfaces-and-integration.md)
and the [finite build-job contract](build-jobs.md). The aggregate Zog
README does not replace private developer checkout setup instructions.
Installed public `zog` package compatibility requires its separately
approved release source selection.

## Direct controls

Save a request ID from `issue_application_request_id()` in station-access before
calling `execute_application_operation(operation, target, request_id=...)`.
Operations are `launch` (application name), `restart` and `terminate` (immutable
runtime ID). Reuse the same ID after a lost response. Changing intent under an
existing ID fails. Existing 48-hour acceptance/replay rules still apply.
The direct `terminate_application_runtime` now accepts `request_id`, like launch
and restart. Existing methods remain supported.

The JSON-safe response contains operation, target, request_id, status, runtime,
and fault. Status is completed, pending, busy, blocked, or failed. Completed
includes the recorded runtime. Pending requires later recovery/evaluation; polling
alone never advances it. Busy means the lock was not acquired. Blocked reports a
storage/recovery fault. Failed reports an unsuccessful call, not proof that all
external effects were undone. Inspect authoritative request/operation records
before deciding to issue a different request.

The default mutation-lock wait is five seconds. Configure
`BoxControl(..., mutation_lock_timeout_seconds=...)` (zero for immediate busy,
None for the legacy unbounded lock wait). This is a lock-acquisition bound, not
an overall bound on recovery plus lifecycle execution. Run synchronous calls in
a bounded web worker/executor rather than an async event loop. A disconnected
browser does not cancel a durable operation. The existing transport budgets now
use context-local state, preventing unrelated threads from sharing deadlines.

## Fresh observation

`observe_application_runtime(runtime_id)` reads the saved expected configuration
and observes each program without reconciliation, locks, cleanup, or writes.
It reports a timestamp and an unlocked snapshot, persisted runtime state and
cleanup flag, and per-program observed, absent, previous-boot,
identity-unavailable, unavailable, or integrity-fault status. Integrity checks
compare the saved invocation and effective configuration against systemd.
Absence is not interpreted as successful execution. Unavailability is not absence.
The production transport shares a five-second observation budget across members.
Snapshots may race with lifecycle changes and are not an atomic project view.

## Authoritative runtime mount observation

`observe_application_runtime_mounts(runtime_id)` is the live kernel counterpart
to `application_runtime_mounts(runtime_id)`. The expected-inventory API remains
historical launch evidence and never probes the host. The observation API accepts
only the immutable runtime ID; callers cannot supply a PID, unit name, cgroup,
mount destination, source path, or `/proc` path.

Root-control resolves the persisted runtime/program records and, independently for
each transient program service, verifies the saved boot ID, transient unit identity,
Invocation ID and cgroup before inspecting processes. Process identity is PID plus
kernel start-time ticks and mount-namespace identity. Every representative process
and the systemd unit are rechecked after reading. A disappearing/reused PID,
namespace transition, cgroup migration, or unit transition yields
`changed-during-read`; root-control never silently switches to a different process.

The stable public schema is `zog-runtime-mount-observation-v1` with
`basis=kernel-mount-namespace` and `snapshot=non-atomic`. Each program reports a
status, one or more mount namespaces, bounded PID/start-time evidence, expected mount
destinations and observed presence/access/backing evidence. Services whose processes
occupy multiple mount namespaces report all bounded namespaces explicitly rather
than selecting one as representative.

Expected kernel mounts come from the persisted expected-launched systemd properties,
not the current application specification. This includes the runtime root,
read-only rootfs projection, immutable `/applications/.store/<artifact>` mounts,
the composed `/applications/<application>` view, writable application-data mounts,
and applicable workspace mounts. Symlink-composed files inside an application view
are not treated as kernel mounts.

For each expected bind source, root-control compares the source filesystem
device/inode identity with the object visible at that destination through the
verified process's `/proc/<pid>/root`. Public results expose only
`verified`, `mismatch`, or `inconclusive`; host source paths and arbitrary
mountinfo source strings are not returned. A correct destination alone is therefore
not reported as verified backing. Any otherwise-matching observation with
inconclusive backing is `partial`, not `observed`; a known backing mismatch is
an integrity fault.

Program statuses include `observed`, `partial`, `absent`,
`previous-boot`, `identity-unavailable`, `changed-during-read`,
`integrity-fault`, `unavailable`, and `legacy-unrecorded`.
Malformed/truncated mountinfo, read deadlines and capacity limits remain explicit;
a truncated read makes unseen expected mounts unknown rather than falsely missing.
Incorrect read-only/read-write flags, missing expected mounts after a complete read,
backing-identity mismatches and Invocation-ID mismatches are integrity faults.
Observation never repairs, restarts, reconciles, mounts or unmounts anything.

The operation runs on root-control's bounded read-worker lane and has a shared
five-second privileged inspection deadline. Station-access should refresh it
separately from historical runtime/software evidence and render expected and observed
values side by side. A stopped runtime continues to retain its expected launch-time
software identity even when live mount observation reports absent or previous-boot.
Do not persist these rapidly changing observations in the station-access database.

## Short polling logs

`application_logs(runtime_id, program, cursor=None, limit=50)` returns one page
for one program's saved invocation. The first read returns the latest entries;
subsequent reads pass back `next_cursor` and read forward. Poll about once per
second while visible, back off on unavailable responses, and use `has_more` to
drain a backlog with bounded requests. Do not create a new request ID to read logs.
An application-wide view can merge separately polled program streams, retaining
one cursor per program. Cross-program ordering is only timestamp based.

Root-control resolves runtime/program IDs from persisted references. Callers
cannot supply a unit name, path to a journal, or arbitrary journal expression.
Both saved boot and invocation identity filter every query. Cursor scope binds
the canonical project, runtime, program, boot and invocation; a cursor is opaque
continuation data, not an authorization token. Restart selects a new stream;
opening the old runtime continues to select its historical stream.

Pages contain entries (timestamp_microseconds, priority, message,
message_unavailable), next_cursor, has_more, and status:

- ok: a valid page, possibly no new entries.
- empty-history: no matching initial entries; never claims to distinguish no
  output from expired journal retention.
- identity-unavailable: the runtime never recorded sufficient execution identity.
- cursor-unavailable: the anchor disappeared; show a gap and explicitly start a
  fresh tail if desired. Never silently skip that gap.
- unavailable: the reader failed, timed out, or exceeded its byte budget; preserve
  the cursor and retry/back off. Reduce the limit for byte-budget failures.

Unknown/pruned runtime IDs, invalid programs, invalid limits, and wrong-stream
cursors are errors. Limits are 1–100 entries, 256 KiB subprocess output per query,
a shared five-second reader deadline and a ten-second log transport budget.
Oversized MESSAGE fields may be omitted by journalctl and are marked unavailable;
this API is a bounded viewer, not lossless log export. Journald retention remains
independent. Library runtime-history pruning does not delete journal data, but
once the runtime record is pruned this identity-based endpoint cannot resolve it.
Root-control serializes lifecycle/systemd RPCs, with at most eight outstanding
requests in that lane. Two dedicated journal-reader slots bypass lifecycle work.
Additional journal requests receive a busy transport error: catch RuntimeOperationError
and back off without changing the cursor. Fresh systemd observations still use
the serial lane and can time out during lifecycle work. Request decoding retains
a ten-second bound on the trusted local socket; readers do not share systemd
backend state. No log poll acquires the project mutation lock.

## Authorization and diagnostics

Before each control, observation, or log request, station-access resolves the
configured project and checks the authenticated user's permitted application or
runtime. Never accept arbitrary project paths from HTTP. Recheck access on every
poll. Render log messages as text, never HTML. Keep root-control's Unix socket
restricted to the trusted service account. This pass does not implement or test
station-access's multi-user authorization layer.

Control responses carry request-correlated faults and cause chains, now including
bounded traceback frame locations without local variable snapshots. The
`zog.box_control.operations` logger emits a `zog_event` structured response field;
station-access configures its handler/formatter and access policy. Root logging
is not reconfigured. These diagnostic events are not authoritative recovery
records, and exception messages/log output can contain application-sensitive data.
Ordinary application stdout/stderr remains in its own journal invocation stream.

## Verification

Local tests cover duplicate controls, termination retries, contention, read-only
observation and integrity faults, cross-thread budgets, cursor scope/gaps,
output/time limits, and structured trace frames. The opt-in
`test_live_station_log_cursor_and_invocation_isolation` exercises the real journal
and root-control socket on the existing disposable-VM fixture. It is not a
station-access browser test. These results exercise controller-side
behavior and (when the opt-in fixture is run) a disposable host journal;
they do **not** establish station-access authorization, frontend behavior,
production deployment, cross-program version compatibility or live
application acceptance. The historical private handoff records dated
test results only; the maintained public [verification chapter](../guide/verification.md)
defines current evidence tiers and outstanding acceptance.
