# Public CI and companion verification

Public CI runs local controller tests and packaging checks without private
repository tokens. The historical secret-backed checkout of root-control
and image-build is intentionally not copied. Once approved public revisions
are available, add pinned cross-repository acceptance and zero-skip checks.
Live systemd and reboot behavior requires separately authorized disposable
host acceptance.

## Public aggregate compatibility and known failure gate

The public-source test job uses a read-only checkout of public `zog` at
`d47d86b363824c718d9c165124cb55bbed12e0f9` for the image-build and
root-control import/contract surfaces. The current box-control package source
takes precedence on PYTHONPATH. This is a bounded, **historical public
aggregate compatibility check**, not validation against the latest private
root-control or image-build developments.

Three test functions in `test_specification.py` (four parameterized cases)
currently fail because the frozen private unstable source's declaration parser
does not yet implement `execution_identity`. They remain in the public
test source and are explicitly deselected by this interim compatibility CI:
`test_symbolic_persistent_execution_identity_requires_no_explicit_user`,
`test_symbolic_grant_rejects_caller_chosen_credentials`, and
`test_symbolic_identity_rejects_nonpersistent_or_multi_instance`.
They are **outstanding product gates**, not passes or waived requirements.
Their actual implementation and independent revalidation belong to the
box-control execution-identity owner before expanding public CI.

## 2026-10-16: candidate security gate (current component sources)

The four symbolic execution-identity cases formerly deselected by the **historical aggregate compatibility** suite are now *executed without deselection* in the distinct candidate **controller-security-regression** job. Its ordinary PR smoke run [38970885412](https://github.com/zog144/box-control/actions/runs/38970885412) passed 468 tests with zero skipped/deselected. The four cases were explicitly checked in the report. The intentionally failing negative-control PR run [38971013118](https://github.com/zog144/box-control/actions/runs/38971013118) correctly failed (1 failed, 468 passed). Both PRs were closed and not merged.

This source gate pins root-control **ba7d0e4afc18493a34d2fd9a05a661b2ca5ad8cd** and historical public Zog aggregate **d47d86b363824c718d9c165124cb55bbed12e0f9**. The historical image-build fixture is **not an approved current image-build release**. The previous exclusions remain visible in public-aggregate-compatibility for historical coverage; they are no longer absent from the candidate security lane. Required branch-protection status-check installation is an **administrator step** and is not accomplished by committing workflow YAML.

Live systemd, socket UID/DAC, real kernel mount, reboot, PAM/seat and root-identity acceptance remain independent disposable-host gates. Cross-repository source checkout verification does not certify a native deployment.
