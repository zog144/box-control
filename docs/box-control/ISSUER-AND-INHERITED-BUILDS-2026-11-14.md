# Combined issuer lifecycle and inherited-peer build execution

The existing issuer selected-program broker, certificate-policy bindings, and
issuer lifecycle tests remain in this branch. Finite compilation changes its
identity contract independently of certificate issuance.

## Exact box-control build wire contract

The new version is `zog-build-inherited-peer-v2`, supplied only as the
top-level `build_contract` value on the `build_register`, `build_prepare`,
`build_start`, `build_observe`, `build_cleanup`, `build_cancel`,
`build_release` and `build_forget` Unix-socket RPCs. The existing
`project_root`, operation/job IDs, source/output resource binding, frozen
command and environment, root read-only policy, network disabled flag,
timeouts, and resource/cgroup constraints remain required.

Registration accepts only `resource_id`, `prepared_root`,
`source_directory`, `output_directory`, and `input_manifest_id`.
The nested finite-build `request` accepts the existing canonical
allowlisted fields and has **no caller-selectable UID/GID, User/Group, PID,
username, or alternate execution identity**. Unknown fields, including old
`execution_user_id`/`execution_group_id` and systemd `User=`/`Group=`
selectors, are rejected before local record creation or a socket send.
Image-build's source adapter must continue to produce this selector-free
frozen request; neither producer nor controller may translate old choices.

Box-control uses a connected `AF_UNIX` socket from the actual controller
process. The socket sender **does not attest itself with user-supplied IDs**:
root-control #48 must check the exact build contract, derive peer PID/UID/GID
from kernel `SO_PEERCRED`, refuse root callers for build mutation, verify
Project access by that executing nonroot principal, and carry the peer identity
through register, prepare, start, observation, cleanup, cancellation and
recovery. If transient systemd unit `User=`/`Group=` are necessary, only
root-control derives them from that authenticated peer. Root-control's
privileged daemon's own effective user/group are not an acceptable fallback.
The same peer/project binding must be durable and checked upon resume.

## Historical record handling

The existing schema-1 controller resource/job records remain readable for
read-only inspection. New records additionally persist
`execution_contract: "zog-build-inherited-peer-v2"`. Old records with no
contract marker, including those with numeric identity overrides, are **not
silently upgraded**; mutation and automatic recovery/replay are held with
`RecoveryRequired` until an explicit separately reviewed native migration or
administrative reconciliation. Ordinary duplicate and uncertain-result
interlocks still apply to new work, including PID/cgroup and boot checks.

## Cross-repository release gate

- **box-control #19 / PR #21**: v2 wire envelope, strict registration/request
  fields, legacy read-only marker, tests. PR #20 is the earlier smaller draft.
- **root-control #48**: actual authenticated nonroot peer-to-child execution
  binding, no root daemon fallback or caller-selected user, historical server
  records inspection-only, systemd 252-compatible execution support.
- **image-build PR #23 and successor**: selector-free producer registration,
  finite-build requests, source/recipe pins and earlier checkpoint rejection.
- **host-deploy PR #35**: hold first-Fonts dispatcher until all three exact
  reviewed source revisions have matching installed protocol and tested
  effective EUID/EGID, Project access, isolation and recovery.

No dispatch has been enabled by this source branch, and no native Host/SSM
command has been executed by this Regular implementation. The existing
issuer/service interface must remain independently usable.
