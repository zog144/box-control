# Certificate inspector runtime ownership review

**Date:** 2026-10-16  
**Review owners:** box-control / root-control  
**Inspected:** box-control `unstable` at `1a072786cb5c99bc27643c491f4d0fc67c982d35`; root-control `unstable` at `38848de2bc65333e07f0c22397a255c5d440d24d` (before the hardening noted below); certify-manage `unstable` at `dfed5105f48eedad699bc5ab2f0d19257553b616`.  
**Decision:** **NO-GO** for deploying the current long-running `zog.certify_manage.inspection` server as an independently installed systemd service. **Preferred first integration:** short-lived, selected `certify-manage/inspect` with a separately authenticated and request-bound, redacted result. **Alternate (future):** a newly specified, durable controller-managed inspection companion runtime.

## Findings grounded in current code

The issuer's `inspection.py` is an appropriate **application-layer** implementation: read-only protocol, Linux `SO_PEERCRED` checks, nonce, no caller-controlled paths, bounded output, lock-aware issuer snapshot and X.509/lineage/private-key/account-policy validation through the existing manager. Its present `_activated_socket()` accepts one inherited AF_UNIX listening descriptor and `main()` loops indefinitely. It is intentionally **not** deployed on systemd. See [issuer-owned inspector contract](https://github.com/zog144/certify-manage/blob/unstable/docs/issuer-inspection-v1.md).

Root-control's existing `BusctlSystemdBackend.start_service()` admits **managed transient Zog services**, not static service units. It verifies a durable selected operation, rootfs generation/runtime scaffold, saved command and unit, exact execution grant, selected persistent storage, host capability, immutable software view, mount inventory, `RootDirectory`, `User`/`Group`, `ExitType=cgroup` and journald output before `StartTransientUnit`. A certificate-specific privileged reader independently verifies the request-bound issuer journal. Source-only selected-program acceptance is documented in [SELECTED-PROGRAM-EXECUTION](SELECTED-PROGRAM-EXECUTION.md).

The root-owned `execution-identities.json` registry explicitly prohibits **two applications sharing the same UID/GID grant**: the identity is bound to the *project + application*, not merely to a numeric user. A `certify-inspector` *second application* cannot reuse `certify-manage`'s UID without redesigning that protected grant model.

The currently deployed/declared `certify-manage` application is `persistent=True`, `multiple_instances=False`, `execution_identity=application`, `start_policy=externally-controlled`. It declares `prepare` and selected `issue`, `renew`, `stage`, `inspect`. Only one unresolved selected runtime is allowed. Therefore selecting the existing inspector's infinite accept loop would keep a runtime unresolved indefinitely and **block renew and stage**.

The guest `/var/lib/zog-certify-manage` and `/run/zog-host/certify-policy` are bound to managed **runtime-specific** mount/rootfs scopes. A host-side static unit with `User=<issuer UID>`, `ExecStart=/usr/bin/python3 ...`, or manually reconstructed `BindPaths` has **not** passed Zog's snapshot-bound root-control admission and may not have the prepared immutable image, host policy or mount provenance. `SO_PEERCRED` and systemd DAC do not cure that loss of lifecycle authority.

### Explicitly disallowed in v1

- An untracked host-installed `certify-inspect.service`, `@.service`, or socket worker that reaches into the project state by host path, even if the unit is root-installed and `User=` equals the issuer UID.
- Adding `inspect-server` as a long-running selected program in the existing singleton, silently moving the issuer UID to host-deploy/controller-owner or granting a second ordinary application the existing grant.
- Using `systemd-run`, shell wrappers, direct unreviewed `StartTransientUnit`, a new writable copy of ACME state, a host interpreter in place of the approved view, a generic root-control run/read-file RPC, or an operator's ad hoc mount workaround.
- Treating the issuer inspection socket's response as authenticated merely because `SO_PEERCRED` authenticates the **client**: the client must establish the server's provenance and exact grant-bound runtime identity independently.
- Treating `journal-ready` or `issuer_verified` individually as TLS/CA/nginx readiness.

## Recommended v1: selected, short-lived inspection

**Use the existing `certify-manage/inspect` program as a finite controller-managed runtime**, instead of activating the current infinite socket server.

