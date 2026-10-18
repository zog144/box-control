# Selected issuer inspect: invocation-bound evidence implementation

**Date:** 2026-10-18  
**Owners:** box-control/root-control Regular  
**Contract:** `zog-trusted-issuer-inspection-v1`  
**Status:** source implementation; native Gate B **NOT RUN**. Gate A native immutable Certbot acceptance and public host-deploy consumer remain separate.

## Narrow controller surface

`IssuerLifecycleBroker._ALLOWED` now permits exactly `renew`, `inspect`, `stage`. It still pins one `certify-manage` application, reviewed generation/software view, persistent singleton, grant and preparation state. It cannot issue `issue`/`recover`/arbitrary programs, paths, policies, shell commands, root-control methods or a persistent inspector server. A selected `inspect` requires its **own** durable controller-issued ID; a request previously bound to renew/stage cannot be repurposed.

Host-deploy's `IssuerLifecycleClient` adds `inspect_issuer_result(request_id)` using operation `inspect-result`. That operation accepts **only the original selected inspect request ID** (no caller-provided runtime, unit, stream, journal cursor, InvocationID, filename, certificate or UID). Broker reads the saved satisfied request and invokes BoxControl's specialized `inspect_issuer_result`, which delegates to the root-control reader. Returned secret-free JSON is strictly schema checked both by broker and client.

**New authentication requirement for inspection clients:** pass `controller_uid=<host-install-approved-numeric-controller-owner-UID>` when creating `IssuerLifecycleClient`; this is a protected installed-client setting, not a wire input. The host-deploy client rejects inspection without it and verifies the connected peer with Linux `SO_PEERCRED` **before** sending a request. Existing renew/stage callers without this new option continue to work unchanged; the selected-inspect result requires the opt-in stronger binding.

## Specialized privileged root-control reader

`issuer_inspect_result(project_root, request_id)` is read-only, runs in root-control's bounded reader lane, and accepts no other arguments. It proves:

1. The original saved queued request is exactly `certify-manage/inspect/launch`, with one satisfied outcome and a single matching committed, finished, published durable operation.
2. The frozen prepared runtime and completed current reference match original request ID, exact selected program, generation, software-view/consumer identity, original immutable command, systemd unit, expected properties, **mount inventory**, boot, application grant and storage ID. No cleanup pending, error or failed process. The protected root issuer execution grant is verified again.
3. The currently protected `certify-policy` and saved runtime policy mount digest agree. The issuer policy hash/SAN/certificate ID comes from the protected host-owned policy, never request JSON.
4. It queries `journalctl` with the **saved** `_BOOT_ID` + `_SYSTEMD_INVOCATION_ID` (not a caller-specified filter). Each returned entry must match that invocation. Exactly **one** bounded `ZOG_CERTIFY_INSPECT_RESULT_V1 ` record must have the right saved `_SYSTEMD_UNIT`, `_TRANSPORT=stdout` and issuer `_UID`. It scans the complete bounded journal stream, not a truncated `--lines` page that might hide a duplicate. Existing bounded reader caps are 256 KiB total and five seconds. Overflow, timeout or incomplete journald output is **unavailable**, never presumed unique.
5. Strict one-line canonical issuer schema, explicit `issuer_verified`, policy/SAN/leaf fingerprint and validity are checked. The included `observed_at` must lie within the recorded operation interval and within a 15-minute fresh-result window; journald's `__REALTIME_TIMESTAMP` must match issuer's timestamp within 120 seconds. Duplicate, malformed, stale, future, mismatched policy, wrong boot/invocation/unit/UID/transport or missing results fail closed. A process exit code of 0 **alone** is never X.509 evidence.

Outer result:

```text
schema: zog-trusted-issuer-inspection-v1
request_id, runtime_id, program=inspect, boot_id, invocation_id
storage_id, grant_id, issuer_uid, issuer_gid, host_policy_digest
inspection: {schema: zog-certify-inspection-result-v1, ...}
status: verified | unverified
tls_ready: false
```

`verified` means an issuer-owned one-shot local inspection reported valid certificate/key/lineage and its precise **saved controller systemd invocation** was proven. `unverified` reports a legitimate completed negative inspection, not success. Missing/uncertain/expired evidence is rejected and requires a **new** selected inspect request after controlled recovery; the original ID is not recycled. The result does not establish nginx serving, ACME issuance, browser/session readiness or public CA/SNI TLS. Consumers must still correlate the *separate* selected renew original request plus issuer journal observer against inspection fingerprint/operation/outcome/policy/time before stage; `renewed` and `unchanged` remain distinct.

## Source CI versus native acceptance

The box and root security jobs pin exact companion source revisions, compile actual namespace source, run `test_issuer_lifecycle_broker.py` and `test_issuer_inspection_result.py`, and include negative saved-selector, stale record, journal-attribution, duplicated/malformed output, UID, mount inventory, schema and expired result cases. These tests mock journalctl output and filesystem grants; **they do not prove native journald's stdout fields, real systemd invocation, actual Certbot X.509/linkage, mounted grant, reboot or host-deploy deployment**.

For Gate B a disposable accepted Zog host is required, after native Gate A passes: select the published immutable `certify-manage` software view, prepare and inspect as separate durable operations with real UID/GID and policy mounts, inspect a valid/missing/expired/wrong-key certificate, prove stdout attribution from actual systemd, observe negative/duplicate/truncated/foreign journal outcomes, test reboot and 48h request retention, and validate exact host-deploy consumer peer UID and report freshness. Never run real issuance/Cloudflare/nginx delivery under this approval. Gate C remains separately unauthorized.

## Source consumer handoff

- **certify-manage:** retain exactly one `ZOG_CERTIFY_INSPECT_RESULT_V1` line, current `zog-certify-inspection-result-v1` schema and immutable launcher. Avoid extra copies of the marker in logs. Changes to issuer result schema or stdout attribution require explicit contract review.
- **host-deploy:** migrate and review source on public `unstable` before integration; pin numeric broker controller UID and call selected inspect with a separate durable ID after original renew; retrieve and correlate `zog-trusted-issuer-inspection-v1`; fail closed on missing or unverified, and independently prove external local TLS delivery.
- **image-build Work:** certify actual native immutable application rootfs/view and dependencies before any selected inspect run.
- **root-control/box-control Work:** observe real invocation metadata, bounded journald completeness and recovery in disposable native Gate B, not CI alone.

**Production:** NO-GO.
