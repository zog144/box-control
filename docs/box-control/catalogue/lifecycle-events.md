# Correlated lifecycle diagnostics

`zog.box_control.lifecycle` emits structured values in each log record's `zog_event`
attribute. Install `zog.box_control.events.EventFormatter` on the embedding service's
handler to serialize that value as JSON (for example to stdout captured by
station-access's systemd journal). The library does not install handlers or
change root logging. Set the logger/handler level to INFO to receive events.

Events include timestamp and, when available, operation_id, request_id, operation,
runtime_id/runtime_ids, program, unit_name and invocation_id. Unit events may
precede discovery of the invocation identity. IDs refer to authoritative records;
program command lines and environment contents are not copied into these events.
Fault cause messages and source locations can still contain sensitive information,
so station-access must restrict access just as it does for application logs.

Event families:

- operation.recorded: a successful synchronized operation-record write, including
  phase (prepared/stopping/launching/committed/failed/abandoned) and publication /
  finished flags. Repeated writes are expected; this is not exactly-once delivery.
- operation.started / recovery.started: beginning execution or resumption.
- operation.returned / recovery.returned: control returned, with actual phase and
  finished/publication flags. Returned is not an assertion of successful launch.
- service.start.requested / acknowledged: saved start intent followed by the
  transport action and its reply. Acknowledged is not overall application commit.
- unit.kill / stop / reset_failed / release requested and acknowledged events:
  per-unit mechanism actions, correlated with their owning program when known.
- runtime.cleanup / terminate started, returned and interrupted events: cleanup
  progress remains separate from the runtime outcome. Returned includes
  cleanup_pending; interrupted includes structured fault causes and frame locations.
- operation.interrupted: execution raised; recovery-critical state remains governed
  by the existing persistence/interlock rules.

Events are best-effort diagnostics. They neither replace filesystem records nor
participate in recovery decisions. A failed logging handler cannot stop an action
or erase a successful result. Crashes can lose events between durable transitions
and emission. Journald independently controls event retention. These controller
events belong to the embedding service's log stream, not to the application's
stdout/stderr stream returned by application_logs.

## Read responsiveness

The pre-change EC2 test measured a 9.53-second journal poll behind a stop job.
Root-control now has one serialized mechanism executor and a separate executor
with two journal-reader slots. Each lane has bounded admission (eight mechanism
requests, two journal requests including active work); excess requests return a
busy error rather than accumulating indefinitely. Journal reads only access
atomic reference snapshots and their own bounded journalctl processes. The
mutable systemd backend and job connection remain confined to the serial lane.

This does not make every inspection concurrent. systemd observations remain in
the serial lane, and the socket's bounded request-decoding step is still shared.
See validation-summary.json (retained historical verification; not distributed with the public manual) for the measured post-change result.
