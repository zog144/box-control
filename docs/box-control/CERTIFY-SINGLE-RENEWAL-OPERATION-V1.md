# Selected certificate renewal: exact operation and evidence v1

**Producer contract:** [certify-manage single-operation renewal](https://github.com/zog144/certify-manage/blob/c8760c71d5af297e2f592c891805fc51f803ea64/docs/single-renewal-operation-v1.md).
**Owners:** box-control/root-control selected-program admission and recovery,
certify-manage issuer verification, host-deploy caller scheduling and delivery.

This is a **source-only** contract, not approval for an unattended timer or
production ACME renewal.

1. The owner durably retains the one selected `certify-manage/renew`
   controller `request_id` **before** contacting the issuer lifecycle broker.
   The broker admits exactly `renew`, `inspect`, or `stage`, with no
   untrusted command, policy, state, runtime, UID, or software override.
   Only the exact host-owner-issued application grant, prepared storage,
   published immutable image and root-approved policy mount may run.
2. Root-control checks the selected program and immutable saved operation,
   request, unit, command, User/Group, working directory, environment and
   storage identity at the privileged systemd boundary. Runtime creation
   returns a request-bound selected `renew` runtime, not permission to
   execute any other declared program.
3. A lost reply reuses the **same** request ID, and controller recovery
   executes the immutable operation snapshot. A request ID already attached
   to another program is rejected. A pending/uncertain singleton blocks
   a *new* request. Controller records are retained for 48 hours;
   missing evidence after expiration is a hold, not a retry license.
4. Broker `result` exposes only `{request_id, program, status, runtime_id}`.
   Status is `unknown`, `pending`, `satisfied`, `failed`,
   `abandoned` or `cancelled`. Unknown is **absence** of result, not
   proof that an ACME operation did not begin. Neither `satisfied` nor
   systemd exit zero proves a renewed certificate; error text and
   private secrets must not flow into this result.
5. A separate selected `inspect` request, proven using the exact
   InvocationID, boot, issuer UID, grant, storage and journald output,
   supplies the secret-free issuer result. Host-deploy must compare
   certificate ID, SAN set, leaf fingerprint, policy binding, issuer
   operation/outcome and update time against the original renewal
   authorization/observation. Inspection freshness is limited to
   15 minutes. `unchanged` must not trigger unnecessary deployment;
   `renewed` merely permits a separately authorized `stage`.
   `tls_ready` is always false at this layer.
6. Failed or uncertain Certbot execution, vanished/malformed results,
   changed credentials/account, failed final journal write and ambiguous
   controller recovery block another mutation until reviewed. Explicit
   issuer `recover` is a **separate administrator action** and only
   yields `recovered-unverified`; it cannot satisfy the original renewal.

**Source tests:** `test_issuer_lifecycle_broker.py`,
`test_selected_program_execution.py`, `test_selected_program_recovery.py`,
and certify-manage `test_single_operation_renewal.py`. Native systemd,
real Certbot/Cloudflare, reboot, HTTPS publication and TLS verification remain
independent acceptance gates.
