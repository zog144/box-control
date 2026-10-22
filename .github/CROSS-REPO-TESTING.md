# Public source CI and companion verification — October 22, 2026

The public `ci.yml` now runs controller source compatibility tests on Python **3.12 and 3.13** with the separately checked-out, immutable reviewed `zog144/root-control@8a9583924109ef324eb80071953151c8c7e20b33`. It checks both current component import locations and keeps `zog144/zog@d47d86b363824c718d9c165124cb55bbed12e0f9` exclusively as historical **non-controller namespace support**. This fixture is *not* a current approved image-build build artifact.

The three previously deselected execution-identity test **functions** (four parameterized cases) are **no longer deselected** from the public workflow:

- `test_symbolic_persistent_execution_identity_requires_no_explicit_user` — one case;
- `test_symbolic_grant_rejects_caller_chosen_credentials` — two cases;
- `test_symbolic_identity_rejects_nonpersistent_or_multi_instance` — one case.

The public compatibility job writes a JUnit report and **fails** if exactly these four instances are not collected, or if any test fails, errors or skips. It uses no `--deselect`, `-k` exclusion or blanket skip. The separate `controller-security-regression.yml` already runs a broader selection on all pull requests into `main` and `unstable`; it also enforces the four required tests, no skips/failures and exact root-control checkout. Root-control's `privileged-socket-regression.yml` pins box-control at an explicit reviewed source revision and fails on any skipped/failing security cases.

**2026-10-22 source CI:** [box-controller security run 39779175887](https://github.com/zog144/box-control/actions/runs/39779175887) PASS; [box public compatibility run 39779175661](https://github.com/zog144/box-control/actions/runs/39779175661) PASS on both Python versions; [root privileged security run 39779113795](https://github.com/zog144/root-control/actions/runs/39779113795) PASS. Certificate and boot-recovery source CI have added ordinary PR triggers; their historical acceptance evidence is not relabeled as native.

**Branch protection is separate:** workflow `pull_request` triggers schedule checks but do *not* make them mandatory before merge. A repository administrator must protect both `main` and `unstable`, require exact job contexts and reviewed approvals, and prohibit bypass/force push as appropriate. Do not claim this is configured based on a passing workflow.

**Native/privileged boundary:** Linux systemd D-Bus, actual socket `SO_PEERCRED` and DAC/UID/GID, filesystem/mount namespace, SELinux, certificate issuance, live reboot and unattended compiler execution remain independent, authorized disposable-host acceptance gates. Source CI never elevates on GitHub runners and never deploys to a real Zog host.
