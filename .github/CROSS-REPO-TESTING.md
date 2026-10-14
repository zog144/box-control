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
