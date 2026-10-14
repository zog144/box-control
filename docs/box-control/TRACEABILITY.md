# box-control documentation traceability

**Migration stage:** navigation established, detailed source texts preserved.

| Existing source | Assigned manual chapter(s) | Disposition |
|---|---|---|
| [APPLICATION-MOUNTS.md](APPLICATION-MOUNTS.md) | Applications, workspaces and mounts | Retained pending status and contract reconciliation |
| [IMAGE-BUILD-EXECUTION-TYPES-AND-MAPPING.md](IMAGE-BUILD-EXECUTION-TYPES-AND-MAPPING.md) | Overview and architecture; Interfaces and integration | Retained pending status and contract reconciliation |
| [PERSISTENT-APPLICATIONS.md](PERSISTENT-APPLICATIONS.md) | Applications, workspaces and mounts | Retained pending status and contract reconciliation |
| [REGISTRATION-RECOVERY.md](REGISTRATION-RECOVERY.md) | Recovery, integrity and retention | Retained pending status and contract reconciliation |
| [WORKSPACE-APPLICATIONS.md](WORKSPACE-APPLICATIONS.md) | Applications, workspaces and mounts | Retained pending status and contract reconciliation |
| [catalogue/build-jobs.md](catalogue/build-jobs.md) | Inspection and events | Retained pending status and contract reconciliation |
| [catalogue/build-visibility.md](catalogue/build-visibility.md) | Inspection and events | Retained pending status and contract reconciliation |
| [catalogue/host-capabilities.md](catalogue/host-capabilities.md) | Interfaces and integration | Retained pending status and contract reconciliation |
| [catalogue/implementation-status.md](catalogue/implementation-status.md) | Overview and architecture | Retained pending status and contract reconciliation |
| [catalogue/launch-preflight.md](catalogue/launch-preflight.md) | Lifecycle, operations and policies | Retained pending status and contract reconciliation |
| [catalogue/lifecycle-events.md](catalogue/lifecycle-events.md) | Lifecycle, operations and policies; Inspection and events | Retained pending status and contract reconciliation |
| [catalogue/operation-recovery.md](catalogue/operation-recovery.md) | Lifecycle, operations and policies; Recovery, integrity and retention | Retained pending status and contract reconciliation |
| [catalogue/process-inspection.md](catalogue/process-inspection.md) | Inspection and events | Retained pending status and contract reconciliation |
| [catalogue/reboot-testing.md](catalogue/reboot-testing.md) | Verification | Retained pending status and contract reconciliation |
| [catalogue/recovery-explanations.md](catalogue/recovery-explanations.md) | Recovery, integrity and retention | Retained pending status and contract reconciliation |
| [catalogue/recovery-inspection.md](catalogue/recovery-inspection.md) | Recovery, integrity and retention; Inspection and events | Retained pending status and contract reconciliation |
| [catalogue/request-retention.md](catalogue/request-retention.md) | Lifecycle, operations and policies | Retained pending status and contract reconciliation |
| [catalogue/station-access.md](catalogue/station-access.md) | Interfaces and integration | Retained pending status and contract reconciliation |
| [catalogue/systemd-vm-testing.md](catalogue/systemd-vm-testing.md) | Verification | Retained pending status and contract reconciliation |

**Source coverage:** 19/19 Markdown documents. Do not delete sources until all requirements, examples, references and historical statuses have been reconciled.

## Editorial consolidation progress — 2026-10-12

This pass replaces the listed canonical chapter placeholders with substantive summaries of current source contracts. The original reference documents remain at their stable paths for exact schemas, historical status and implementation-specific details. A synthesized chapter is not yet a deletion authorization for any referenced file.

| Synthesized reader chapter | Primary preserved evidence | Status |
|---|---|---|
| [Applications, workspaces and mounts](guide/applications-workspaces-and-mounts.md) | PERSISTENT-APPLICATIONS.md · APPLICATION-MOUNTS.md · WORKSPACE-APPLICATIONS.md | Reader synthesis committed; original detail preserved |
| [Recovery, integrity and retention](guide/recovery-integrity-and-retention.md) | catalogue/operation-recovery.md · catalogue/request-retention.md · catalogue/recovery-inspection.md · catalogue/launch-preflight.md | Reader synthesis committed; original detail preserved |

**Editorial boundary:** The remaining manual chapters still need full synthesis. The retained documents must be compared line-by-line for unique requirements, example validity, duplicate claims, implemented-versus-historical status and explicit acceptance evidence before any source deletion. Machine-readable schemas and release provenance remain standalone. Source change does not establish production deployment or stable-release readiness.

## Editorial consolidation progress — 2026-10-12, continuation

The following chapter entry points now contain substantive synthesis of the preserved detailed sources. Unique versioned contracts, source evidence and historical acceptance declarations remain under their existing paths; the prose has **not** authorized deletion of any original document or production deployment.