1. Host-deploy's narrow lifecycle capability may be extended specifically to **request a new selected `inspect` invocation**, but not `issue`, `recover`, `prepare`, arbitrary application commands or an `inspect-server` selector. Do not open a generic evaluator, raw controller, or root-control socket. Existing `renew`/`stage` authority remains unchanged. The request ID must be issued/retained durably before submission.
2. The existing box-control preparation/singleton admission and root-control validation bind the exact `inspect` program, immutable published generation, software view, storage ID, execution identity grant, host policy and approved mount inventory. A running, uncertain or cleanup-pending renewal **blocks inspect**, which is the intended consistency behavior. Inspection never cancels/replaces an in-flight mutation.
3. The issuer's `inspect` implementation must reuse its current real X.509/lineage/key/ACME renewal-policy checks and produce **one strictly bounded secret-free inspection result**. If running outside its normal singleton for any future reason, it must also hold the issuer's protected shared snapshot lock; do not rely on arbitrary private JSON from the caller. The current one-shot CLI emits a JSON status to stdout; it is not *yet* a trusted inter-process result API.
4. Implement a **dedicated read-only result adapter** in the controller/root-control boundary: derive the original selected `inspect` request, committed successful operation, runtime ID, verified boot/systemd invocation ID and exit/cleanup evidence; acquire exactly one bounded, strictly parsed, secret-free issuer status for **that exact invocation** (e.g., via root-control's invocation-scoped journald barrier). Reject zero/multiple/malformed/stale records; never accept a caller-provided status, general journal search or unbounded journal text. The current issuer-observer operation validates `issue`/`renew` journals and must not be relabeled as an X.509 result.
5. Host-deploy correlates the latest verified issuer inspection with the separately authenticated **original renew request** and its trusted `journal-ready` observation: certificate ID, SANs, policy hash, fingerprint, outcome, operation timestamp, storage/grant and issuer identity must agree. A later inspect has its **own** controller request ID; it is **not** substituted for the original renewal ID.
6. Keep the number of inspection launches appropriately bounded. Each inspection is a real Zog runtime/history entry; do not exhaust the default 25-entry history while pending 48-hour request/provenance evidence still matters. Protect unclosed original operation evidence against normal retention, or prove the cadence cannot prune required observations.

**This is a design proposal, not a statement that the journal result adapter or broker's `inspect` selector is currently implemented.** The existing `IssuerLifecycleBroker` allows only `renew` and `stage`. Certify-manage's currently implemented independent inspection socket remains useful protocol code but is not approved as an externally installed service.

## Longer-term alternative: managed persistent inspection companion

If long-lived AF_UNIX inspection is a hard requirement, a new **controller-owned companion runtime** is necessary. Before implementation, specify and test all of the following as one contract:

- The companion belongs to the **same** `certify-manage` project/application and original exact UID/GID grant, storage ID and immutable software view. It receives a separate durable runtime/member identity; it is **not** a second ordinary application reusing the UID.
- Controller persists a bound operation/supervision record for the companion **before** privilege is exercised. Root-control must match project, application, recorded command, image/generation/software binding, guest rootfs, storage (read-only private mount access where feasible), host-policy digest, exact unit/slice/cgroup and selected activated socket path/inode; reject unrecorded/changed units. No direct static unit file/host runtime bypass.
- An explicit narrow concurrency rule allows **this one read-only companion** alongside selected `renew` and `stage`; their issuer writer lock remains authoritative. The companion does not expand mutation authority or acquire its own storage grant. No ordinary application may opt into this without a new approved capability.
- Specify socket activation ownership, listener creation, UID-to-UID authentication, server identity verification, client nonce, startup readiness, socket inode/link changes, rate/connection bounds, crash/restart/reboot reconciliation, policy/view rotation, graceful stop, pending cleanup and generation reclamation. External systemd changes are integrity faults, not a valid alternate control plane.
- Verify host-deploy cannot read raw protected state, keys or provider credentials and cannot cause arbitrary shell/service launches. Host-deploy's cached inspector result expires; on disappearance or mismatch, fail closed and recover through controller records.

This companion is a substantive box-control/root-control lifecycle extension, not an `ExecStart` option in an installer. Do not deploy until it has acceptance coverage.

## Immediate root-control hardening

In this review, root-control `unstable` was hardened to reject **unreviewed `certify-manage` program names** at privileged service admission whether or not they carry a caller-supplied `selected_program` value. It preserves the nonselected controller-owned `prepare` workflow and requires explicit selected authority for `issue`, `renew`, `inspect`, `stage`. A selected `prepare` is rejected. Regression source cases include `inspect-server`, `recover` and `probe`.

This guard constrains **root-control's own service-start interface only**; it does not grant control over arbitrary units installed through an independent host-root systemd path. Do not describe it as a comprehensive systemd service installation blocker.

- [Privileged systemd code](https://github.com/zog144/root-control/blob/unstable/src/zog/root_control/systemd.py)
- [Selected-program authority tests](https://github.com/zog144/root-control/blob/unstable/tests/test_selected_program_authority.py)

## Owner handoffs and acceptance

**certify-manage:** either publish a one-shot `inspect` result suitable for strict invocation-scoped observation (reusing all existing issuer validation), **or** propose the companion-runtime contract before requesting installation of its current socket service.

**box-control/root-control:** first extend the fixed broker only with the reviewed selected `inspect` scope and implement a bounded authentic result reader. No direct privilege or issuer-state access to host-deploy. If opting for the companion later, design and test durable identity, multi-operation concurrency, crash/reboot recovery, socket binding and generation protection before deployment.

**host-deploy:** use retained separate controller request IDs for inspect and renew; verify two authenticated sources and never accept a raw caller-supplied inspection JSON. Keep the old renewal fenced during uncertain/lost-reply conditions.

**Work acceptance:** isolated host with three distinct UIDs; verify exact rootfs, grant/NSS, mount device/inode, protected policy, runtime and journal invocation, wrong UID/nonce, race/lock, revoked policy, mixed-generation, lost reply, reboot, and changed/unchanged Certbot outcomes; separately verify nginx delivery and CA/SNI/fingerprint.

**Current result:** deployment of a long-running inspector under a static host systemd unit is **NO-GO**. Neither the selected-result adapter nor the companion-runtime alternative has native acceptance yet. Production renewal and HTTPS cutover remain **NO-GO**.
