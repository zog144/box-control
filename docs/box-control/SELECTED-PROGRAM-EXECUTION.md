# Durable exact single-program application execution (v1)

**Owner:** box-control and root-control  
**Date:** 2026-10-14  
**Status:** source implementation on `unstable`; native privileged systemd/Certbot acceptance pending.

## Purpose

Zog applications can declare several named programs. Ordinary application launch
still starts all declared programs, as before. A managed persistent singleton
with `start_policy="externally-controlled"` can now explicitly execute **one**
named program without creating a separate Zog application, UID, storage namespace
or arbitrary-command API. The initial production consumer is
`certify-manage` (`issue`, `renew`, `inspect`). This is **not** a renewal
scheduler; an authorized caller selects the command and supplies or obtains a
durable request identity.

```python
request_id = control.issue_application_request_id()
runtime = control.launch_application(
    "certify-manage", program_name="renew", request_id=request_id,
)
# Or persist intent and let the existing evaluator process it:
request = control.request_application_launch(
    "certify-manage", program_name="renew", request_id=request_id,
)
# Caller reconciles with:
result = control.application_request_result(request_id)
```

Use either direct execution or the queued evaluator path, **not both** for the
same new intent. Reuse the *same request ID* when resolving a lost reply. Never
switch `program_name` while retaining the old ID. Request identity retention
is bounded by the established 48-hour window.

A direct selected launch requires an explicit request ID. A queued launch may
issue its ID when omitted; the caller must durably retain the returned ID before
relying on execution or retry behavior.

## Controller admission and durable identity

Only a declared program name can be selected, with no caller-controlled command
path, arguments, UID/GID, mount, software view, execution environment or
storage override. The application must be `persistent=True`,
`multiple_instances=False`, `execution_identity="application"`,
`start_policy="externally-controlled"`, and not workspace-attached.
The selected command gets the **same** stable issued application execution
grant, prepared storage ID, original rootfs generation and immutable software
binding as every other operation for that application.

The controller first binds and validates **the complete application**
against its prepared storage and published software view. Only afterward does
it narrow the effective runtime snapshot to exactly one `ProgramSpec`.
The saved authoritative operation includes
`selected_program`, one selected command and its environment, request ID,
execution grant, prepared software view, and one runtime identity. Durable
runtime references and request results also carry the selector. Crash recovery
executes the snapshotted program, not newly edited application.py.

For `certify-manage`, even a declaration containing only one lifecycle
program requires an explicit selector. The previous protective parser ban on
multi-program certificate declarations has been replaced with this stricter
**execution-time** selection requirement. Certificate preparation steps remain
their own approved preparation workflow and are not automatically issued or
renewed as part of normal application launch.

An unresolved prior runtime belonging to the same singleton blocks any newly
selected program. A selected request **never replaces** a running/uncertain
operation to take its place. Queued requests remain pending; an explicit direct
attempt reports the pending condition. Once the old runtime is conclusively
terminal with cleanup complete, a new separately authorized request may proceed.
The ordinary multi-program application, restart, role and workspace lifecycle
contracts remain unchanged. Restarting a selected one-shot runtime by its runtime
identity is rejected; a genuinely new invocation needs a new exact-program
request and its own durable operation.

### Recovery and evidence

Before starting a selected service, the authoritative operation records the
unit start intent; an ambiguous systemd reply must be resolved from invocation
and operation evidence, not retried blindly. Pending/starting/terminating or
failed durability states block new lifecycle mutations as usual. Completed
one-shot successes and failures enter the shared application runtime history.

Root-control does **not** trust a caller-supplied `selected_program` label alone.
At its privileged systemd boundary, it checks the exact saved operation
(`launching`, one attempted unit, original request ID, selected program and
single-command snapshot), the matching queued/direct request, and the immutable
saved runtime reference's service unit, command and grant. It also checks
effective command, environment, working directory, numeric User/Group, timeout,
storage ID and execution grant against the saved snapshot before starting
systemd. For the certificate `issue`, `renew`, `inspect` lifecycle programs,
a missing selector is explicitly rejected. No arbitrary command-execution
endpoint is introduced into root-control.

The ordinary systemd observation and credential/mount inspectors remain the
authoritative **runtime** evidence surfaces; a successful request is not
sufficient proof of ACME issuance, certificate readiness, key publication or
consumer activation.

## Consumer follow-up

The certify-manage owner should retain the declarative three-program source
and use this program selector from an authorized caller, without manually
creating separate accounts or multiple application storage IDs. Host-deploy
must own and approve the automated renewal caller, its timer cadence and the
only-publish-validated-certificate transaction. The caller should inspect the
previous request and Certbot's saved operation/lineage status before scheduling
a new renewal; a systemd successful exit alone cannot prove TLS readiness.

The immutable image must actually contain the reviewed CLI/Cerbot/cloud plugin.
The v1 controller implementation does **not** enable a systemd timer, ACME
challenge routing, certificate publication, nginx reload, or a live host cutover.

Production acceptance must still cover the exact root-control mechanism on a
native systemd host, private certificate state ownership/mounts, missing/revoked
policy, same-UID serialization, interrupted launch, lost reply, real reboot,
immutable image upgrade and authentic Certbot `issue`, `renew`, `inspect`
commands. Keep production issuance **NO-GO** until those gates pass.
