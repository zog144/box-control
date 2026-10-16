# Split-UID certificate inspection: execution and authentication boundary

**Owner:** box-control / root-control  
**Date:** 2026-10-16  
**Decision:** approve the existing *selected execution* and *controller-journal observation* boundaries for integration testing; specify the missing issuer-owned X.509 inspection boundary below. **Not authorization for unattended production renewal or HTTPS cutover.**

## Three independent principals and paths

1. **host-deploy (scheduler / delivery coordinator)**: a distinct, fixed unprivileged service UID. It retains the original controller-issued request ID durably and decides when a renewal is due. It has **no** controller project-owner identity, issuer-private filesystem permissions, Cloudflare token, or access to the privileged root-control socket.
2. **box-control (controller-owner service)**: the separate approved non-root controller UID. A dedicated, root-installed Unix socket runs `zog.box_control.issuer_lifecycle_broker`. On Linux it checks `SO_PEERCRED` **before parsing** a frame and admits only the fixed host-deploy UID. Its only mutating selectors are `certify-manage/renew` and `certify-manage/stage`; other calls are limited to scoped preparation, request-ID issuance, result, cancellation and sanitized runtime observation. At launch, the existing durable mutation guard, immutable image selection, application execution binding, singleton policy, prepared storage and exact selected program are authoritative. There is **no** general evaluator, shell, arbitrary application, UID, path, root-control or policy selector. Reuse the same signed request ID after a lost reply; a `stage` launch has its **own** request ID.
3. **certify-manage (issuer execution UID/GID)**: the distinct grant-bound application identity owning ACME configuration, keys, certificate lineage and private journal. `renew`, `stage` and any accepted `inspect` execution must use the original grant and persistent storage, and the root-owned policy/mount binding. The issuer alone reads private certificate state. root-control remains the privileged systemd/execution-admission authority; it does not become a generic Certbot runner or certificate-serving proxy.

The existing `issuer_observer` is a *second*, **read-only** controller-owner socket, not an alternative lifecycle broker. With the same peer-UID rule it accepts only `observe-issuer` plus the original exact request ID. root-control derives storage/grant/issuer UID, protected policy and private journal from the saved controller operation. `journal-ready` proves matching request/runtime/journal completion; `tls_ready` remains **false**, always. `ongoing`, `stale` and `uncertain` never authorize publication. Keep both existing interfaces unchanged for this integration.

## Required missing interface: issuer-owned inspection

**Preferred placement:** a narrow read-only local Unix-socket inspector implemented by the `certify-manage` owner and running as the **same grant-bound issuer UID/GID** as the certificate application. It must reuse `Authorization`, `trusted_policy`, `CertificateManager.inspect()` and their real X.509/lineage/private-key/renewal-account validation. This is not an import of certificate-private state into host-deploy or the root-control daemon.

Deployment of the inspector is an independent **root-controlled and reviewed** execution/mount operation: pin exact installed code/image, fixed certificate policy and persistent storage identity, UID/GID and read-only private-state access (except where an existing validator genuinely requires otherwise). Do not give host-deploy filesystem access to the state; do not create a world-readable inspection socket; do not run the inspector as root or the controller owner; do not reassign issuer ownership to make an old same-UID timer work. The implementation must prove the private state it inspects is the same storage/grant identified by the privileged observer. If this cannot be guaranteed with a static inspector unit, revisit the deployment topology before implementation; do **not** invent a second independent writable state directory.

### Inspector protocol requirements (producer contract)

