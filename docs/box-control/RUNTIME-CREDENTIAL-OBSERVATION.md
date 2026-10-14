# Authoritative runtime execution credential observation (v1)

**Program:** box-control + root-control  
**Status:** source implementation; real-host validation pending

## Read-only consumer interface

`BoxControl.observe_application_runtime_credentials(runtime_id, limit=50)`
accepts only the immutable runtime identity and an optional bounded
process count (1 through 64). A caller cannot specify a PID, cgroup,
systemd service name, expected UID/GID or a user account. No mutation,
service start/stop, privilege grant or host NSS lookup is performed.

The root-control daemon implements
`application_runtime_credentials` in its bounded **read-only**
inspection lane. It derives the authorized program units, saved
invocation IDs, boot ID, prepared execution identity and expected
systemd properties from the persisted runtime reference. It then reads
real Linux kernel `/proc/<pid>/status` credentials for processes
inside systemd's observed cgroup (including nested cgroups).

Response schema: `zog-runtime-credential-observation-v1`.

Each process entry reports:

- PID and kernel start-time ticks, for PID-reuse identification.
- Four UIDs and four GIDs, in Linux order:
  **real, effective, saved-set, filesystem**.
- Supplementary group IDs, and one of `match`, `mismatch` or
  `unbound` comparisons against the exact saved controller grant.

Each program entry includes saved systemd unit/invocation, observed
control group, expected execution grant when available, a bounded
process list and its independent status. Result-level statuses are
`observed`, `credential-mismatch`, `integrity-fault`,
`changed-during-read`, `partial`, `legacy-unbound`,
`absent`, `previous-boot`, `identity-unavailable` or
`unavailable`. The snapshot is explicitly **non-atomic**.

## Authoritative evidence rules

1. Saved runtime boot ID must equal the current host boot.
2. Each service must remain an unmodified transient systemd unit with
   exactly its recorded invocation ID before and after the bounded
   process inspection; the selected cgroup comes exclusively from
   systemd.
3. Every reported process is scoped to that cgroup. Its `/proc`
   directory is opened without symlink following; process start ticks,
   membership and all status credentials are rechecked inside the
   same directory descriptor to avoid PID reuse or mid-read changes.
4. Managed runtime grants must agree with their saved project,
   application, grant ID/version, and the launched `User` and
   `Group` systemd properties. All four real/effective/saved/filesystem
   UID/GID fields must match the controller-issued numeric pair, and
   supplementary groups must not contain a foreign GID.
5. Incomplete cgroup traversal, disappeared PIDs, changing
   credentials, overloaded limits or a lost systemd unit yield
   partial/unavailable results, **not verified identity**.
   A stable observed difference yields `credential-mismatch`.
6. Historical applications without a managed execution grant expose
   factual UID/GID/groups but are explicitly `legacy-unbound`.
   Their mutable host NSS lookups are never treated as evidence of a
   controller-issued identity.

This is a runtime inspection, **not** a saved immutable historical
credential record, nor an attestation of an exited process. No
inspection result can reconstruct credentials for a transient process
after that process and its cgroup have disappeared.

## Verification handoff

Tests exercise real/effective/saved/filesystem UID/GID fields,
supplementary groups, project/application/systemd invocation binding,
reused PIDs, changed cgroup membership, changing credentials,
nonexistent units, previous boots, incomplete scans and read-only
daemon/API limits. The existing opt-in native systemd credential
canary remains a separate privileged test.

Host-deploy should use this API while a bounded station-access and
archive-mirror process is actually running, recording the exact
source revision, grant IDs, /proc credentials, observed group sets,
systemd invocation, cgroup and boot ID. Test a mismatched-identity
candidate in a disposable project only. Observe actual DAC,
persistent storage ownership and mount namespaces separately; a
`status='observed'` from this API alone is not a complete production
cutover acceptance.

**Production: NO-GO** until owner-controlled host acceptance completes.
