# Build registration progress and reply timeouts

`RootControlReplyTimeout` distinguishes a request successfully sent but not
answered within its deadline from a connect failure. It carries `operation` and
`elapsed_seconds`; the message states that the outcome may be unresolved.
Send failures also report uncertainty. Callers must not blindly retry mutations.

`BoxControl.inspect_build_root(resource_id)` reads the privileged registration
snapshot without mutation/recovery, even when the caller retains a mutation
marker. It validates the returned identity and intent against the local record.
Root-control routes `build_registration_status` on its independent inspection
lane. This method never dispatches work, changes ownership, marks a root ready,
or clears a guard. It uses the same trusted-local socket boundary as existing
controller operations.

Registration persists phase, start/update timestamps and elapsed seconds through
validate, copy-root, sync-root, sync-source, sync-output and ready. Interrupted
imports remain importing at their last durable phase. A phase snapshot alone
is not a claim that a worker is alive. Existing registrations without phase
fields remain readable. Exact replay of ready registration returns its previous
record without recopying; mismatched intent remains rejected.

Structured events go to daemon stderr for journald: registration start (including
queue delay), phase, finish (success/failure and duration), and undelivered reply.
No command, environment or source contents are logged by these events.

The import remains serialized and synchronous in the mutation worker. Consumers
can observe it after a reply timeout and reconcile the same identity after it
becomes ready. They must bound polling and preserve unresolved resources. A
future asynchronous import API could remove the initial wait; it is not part
of this interface. Do not remove fsync or integrity checks to avoid a timeout.
