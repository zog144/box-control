# Recovery, integrity and retention

## Three separate states

Box-control reconciles (1) the **desired** declaration, (2) the **expected-launched** persisted runtime, and (3) **observed** service state from systemd. Desired differing from expected represents legitimate configuration drift; observed contradicting a prepared, expected launch may be a controller-integrity fault. Neither discrepancy authorizes an unconditional restart.

Request acceptance is not execution. When preparing an operation, box-control durably binds the resolved launch configuration, selected rootfs and affected runtime identities **before** stopping or starting services. The serialized journal can traverse prepared, stopping, launching and terminal phases. Recovery continues that **same prepared operation**: it cannot adopt a newer spec, different generation or replacement identity to make progress. Missing bound inputs cause a block, not an automatic rebuild.

## Uncertainty, reboot and blocked mutations

Start intent is persisted per service. If a launch was attempted but its acceptance/result is not known, a later boot cannot simply replay it: doing so could duplicate an action with an unknown outcome. A prepared operation that has **not** attempted a service start may begin on a new boot only after durably updating the execution boot ID; original preparation bindings remain immutable. A storage failure in that transition prevents service start.

Unfinished prepared operations protect their generations and required inputs through operation and cleanup completion. Unexpected storage or observation failures preserve the mutation interlock and evidence. Pending cleanup likewise remains protective. See [operation recovery](../catalogue/operation-recovery.md).

## Inspect without altering state

`BoxControl.recovery_status()` reports request, operation and runtime summaries, mutation interlock and structured faults. It is an observational local API: no systemd query, recovery, marker clearing, directory creation or pruning. It obtains a nonblocking shared lock when possible; busy or unavailable snapshots report uncertainty instead of partially reading a mutator's work.

A `mutation_status` of `no-recorded-block` is **not authorization to mutate**; normal operations must recheck configuration, storage, service state and preconditions. Fault guidance distinguishes safe evaluation from exact-input restoration, cleanup, storage repair and manual investigation. No generic force-unblock exists. See [recovery inspection](../catalogue/recovery-inspection.md).

`preflight_application_launch()` is likewise **advisory** and side-effect free: it creates no request, image, mount, runtime or service. `ready-to-attempt` can become blocked when launch reobserves the world. An unavailable image preview or busy snapshot is `unable-to-verify`, never a silent fallback to building software. See [preflight](../catalogue/launch-preflight.md).

## Idempotency and bounded historical storage

Caller-issued request IDs permit duplicate requests to converge on one intent/result. Issuance uses time, nonce and a per-project HMAC key; validation of issuance is **not** network-user authorization. Unused IDs expire after 48 hours. Completed results and operations remain for at least the bounded post-completion window; **accepted unresolved work does not expire solely due to elapsed wall time**. Cleanup and live-runtime references extend protection. Reuse with a different intent fails; an expired ID does not become valid merely because its old status record was pruned.

Deletion is also journaled: a frozen list of eligible records is durably stored before unlinking, and interrupted pruning finishes before subsequent lifecycle mutation. The standard 25-entry runtime-history policy is independent of request/operation retention; journald, user data and immutable image ownership are separate stores. Refer to [request identity and retention](../catalogue/request-retention.md).

## Operator sequence

1. Inspect recorded recovery and exact request/runtime identities without mutation.
2. Identify whether the problem is an unattempted prepared operation, unknown launch, missing bound input, cleanup debt or damaged storage.
3. Restore the **recorded** inputs or persistence where possible. Avoid substituting today's image or changed specification.
4. Use the supported evaluation/recovery path only when the inspection guidance and owner policy permit it.
5. Preserve unresolved faults and manually investigate if the controller cannot prove a safe outcome.

### Detailed sources

- [Operation journal and reboot recovery](../catalogue/operation-recovery.md)
- [Structured read-only recovery inspection](../catalogue/recovery-inspection.md)
- [Idempotency and pruning rules](../catalogue/request-retention.md)
- [Preflight advisory limits](../catalogue/launch-preflight.md)
- [Persistent application migration uncertainty](../PERSISTENT-APPLICATIONS.md)
