# Reboot recovery phases

`tests/reboot_phases.py` supplies separate `prepare` and `verify` commands for
host-deploy. The project path must be a fresh, dedicated directory on persistent
storage, such as `/var/lib/host-deploy/<run>/reboot-prepared`; do not use `/tmp`
for an actual reboot test. Use a distinct project for each case.

Cases: `completed`, `prepared`, `uncertain`, `failed`.

Preparation launches real services in real mode and durably writes
`reboot-checkpoint.json` at the required interruption boundary. It exits with
`os._exit(0)`, deliberately bypassing controller rollback/finally handlers. The
checkpoint is required evidence: an exit code alone does not establish readiness.
Preparation refuses a pre-existing checkpoint or controller state directory.

Verification uses a fresh process, checks the boot ID changed, observes that the
old transient units are absent, invokes the public evaluation/recovery API, and
asserts the case-specific outcome. It also checks duplicate requests, generation
protection/reclamation, and preserved identities. The uncertain case deliberately
uses explicit abandonment after checking that recovery blocks new mutations and
inspection remains available. That is acceptance-test behavior, not automatic
abandonment in production.

## Real host integration (prepared, not executed in pass 4)

From the source archive root, with the sibling packages installed or on PYTHONPATH:

```sh
python box-control/tests/reboot_phases.py prepare --case prepared \
  --project /var/lib/host-deploy/RUN/reboot-prepared \
  --rootfs /var/lib/host-deploy/RUN/rootfs --socket /run/zog-test-root.sock
```

Host-deploy must start an independent root-control daemon before preparation,
verify the checkpoint exists, request a normal reboot, wait for a changed boot ID
and SSM access, and start a new root-control daemon before verification. The
fixture rootfs must contain `/bin/sh`, `/bin/sleep`, `/bin/true`, their shared
libraries and a root user/group, as in the existing lifecycle recipe. The driver
creates test generations from this fixture; production image ownership is unchanged.

```sh
python box-control/tests/reboot_phases.py verify --case prepared \
  --project /var/lib/host-deploy/RUN/reboot-prepared --socket /run/zog-test-root.sock
```

Verification writes `reboot-result.json` only after every assertion passes.
Host-deploy should preserve both phases' stdout/stderr, checkpoint, result,
controller state and journals, and own the host timeout and final disposal.
Successful prepared recovery leaves its test services running so their invocation
identities can be checked. Host-deploy must dispose of the dedicated host after
collecting evidence. Do not repeat a completed preparation; the orchestrator must
track phase outcomes. An interrupted verification without a result requires
inspection before rerunning the acceptance driver, which has strict initial-state
assertions. Controller recovery itself remains retryable.

## Simulation exercised by pytest

`test_reboot_recovery.py` runs the same phase commands in separate subprocesses
with `--simulate-boot-id boot-before` and then `boot-after`. Each process gets a
fresh fake systemd transport; no units survive into verification. A same-boot
verification attempt must fail before controller mutation. Both the checkpoint
and result explicitly identify simulation. Real and simulated phases cannot be
mixed.

This tests controller logic and on-disk recovery across fresh processes. It does
not emulate kernel reboot, systemd shutdown behavior, filesystem write loss,
storage-cache behavior or power loss. The real-mode entry points still need EC2
acceptance after host-deploy gains reboot coordination.
