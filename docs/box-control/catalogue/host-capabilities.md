# Host-service directory capabilities — production cutover contract (2026-10-11)

This is the controller portion of the
station-access production cutover handoff (retained historical verification; not distributed with the public manual).

## Purpose and security boundary

Application specifications continue to declare only named application-owned writable
data mounts. They **cannot choose a host source path**. The privileged host
administrator can instead install a separate, root-owned and not group/other-
writable file at `/etc/zog/host-bindings.json`; `/etc/zog` and its ancestors
must be owned by root and not group/other-writable. Missing policy means no
host grants. A malformed policy blocks operations; it does not grant defaults.
The file must be readable by the unprivileged local box-control component so
prepared launch inputs can be durably bound.

Example (replace the project path and the provisioned export directory with
actual approved host values; this is *not* an instruction to install blindly):

```json
{
  "schema": 1,
  "bindings": [
    {
      "project": "/root/zog-station-production",
      "application": "station-access",
      "program": "scheduler",
      "capability": "mirror-live",
      "source": "/run/zog-host-export/mirror-live"
    }
  ]
}
```

Only the named program receives a read-only bind of the **directory** at
`/run/zog-host/mirror-live`. The web program has no such grant. No application
data, specification or environment value can expand the source. The source
must be an existing, real directory beneath `/run/zog-host-export/` or
`/var/lib/zog-host-export/`, without symlinks in its ancestry. Direct source
binding of `/var/lib/host-discover/credentials`, `/etc`, or a general host state
root is not supported. A privileged restart re-reads the policy,
checks exact program/systemd identity and compares the source device/inode
against the durable prepared binding. A changed policy or directory identity
blocks rather than silently substituting. The kernel-backed observation API
reports the effective destination/access/backing of an accepted mount. The
historical inventory marks it as `host-capability` with `lifetime=host`.
Application cleanup must not delete external host-owned sources.

The **directory**, rather than an individual keyring or lease file, is bound,
so atomic child-file replacement or renewal is visible without a new mount.
This does *not* authorize mounting the whole host-discover credentials
directory; host-discover must publish a narrowly scoped directory dedicated
to the authorized consumer, with appropriate file-mode/UID permissions, and
rotate the files within it. The producer owns the lifecycle of that export.
Read-only bind mounts do not override Linux DAC; the runtime UID must already
have the explicitly intended read access. Program mount namespaces are **not**
a complete confidentiality boundary between web and scheduler programs with the
same host UID; they can potentially inspect one another via host process facilities.
Do not publish raw enrollment keys through this mechanism. Use a constrained,
authenticated host-discover operation gateway for private key use, or first
establish separate program identities and verified process isolation. Never make
enrollment credentials world-readable for this integration.

No `writable` or `socket` host capability is implemented. In particular,
**do not expose the legacy bootstrap or host-deploy controller socket directly**
to the application. A namespaced, authenticated station-access gateway/adapter
must use host-deploy's adopted-workspace ownership model, distinguish
read-only observation from authorized mutations, and never pass through an
obsolete direct writer credential.

## Remaining producer and production tasks

- **host-discover**: implement a stable, least-privilege live export of
  mirror role/lease/keyring data to a dedicated directory (or a mutually
  authenticated gateway). Preserve collection scope, replacement semantics,
  host identity and revocation; do not copy stale lease files into the app.
- **station-access**: port `compiler_gateway:create_gateway` and its
  integrations into the namespaced artifact, backed by the current authenticated
  host-deploy owner-aware API. Grant scheduler-only mirror access.
  Explicitly migrate archive content and pin metadata into an application-owned
  persistent mount with a controlled one-writer cutover, or agree a separate
  host-owned writable-storage design before enabling writes.
- **host-deploy/host-install**: establish an administrator-managed durable
  root-control socket/service and box-control persistent application restart
  policy on boot, respecting prepared operation recovery and ownership
  coordination. The former isolated test controller was not a boot service.
- **Work acceptance**: in the adopted host-deploy workspace, inspect and
  provision the narrow export, test atomic replacement/renewal, policy changes,
  missing-source blocking, read-only mount evidence and lack of web access.
  Rehearse an isolated application against an independently protected
  **copy** of production state before proposing any cutover. The approved
  deployment plan must identify: exact backup/restore checkpoints and
  key custody; archive and database migration ownership and one-writer
  guarantees; host-discover export freshness and lease/keyring policy;
  controller/runtime identity and durable restart evidence; nginx routing,
  TLS and port-443 rollback; and explicit readiness/health acceptance.
  Observe and record each gate separately. The historical private handoff
  is not a deployable sequence or substitute for an authorized current
  plan. **Do not switch port 443** until every gate is accepted and an
  independently tested rollback remains available.

## Limits

This is a **controller-source/CI contract**, not a completed production
cutover. Host policy provisioning, host-discover exports, station-access
adapter, durable startup and live tests are separate accountable items.
