# Dedicated host-welcome controller-progress observer — controller peer review

**Date:** 2026-10-17  
**Owners:** box-control/root-control Regular (source authority and peer separation).  
**Deployment owner:** host-install Regular; **native acceptance:** host-deploy Work on a disposable host.  
**Origin:** [host-welcome handoff](https://github.com/zog144/know-file/blob/8bb31f175c5449737c01cc1397581e389ff127eb/host-welcome/handoffs/host-install-box-control-console-observer-2026-10-17.md).  
**Status:** controller source implementation and negative regression fixtures supplied; no native deployment, socket creation, UID assignment, privilege change, local-seat authorization or getty change.

## Separation decision

Host-welcome must **not reuse** the station web progress socket at `/run/zog-host-export/application-progress/application-progress.sock` (or its station runtime projection). Its optional controller channel is the distinct **exact** host path `/run/host-welcome/controller-progress.sock`. It uses a separate instance of the *existing read-only* `zog.box_control.application_progress_broker`, under the fixed, non-root controller project-owner UID, serving only the dedicated **host-welcome** UID. The independent station service and socket ACL remain unchanged. Sharing source code does not share the listener or consumer credential.

The privileged `root-control` socket at `/run/zog/root-control.sock` continues to admit only the explicitly configured numeric controller UID through peer credentials. Host-welcome, station web, issuer, host-install observer and other roles must **never** receive membership/access to that generic privileged endpoint; all their permitted reads remain on separate narrow brokers.

### New broker activation contract

Host-install's root-protected systemd service **must** include:

```text
User=<approved-numeric-controller-owner-uid>
Group=<approved-numeric-controller-owner-gid>
ExecStart=<approved-fixed-python> -m zog.box_control.application_progress_broker --project <approved-absolute-controller-project> --authorized-uid <approved-numeric-host-welcome-uid> --root-control-socket /run/zog/root-control.sock --observer-role host-welcome --controller-owner-uid <approved-numeric-controller-owner-uid>
```

The corresponding socket unit is a *separate* root-installed AF_UNIX socket with:

```text
ListenStream=/run/host-welcome/controller-progress.sock
SocketUser=<approved-numeric-host-welcome-uid>
SocketMode=0600
Accept=no
```

The root-owned systemd unit may specify an explicit approved consumer GID as `SocketGroup`, but **must not** use group mode/access as authorization; it must not grant access to station web or ordinary applications. The broker itself checks Linux `SO_PEERCRED` **before frame parsing**, admits only `--authorized-uid`, accepts only `snapshot` and inert `local-session`, and returns only bounded secret-free statuses. No launch, prepare, cancel, build, filesystem, login or credential API is added.

Source extension in `application_progress_broker.py`: `--observer-role host-welcome` **requires** the exact inherited activated listener pathname, root-owned parent chain with no symlink or group/world write, listener socket inode type, UID equal to `--authorized-uid`, and file mode **exactly** `0600`. It rejects the dedicated console listener when the installer omits the explicit role flag, and requires the executing UID to match the separately pinned numeric `--controller-owner-uid` (at least 1000). Station mode rejects this console-only option. Station mode retains the earlier activation behavior so no existing station unit needs changing. `--root-control-socket` must remain the existing literal `/run/zog/root-control.sock`; root control's own peer UID policy still protects that endpoint.

The verified socket path and numeric consumer UID are **installation authority** held by root-owned systemd files; a JSON request cannot select either. The broker's permission check does not substitute for installed-unit authenticity, actual package bytes or filesystem denial of wrong peers. It is deliberately not an independent controller-managed application runtime and gives host-welcome no additional execution authority.

### Required root-owned host-welcome policy

Only the installer can stage `/etc/zog/host-welcome-progress-observer.json` with `schema=zog-host-welcome-progress-observer-v1`, `enabled=true`, exact distinct numeric `consumer_uid` and `controller_owner_uid`, exact fixed `socket`, and a **full 40-hex reviewed `box_control_source_commit`**. Reject symlinks, group/world write, non-root ownership, unknown or duplicated fields. The official host-welcome client already validates this file, its runtime UID, server peer UID, boot, response freshness and exact page structure, and requires that the policy exist before loading the optional controller module.

The staging operator must resolve controller UID/GID from the protected controller-owner authorization and welcome UID/GID from the **actual host-welcome account**, not guesses or usernames embedded in policy. Station UID must be a *third, distinct* authorized numeric UID. Source tests use illustrative fixture numbers only. If the root-control daemon has not been configured for the actual controller owner UID, the inspector cannot be enabled by weakening the root socket; return controller UNAVAILABLE.

**Source pin ≠ byte attestation.** The host-install owner must install exact reviewed packages/aggregate, compare the actual on-host loaded Python origins and `sha256sum` bytes for `application_progress_broker.py`, `application_progress_client.py`, `application_progress.py`, `role_broker.py`, their relevant imports and wheel RECORD against corresponding blobs extracted from the pinned Git tree. Verify installation path/parents are root-controlled and the console process cannot rewrite imports or replace `PYTHONPATH`; reject mismatches or mutable source. The eventual protected policy must cite the reviewed **post-integration** commit rather than inheriting the earlier station profile pin (`03c847ec...`). Do not claim a Git SHA string in a policy proves the installed code. Client and broker must use a compatible exact source selection.

### Rootfs bootstrap versus selected application

The current host-welcome Phase 2 `host-welcome-progress --live` is a **one-shot native console/bootstrap reader** using the official host-install read-only welcome socket. Native host-install must affirm whether it runs in that host context, or has become a selected Zog application with a private `pivot_root` view. If native bootstrap, allow it to connect to the **one** dedicated root-controlled host socket and policy; no separate application host-bind capability is implied. If selected/pivot-root, host-install and box-control must first define an exact scoped read-only host-binding capability **only** for that authorized welcome program, with its frozen selected-runtime identity, protected grant and guest destination. Do not bind arbitrary host `/run`, mount `STATE`, expose station's socket, root-control's socket, or borrow station-access's host binding. Do not put socket path, UID or mount source in browser/client parameters.

### Ordering and fallback

`host-install-welcome.socket` and getty/console remain independent of controller, root-control, graphics, image-build, networking, station and the optional progress socket. The new observer must be optional, socket-activated and **not** a `Requires=`, `BindsTo=` or failed dependency of getty, host-install bootstrap, or login. A controller timeout, absent package, missing/invalid root policy, old boot ID, wrong SO_PEERCRED, socket activation failure, or stale/prohibited page makes the controller section **UNAVAILABLE**; separately authenticated host-install headless progress continues. No automatic background polling, service restart loop triggered by a console read, GUI launch, greetd/PAM/logind role, generation activation, host reboot or privileged state mutation.

### Source regression matrix / remaining native gate

The controller's CI includes synthetic checks for correct exact listener, swapped station listener, path owner/mode mismatch, symlink/writable ancestor, missing explicit role flag, UID mismatch before parsing and forbidden operations. Root-control CI includes wrong host-welcome/station UIDs against the general socket. Source CI tests do **not** prove the actual uid/gid, systemd socket creation, ACL/SELinux, installed package integrity, bind visibility, executable path, reboot/getty fallback or runtime invocation.

host-install must stage the dedicated .socket and .service and protected JSON from authoritative numeric sources and independently validate the installed wheel bytes; station profile and host-binding policy stay byte-for-byte unchanged. Host-deploy Work later performs native multi-UID grants, wrong-peer and exact-server UID tests, unit/socket ancestry, LSM policy, reboot/restart and getty headless fallback. Until then **no production observer cutover is authorized**. Image-build's separate attempt/publication observer and greetd remain independent NO-GO dependencies.

**Next handoff to host-install:** consume the `--observer-role host-welcome` contract, pin exact reviewed controller source commit and byte inventory, stage separate units + policy and return the source commit, protected account/grant IDs and native acceptance plan. Do not make unrelated controller or local-login changes.
