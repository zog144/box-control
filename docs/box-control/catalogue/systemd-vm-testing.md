# Systemd lifecycle VM acceptance

Use a disposable Linux VM with systemd as PID 1, cgroup v2, working mount
namespaces, Python 3.11+, busctl, and root access. Test the actual distribution
and systemd version intended for deployment (minimum currently declared: 250).
These are host-mutating tests and are opt-in. Each test uses a unique project
identity and removes its own application services/slice; reusable ancestor
slices may remain. Do not run against an existing Zog project.

## Minimal rootfs fixture

Prepare a dedicated small rootfs with executable `/bin/sh`, `/bin/sleep`, and
`/bin/true`, their dynamic loader/libraries if applicable, and `/etc/passwd` and
`/etc/group` entries for root. A static BusyBox fixture with those applet links
is sufficient. Provide writable directory metadata suitable for root. The tests
copy this fixture into a unique generation, add its test manifest, and create
`/data`, `/tmp`, `/proc`, `/sys`, and `/dev` if absent. Do not pass the VM's `/`
as the fixture. The fixture is test data, not a new image-build workflow.

## Commands

From the extracted bundle, create an environment and install both siblings:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ../dependencies/zog144/image-build -e './box-control[test]'
.venv/bin/python -m pytest -q
sudo .venv/bin/python -m pytest -v box-control/tests/test_systemd_integration.py \
  --run-systemd-integration --systemd-rootfs=/absolute/path/to/minimal-rootfs
```

The first suite includes a real private-D-Bus test with a simulated manager;
it should run on the VM, rather than be skipped for socket restrictions.
The second starts a real root-control subprocess on a test Unix socket. It does
not require installing or replacing the machine's root-control service. Missing
prerequisites fail when the tests are explicitly enabled.

## Automated acceptance scenarios

1. Fast successful `/bin/true`: launch records invocation identity and success,
   then service and application slice unload.
2. Exec failure after a live peer starts: launch fails, rolls back the peer,
   retains failure evidence, and unloads services and slice. Assertions require
   both member Invocation IDs, broken-member exit-code evidence, and an error
   naming that broken unit.
3. Main shell exits leaving a child: application stays running under
   `ExitType=cgroup`, and stopping it removes the descendant.
4. Program ignores INT and TERM: systemd's stop escalation finishes within the
   configured bounds and the test confirms unit unloading.
5. Two runtimes write different values through `/data`: host sources contain
   independent values and each runtime cleans up its own units.

Failures leave pytest's temporary files available for diagnosis. Read their
runtime reference records and use the exact recorded unit/Invocation IDs to
inspect journald. Journal and core-dump retention remain owned by those systems.
If cleanup itself fails, inspect and stop only the exact test-owned names before
reusing the VM; do not apply wildcard stops to unrelated Zog projects.

## Manual interruption/reconnect acceptance still required

- Interrupt box-control after identity save, after one successful member, after
  evidence save but before reset, and after reset but before release. Re-evaluate
  from disk and verify failed/incomplete launches never become successful.
- Terminate root-control after systemd accepts a start but before the controller
  receives the reply. Restart root-control and evaluate: no duplicated launch,
  no unprotected generation while cleanup remains unresolved.
- Lose the bus connection while a job is pending; check bounded failure, no
  automatic start replay, and successful later cleanup on a new connection.
- Disconnect a root-control client while its job is pending, then confirm that
  the daemon remains available for a later client and cleanup request.
- Keep an external reference to a stopped test unit. Cleanup should remain
  pending without deleting its runtime history or generation. Release that
  reference and verify a later evaluation completes cleanup.
- Repeat launch/failure/termination and inspect retained transient services and
  per-runtime slices for accumulation. Confirm root-control's connection does
  not retain units once cleanup has completed.

Record OS/systemd versions, test output, failure state and relevant journal
excerpts for Code review. The local simulated tests are not a substitute for
these host observations.

## Reference contracts

- [Systemd manager D-Bus API](https://www.freedesktop.org/software/systemd/man/org.freedesktop.systemd1.html)
- [Unit collection and job timeout semantics](https://www.freedesktop.org/software/systemd/man/systemd.unit.html)
- [dbus-next asynchronous connection API](https://python-dbus-next.readthedocs.io/en/latest/message-bus/aio-message-bus.html)
