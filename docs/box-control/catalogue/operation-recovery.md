# Operation recovery contract — implemented pass 2

The original D1–D16 decision discussion is retained as historical internal
design evidence, not a dependency of the published recovery interface. The
maintained [recovery, integrity and retention chapter](../guide/recovery-integrity-and-retention.md)
and [request-retention contract](request-retention.md) state the current
reader-facing constraints; this page preserves the additional preparation
and reboot cases. No private design or handoff file is required to apply the
public contract.

In particular, request acceptance is not execution; a prepared operation
binds its immutable launch inputs before effects; unknown service-start
outcomes cannot be blindly replayed; incomplete cleanup and protected
generation references continue blocking mutation. The normal 25-entry
runtime-history view is separate from durable operation/request records.
Storage faults, missing recorded inputs or inconsistent service evidence
remain fail-closed. Recovery is not a force-unblock facility.

## Additional accepted decisions

D17 — Preparation binding. Durable preparation binds an operation to its complete
resolved launch configuration, selected rootfs generation, and affected runtime
identities before its first lifecycle action. Acceptance alone records the caller's
operation and target. Unprepared requests resolve their configuration when preparation
begins. Recovery uses the recorded configuration; missing inputs block without
substitution or image creation by box-control.

D18 — Prepared input protection. Unfinished prepared operations protect their
selected generation and other Zog-managed required inputs until both the operation
and required cleanup are complete. External removal still causes a blocking fault.

## Record and recovery sequence

The serialized operation progresses through prepared, stopping, launching, and
committed/failed/abandoned phases. Its affected runtime snapshots are authoritative
until publication and required cleanup finish. A previous snapshot permits repair
of the one interrupted derived write; other contradictions require inspection.
Start intent is persisted per service, and recovered service evidence is checked
against its expected properties and known invocation identity.

Operation records are independent of the normal 25-entry completed runtime-history
view. Their compact retention/expiry implementation is still pending. Do not delete
records manually to simulate expiry or clear recovery blocks.

## First execution after a reboot (pass 4)

When no service-start intent has been recorded, a prepared operation may begin on
a later boot. Before its first start, the operation journal and runtime reference
must durably record that execution boot. The operation's optional
`prepared_boot_id` retains the original preparation boot; `boot_id` identifies the
boot to which service-start intent belongs. Older records without
`prepared_boot_id` are upgraded when this transition occurs. Runtime identity,
configuration and selected generation remain bound to the original preparation.

This ensures that START_ONCE_PER_BOOT counts a short-lived recovered execution
against the boot on which it actually ran. A storage error during the boot update
prevents service start. An attempted, uncommitted launch from a previous boot
still blocks as uncertain; the boot update never authorizes its replay.
