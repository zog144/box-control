# Console-first application progress and local-seat boundary — box-control/root-control response

**Date:** 2026-10-16  
**Owner:** box-control / root-control Regular  
**Origin:** [host-welcome coordinated handoff](https://github.com/zog144/know-file/blob/b135b4f2f45991e0fb655aacf72346d7c3086437/host-welcome/handoffs/console-first-application-progress-and-greetd-2026-10-16.md)  
**Status:** progress read-only source and synthetic tests; local login role **fenced**, not implemented; native PAM/systemd/seat acceptance **NO-GO**.

## Handoff A result

### Local display versus remote workspace

Existing `zog-workspace-v1` is **not a local-seat contract**. Its X11/VNC workspace, authorization directories and application attachments cannot claim ownership of a local VT, `seat0`, DRM/KMS device, logind session, PAM authentication or physical input. The controller must never present an existing TurboVNC display as an available local login.

Versioned `zog-local-session-capabilities-v1` now returns an **honest inert baseline**:

```json
{
  "schema": "zog-local-session-capabilities-v1",
  "supported": false,
  "status": "not-implemented",
  "physical_seat": "unavailable",
  "virtual_seat": "unavailable",
  "login_role": "not-authorized",
  "workspace_v1_is_local_seat": false
}
```

This response is read-only, makes no systemd/logind/device calls, and carries no authority to install or start anything.

### Reserved login-manager role: proposed v1, not enabled

The selected login-manager role should ultimately be `zog-local-login-manager-role-v1`, **a dedicated reviewed privileged boundary**, not a generic request from a web page, desktop workspace, welcome console or ordinary application. For first native acceptance, use a *disposable virtual graphical session* with unprivileged X11/Xvfb/i3/Firefox before attempting PAM and a physical seat. Virtual graphical rendering **does not** prove greetd, PAM or seat security.

**Split the services:**

1. **Authentication/session broker (greetd candidate)**: may require privileged PAM and logind operations, reviewed root-controlled identity, dedicated approved host account/privilege profile, protected PAM service name/config, exact systemd dependencies and explicit seat/VT assignment. It must not inherit the ordinary application grant as a substitute for its different trust domain. No general root/shell/DBus/tty proxy or `User=0` relaxation is authorized.
2. **Greeter** (agreety candidate): minimally privileged, exact installed package/view, pinned greeter command and no ambient root permissions; user credentials enter only the PAM-controlled authentication conversation, never box-control request JSON, environment, browser IPC, application logs or UI progress.
3. **Desktop/X11/Wayland compositor/window manager/browser**: separate optional ordinary application software views and controlled runtime identities; no PAM secrets, private greeter state or implicit screen/VT access. Browser never gets a login-manager capability. Xauthority and Wayland socket location must be generated/verified by an authenticated session owner rather than request fields.
4. **Host console/getty**: independent non-graphical fallback; failure to build, publish, launch, authenticate or render a graphical login must leave host installation and existing console operable.

**Admission requirements before opening this role:** root-protected operator approval of exact `greetd` image generation *and* immutable application software-view manifest/digest; exact installed program hash/argv/environment, config and PAM service; fixed backend principal and greeter user; audited PAM modules, no hidden environment expansion; inspected logind seat/VT and device ACL management; bounded approved session type/X11 or Wayland resources; host-owned socket/SELinux labels and ownership; original request ID plus recorded runtime/invocation and exit/cleanup; explicit stop/restart/reboot and rollback to console. A web/console caller cannot provide arbitrary DISPLAY, XAUTHORITY, XDG_RUNTIME_DIR, DRM device, seat, VT, session command, login account or password as an execution selector.

Root-control now **rejects reserved `greetd`, `seatd`, `login-manager`, `display-manager`, `local-login-manager`, `greetd-daemon` application/program names** in its own privileged service admission path. This defense-in-depth fence does **not** constitute a complete detection mechanism for every differently named privileged login program and does not block unrelated systemd users outside Zog. Do not deploy a renamed greetd service as a bypass. The future selected role needs real positive authentication/authorization, not name matching.

### Future `zog-local-session-status-v1` (not yet implemented)

A future read-only local-session producer must bind: original approved role/selected runtime and current boot/systemd invocation; pinned application/software view and verified rootfs; *physical seat/VT* identity **or** explicitly *virtual display* identity, never conflated; logind `session-id` (after actual authentication); observed socket/display owner and permissions; exact greeter/window-manager/browser runtime refs; status `unknown | preparing | awaiting-login | authenticated | running | blocked | failed | uncertain | stopped`; observed UTC time and evidence freshness; and shutdown/cleanup/reboot status. Avoid usernames, passwords, PAM environment, X cookies, private sockets/paths, raw commands and kernel device identifiers in general-visibility replies. Only the session owner, not browser input, can attest login success or select VT/seat.

Shutdown must stop browser/workspace children, greeter session, display/compositor and privileged seat/login manager in a validated order; ownership and immutable software pins remain durable until cleanup is proven. On interruption, old invocation/seat/session conflicts block fresh admission rather than silently reusing display numbers. Headless `not-requested` is a valid end state and must not delay host-install completion.

## Read-only controller progress source (implemented)

`zog.box_control.application_progress.observe_application_progress()` exposes bounded `zog-application-progress-v1` pages of **saved finite build jobs** and **controller application-runtime references**. `application_progress_broker` exposes only `snapshot` and inert `local-session` over a **dedicated systemd-activated local AF_UNIX socket**. An exact configured peer UID is checked with Linux `SO_PEERCRED` **before** request parsing; replies have a bounded frame and redacted failure, no arbitrary path, file, PID, program, command, start, cancel or build operation. The separate consumer client verifies expected controller-owner peer UID, response limits, schema, status/message enums and timestamp freshness. A caller must learn fixed socket/UID from protected host installation configuration, not web query parameters.

**Source identity:** the controller service runs under the existing non-root *project owner*, never host-welcome's user and never root. The future root-installed systemd service/socket must be pinned to the reviewed interpreter/package code and the exact canonical project path. The socket path and access ACL/mode must be administrator-controlled; give access to only an approved host-welcome/host-install role and separately authorized remote UI adapter, not all local users. Client binding to server UID is necessary but not sufficient without protected socket ancestry and reviewed installed service unit.

**Projection limits:** up to four build jobs plus four runtime references per page; independent explicit next-build/next-runtime cursors and `has_more` flags. `snapshot_id` is unique to each **non-atomic page**, never a claim that build and runtime records share an atomic transaction. `host_id` and `installation_id` are *null* until an independently authenticated installer/HostIdentity producer binds them; no inferred host identity. `boot_id` is present only when the controller's boot source is structurally valid. Attempt/stage/package/command associations remain *null* in this producer: the image-build owner joins those separately with actual authenticated attempt/checkpoint evidence. No source argv, environment, paths, credentials, stdout, arbitrary log strings or unapproved message text are projected.

**State rules:** `completed + process_cleanup_complete + outcome=success` is only a `building/succeeded` command, **not publication or application ready**. `running` build commands are not percent-complete. `unknown/cancelling/cleanup` become uncertain; a missing/pruned/inconsistent job record becomes unknown rather than success. A saved app `running` state becomes `launching/running` only after a same-boot `observe_application_runtime()` reports matching active program observation; previous-boot or unavailable/integrity-fault remains unknown. Terminated and fully cleaned runtimes with recorded successful members may show `launching/succeeded`, not browser/window/display readiness. No implicit `not-requested` entries are fabricated: absence of a build job cannot prove there was an application installation request. Host-install and image-build decide requests/publication separately.

**Important:** the built-in runtime inspection uses a bounded privileged transport observation and may be slow for four runtimes; no automatic refresh storm or unrestricted polling. The broker starts no job and never retrieves logs. Existing `list_build_jobs`, `inspect_build_job`, `build_job_logs`, `application_runtime` and `observe_application_runtime` remain independently authorized and unchanged.

### Example approved service topology (NOT a deployable unit)

- Root-installed socket at an exact protected `/run/zog/...` location; DAC only allows fixed observer UID. Socket activation passes one inherited listening socket; systemd service `User=` is the existing controller project owner and **not root**.
- Reviewed `python -m zog.box_control.application_progress_broker --project <fixed-controller-project> --authorized-uid <approved-consumer-uid> --root-control-socket <approved-existing-read-only-transport>`; all arguments are installed once under root-controlled configuration, not accepted from socket requests.
- Client `ApplicationProgressClient(fixed_path, controller_uid=<controller-owner-uid>)` uses the exact expected peer identity. It must be wrapped in a **host-install-controlled authorization** for pre-install/console views and an independent station-access authorization for remote users.
- If socket, controller, build job or root observation is unavailable, show **unknown/unavailable**; do not auto-install, retry, rebuild, resume, start greetd or claim the graphical desktop is ready.

### Important remaining boundary / limitations

`zog-application-progress-v1` currently reports **controller producer rows**, not full image-build attempt->stage->package phases, verified publication or installer success. Those require image-build, host-install and authenticated aggregator inputs. The status schema has no implied trust from its JSON alone. The controller's current partial pages are not a durable transaction snapshot across two stores. Consumers must gracefully restart pagination when rows change and reject stale or conflicting source identities. An explicitly image-build-mapped pruned job must remain an unknown/expired reference even if the job no longer appears in the controller listing.

## Acceptance recipe

**Source:** run `box-control/tests/test_application_progress.py`, existing `test_build_jobs.py`, `test_workspaces.py`, `test_role_broker.py`; root-control `tests/test_selected_program_authority.py` and `test_root_control_systemd.py`. Inspect that read-only progress calls never invoke `submit_build_job`, `refresh_build_job`, `launch_application`, `evaluate` or any mutation. Test wrong client UID before parsing, wrong controller server UID, malformed framing, hostile fields, stale boot, unknown/pruned jobs, uncleaned success, no view inferred, and no privileged login activation.

**Native (separate, not completed):** disposable Linux with real root-installed socket and controller-owner service; actual distinct UID credentials and policy, socket ancestry and IPC denial; real root-control observation including wrong invocation/reboot/integrity fault; genuine image-build attempt view and failed publication; getty/host-welcome offline fallback. Later test disposable virtual X11 first, then physical local VT and approved PAM/logind/greetd with session cleanup/reboot. Confirm a failed GUI leaves authenticated console/getty accessible. No production login manager, graphical application or timer is enabled by this source pass.

## Cross-owner request

- **image-build:** independently signed/bound attempt/stage/package/command-to-job mapping, actual acquire/test/verify/publish status and exact software IDs; never interpret controller build success as publication.
- **host-install:** authoritative host ID/installation ID and separate installation readiness; protected observer socket deployment; retain getty fallback.
- **host-welcome:** consume via protected authenticated adapter, not public fixture JSON, display `not-requested` only from installation request authority, and never mutate sources.
- **station-access:** independent authorization to view sanitized per-role statuses; never conflate remote VNC workspace with local seat.
- **host-deploy Work:** no privilege/live seat mutations until the missing PAM/logind/image/console gates are explicitly reviewed.

**Conclusion:** G0 design and denial fence, plus the controller portion of G1 progress producer, are source-ready; G3/G4 remain blocked. No root/login-manager authority or real seat status has been granted.
