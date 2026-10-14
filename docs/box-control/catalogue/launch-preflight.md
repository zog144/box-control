# Read-only launch preflight

Call `control.preflight_application_launch(application_name)` from station-access.
The JSON-safe schema-1 result includes `status`, `checks`, `generation`,
`replacement_runtime_ids`, `snapshot`, `observed_at`, `advisory`,
`replacement_basis` and `capability_scope`.

Overall status is ready-to-attempt, blocked, or unable-to-verify. Individual
checks use ready, blocked, or unable-to-verify. A known block takes precedence
over uncertainty; display individual checks too. Invalid argument types raise
ValueError. Busy project snapshots return immediately as unable-to-verify.

No requests, runtime identities, operations, mounts, state directories, images
or services are created. No recovery, reconciliation or reclamation runs. The
existing project lock is acquired shared/nonblocking for filesystem reads and
released before probing the transport. External edits remain possible.

Definitions use the existing parser and dependency resolver. Singleton candidates
include nonterminal runtimes and failed/terminated runtimes with pending cleanup.
Preflight and launch share the same oldest-first candidate selector. Completed
cleanup terminal records are excluded. Launch re-observes candidates. Recovery
inspection retains its existing full-store scan: there is no new total filesystem
scan budget. No resolved environment values or command arguments are returned.

## Image provider contract

Optional `preview(project) -> ImageSelection | None` must resolve the same image
as ensure, without writes, builds or activation. None means no image is selected.
Missing preview or a preview error yields unable-to-verify; there is no fallback.

The default provider consumes `state/image-build/active` using the pinned external
image-build reader. It validates schema, identity, kind and output inventory.
Only published `image` generations are accepted. Package recipes and source trees
are not resolved or hashed. Publishing/activating an image belongs to image-build.
Inventory verification can still be expensive for a large rootfs: measure before
frequent polling. It runs under the shared project snapshot lock.

## Advisory limits

Capability checks cover transport connectivity and minimum systemd version only,
not authorization for a particular future privileged launch. The production
transport gets a five-second operation budget. Custom transports without
operation_budget must enforce their own timeout.

Preflight neither reserves inputs nor promises success. Normal launch repeats
checks and durably binds resolved inputs at preparation. Missing image means no
published active image is available; launch never compiles it.
Station-access owns authorization, endpoint exposure and short-poll scheduling.
This is a library API, not a new privileged RPC.