| Reader chapter | Important preserved sources | Status |
|---|---|---|
| [Lifecycle, operations and policies](guide/lifecycle-operations-and-policies.md) | catalogue/implementation-status.md · catalogue/station-access.md · catalogue/build-jobs.md · catalogue/lifecycle-events.md | Synthesized; detailed contract retained |
| [Inspection and events](guide/inspection-and-events.md) | catalogue/process-inspection.md · catalogue/build-visibility.md · catalogue/recovery-inspection.md · catalogue/host-capabilities.md | Synthesized; detailed contract retained |

**Unresolved editorial work:** Reconcile source claims and examples against the current implementation before replacing detailed files; complete the remaining chapter summaries; audit duplicate or superseded requirements separately from live acceptance. This is documentation review, not a component release candidate.

## Editorial consolidation progress — 2026-10-12, enrollment and contracts

Two further reader chapters replace placeholders with reviewed conceptual synthesis. Detailed source contracts and immutable schemas remain at their original paths, including older dated implementation and acceptance reports. Where later sources supersede an earlier limitation, the consolidated chapter names the change in scope without deleting the historical evidence.

| Consolidated chapter | Preserved contract or evidence | Treatment |
|---|---|---|
| [Interfaces and integration](guide/interfaces-and-integration.md) | catalogue/station-access.md · WORKSPACE-APPLICATIONS.md · catalogue/build-jobs.md · REGISTRATION-RECOVERY.md · catalogue/host-capabilities.md | Synthesis committed; original authoritative sources retained |
| [Verification](guide/verification.md) | catalogue/systemd-vm-testing.md · catalogue/reboot-testing.md · catalogue/recovery-inspection.md · APPLICATION-MOUNTS.md | Synthesis committed; original authoritative sources retained |

**Evidence boundary:** This editorial work makes no production cutover, host mutation, rootfs publication, source-pin update or new acceptance claim. Remaining chapters and line-by-line reconciliation of redundant historical notes are separate tasks.

## Requirement-family reconciliation — 2026-10-12

The architecture and integration chapters are now reader-facing contracts. The matrix records which earlier pass reports are historical, while preserving program-owned lifecycle and validation semantics.

| ID and requirement family | Reader synthesis | Retained detailed sources | Deduplication disposition |
|---|---|---|---|
| **BC-LIFE-01** — Application/spec/runtime identities, systemd service and start policies | [Architecture](guide/overview-and-architecture.md), [Lifecycle](guide/lifecycle-operations-and-policies.md) | [Implementation status](catalogue/implementation-status.md) | Historical pass-specific status; preserve unique start-policy and schema migration assertions |
| **BC-REC-02** — Prepared-operation binding, unknown launches, reboot recovery and mutation guard | [Recovery](guide/recovery-integrity-and-retention.md) | [Operation recovery](catalogue/operation-recovery.md), [Recovery inspection](catalogue/recovery-inspection.md) | Keep exact recovery API/phase semantics and fault signatures as contracts |
| **BC-MNT-03** — Immutable rootfs, writable STATE, host capabilities and workspace mounts | [Applications/mounts](guide/applications-workspaces-and-mounts.md) | [Application mounts](APPLICATION-MOUNTS.md), [Host capabilities](catalogue/host-capabilities.md) | Keep normative host-root policy and privileged validation contract |
| **BC-INT-04** — Scoped station-access calls, read-only observations, request IDs | [Integration](guide/interfaces-and-integration.md), [Inspection](guide/inspection-and-events.md) | [Station-access integration](catalogue/station-access.md), [Preflight](catalogue/launch-preflight.md) | Retain exact response schemas, identity/cursor limits and advisory meanings |
| **BC-JOB-05** — Finite build jobs, registration progress and journal identity | [Lifecycle](guide/lifecycle-operations-and-policies.md), [Integration](guide/interfaces-and-integration.md) | [Build-job contract](catalogue/build-jobs.md), [Registration](REGISTRATION-RECOVERY.md) | Keep controller request/result and root registration types independently |
| **BC-TEST-06** — Mock/VM/reboot acceptance and evidence limitations | [Verification](guide/verification.md) | [VM testing](catalogue/systemd-vm-testing.md), [Reboot test phases](catalogue/reboot-testing.md) | Do not delete opt-in host procedures or reinterpret simulation as power-loss testing |

**Retirement gate:** Before removing a source file, enumerate every `MUST`, `MUST NOT`, API field, error/status meaning, request identity, example path, test limit, unique historical result and licensing notice. Verify each surviving destination against the exact reviewed implementation and version. Keep an immutable know-file reference for dated evidence, update all inbound links, run source/packaging checks, and obtain an explicit commit review. No original documents are retired by this matrix.