- Dedicated root-approved local socket, bounded one-request framing, only one operation such as `inspect` and **no caller-supplied path, certificate ID, hostname, policy, storage, UID, program, shell command, or arbitrary request document**. The service checks the **exact authorized host-deploy caller UID** through `SO_PEERCRED` before reading the request. Root-owned unit/socket/config and safe socket ancestry are part of the deployment trust root.
- A successful response is a **whitelisted, bounded, secret-free** `zog-issuer-x509-inspection-v1` record: certificate ID, canonical hostnames, issuer policy SHA-256, health, valid-from, expiry, leaf fingerprint SHA-256, operation/outcome, journal operation timestamp and a clear indication that issuer-owned `inspect()` succeeded. It must not contain ACME account data, Certbot directory names, PEM, private keys, DNS tokens, client-provided statuses, stdout/stderr or systemd internals. A negative/missing/uncertain result remains explicit and fail-closed.
- The inspector can supply a current snapshot **before** deciding whether renewal is due. Such a snapshot does *not* prove any particular controller request. Its response MUST NOT assert `journal-ready`, `tls_ready`, nginx delivery or a trusted controller identity merely by echoing a request ID.
- The controller/host-deploy consumer must know the inspector's service identity from **independently root-protected installed unit/socket/policy and matching original execution grant**, not from a UID claim inside the untrusted reply. `SO_PEERCRED` checked by a systemd socket-activated server authenticates the **caller**, but is not by itself proof to the client that the service executes as the intended issuer UID.
- No new root-control generic `run`, `read-file`, `get-secret` or unbounded `inspect` RPC is approved. A future specialized root-controlled activation/observation operation needs a separate explicit review if the static issuer-owned inspector cannot meet the grant/mount constraint.

### Consumer conjunction (must satisfy **all**, never only one)

A completed renewal is eligible for staging only after:

1. The retained **original** signed controller request has `application=certify-manage`, `program=renew`, `operation=launch`, exact runtime ID and `status=satisfied`.
2. The authenticated root-control-backed issuer observer reports `journal-ready` for that exact request/runtime, matching protected certificate ID, policy SHA-256, host-policy digest, storage ID, grant ID, issuer UID/GID, `outcome` and a fresh journal timestamp. Its `tls_ready=false` is **not** interpreted as a negative X.509 result or as TLS readiness.
3. A separately authenticated **issuer-owned** inspection under the proven matching issuer execution identity reports `health=valid` with no policy/renewal-account mismatch, the approved SAN set and validity interval, and the same certificate ID, policy SHA-256, **leaf fingerprint**, operation, outcome and coherent journal timestamp as (2). Reject failures, mismatch, missing X.509/key/account validation, stale/future times, `recovered-unverified` and unknown outcomes. Never mark an operation verified from controller success plus journal evidence alone.
4. Only after this conjunction: `renewed` may advance to a **new separately selected `stage` request** and the existing host-deploy delivery/rollback bridge; `unchanged` ordinarily performs **no stage or reload**. A successful `stage` is still not proof of nginx publication. Public HTTPS readiness requires independent CA-verifying SNI/leaf-fingerprint and nginx-serving verification.

The inspector may be temporarily unavailable; this blocks **new issuance, stage and declaring verified**, not the controller's read-only result recovery. An interrupted or uncertain renewal preserves its saved request ID and needs operator reconciliation. The established 48-hour request deduplication window and more conservative caller-side age fence remain in force.

## Evidence required before production approval

- Positive and negative real Linux socket tests: correct host-deploy UID, wrong UID, forged/malformed/oversized request, invalid socket ancestry, wrong server UID/unit and authorization/policy changes.
- Real systemd identity, immutable image, grant UID/GID, exact storage bind (device/inode), read-only policy, restart/reboot and cancellation/lost-reply tests across **three separate UIDs**.
- Independent real issuer inspection on valid/missing/expired/wrong-SAN/wrong-key/account-policy mismatch and operation-journal uncertainty, then strict cross-check with the controller observer.
- One changed renewal and one unchanged renewal, selected stage isolation, delivery reload/rollback, and external CA/SNI/leaf checks on an approved isolated ACME staging host.

**Present status (2026-10-16):** the selected lifecycle broker and request-bound controller observer are implemented in `box-control`/`root-control` source. The issuer-owned authenticated inspection service and its split-UID deployment/consumer wiring require `certify-manage` and `host-deploy` work and live acceptance. **Production NO-GO.**
