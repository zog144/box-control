# certify-manage renewal follow-up: exact stage selection and trusted issuer observation

**Date:** 2026-10-15  
**Owner:** box-control / root-control  
**Origin:** [certify-manage selected-program renewal handoff](https://github.com/zog144/certify-manage/blob/unstable/handoffs/2026-10-15-selected-program-renewal.md)  
**Status:** controller source implementation completed; native systemd, issuer/ACME trust and consumer integration still NO-GO.

## Controller-side stage authority

The reserved root-owned `/etc/zog/certify-policy` host-directory capability
`certify-policy` now permits the **exact** program `stage` of application
`certify-manage` in addition to `prepare`, `issue`, `renew`, `inspect`.
This is not a general host-directory grant and its read-only destination remains
`/run/zog-host/certify-policy`. The canonical administrator-controlled
`/etc/zog/host-bindings.json` schema 1 must carry a **separate** explicit
project/application/program binding for `stage`:

```json
{
  "project": "/approved/controller/project",
  "application": "certify-manage",
  "program": "stage",
  "capability": "certify-policy",
  "source": "/etc/zog/certify-policy"
}
```

No other application, program, writable bind, alternative source or additional
capability is allowed for stage. The existing host-policy digest and source
device/inode are bound to the controller service and independently checked
in root-control's privileged systemd admission.

A selected `stage` runtime must have **a new durable request ID** and
`program_name="stage"`, use the same approved persistent storage and
application UID/GID as the prior issuer, be the only unresolved runtime
for that managed singleton, and pass root-control's original operation,
request, command, runtime reference, policy and execution-grant checks.
Root-control expressly rejects a certificate `stage` start that lacks
`selected_program`.

The consumer's current review-only `certify-manage` application template
does **not yet declare stage**. The certify-manage owner must add a
`program(name="stage", command=(..., "stage", "--state", ...,
"--policy", ...), mounts={"certificate-data": "/var/lib/zog-certify-manage"})`
declaration and retain the existing root-owned policy destination. Stage
is an issuer-private generation; it cannot export certificate material,
reload nginx or claim ingress readiness.

## Read-only issuer observation path

A separate systemd socket-activated `zog.box_control.issuer_observer`
process runs under the **existing non-root trusted controller project owner**.
It is bound at service startup to one absolute canonical project,
application `certify-manage`, and **one explicit authorized host-deploy
caller UID**, distinct from the project owner UID. Authenticate with
Linux `SO_PEERCRED` *before* processing a request. Only one request shape
is allowed over this socket:

```json
{
  "schema": 1,
  "operation": "observe-issuer",
  "request_id": "EXACT-SAVED-REQUEST"
}
```

The wire schema/version must match `zog.box_control.role_broker.SCHEMA`
in the installed revision. There is **no** write, shell execution,
arbitrary application selector, path, policy override, grant override,
UID/GID override, or caller-supplied JSON issuer result.

The host-deploy client uses
`IssuerObserverClient(<systemd-socket-path>).observe_issuer_request(request_id)`.
A project-controller owner can also use
`BoxControl.observe_issuer_request(request_id)`; that calls the
existing root-control privileged socket through a new dedicated read-only
`issuer_observe` operation on root-control's bounded inspection lane.
**Do not grant host-deploy direct access to the privileged root-control
socket or to issuer-private 0700 storage.**

Root-control derives all evidence independently from the requested original
persisted `certify-manage` selected `issue` or `renew` launch, its saved
`operation` and `runtime reference`, original program selection,
controller-issued persistent storage ID, protected application UID/GID
grant and canonical root-owned host policy. It reads only the selected
certificate's private bounded `operations/<certificate-id>.json` issuer
journal, via no-symlink directory/file descriptors with exact owner/GID,
directory 0700, file private and single-link checks. The parent private
storage is never made accessible to host-deploy.

The schema `zog-trusted-issuer-observation-v1` returns only:
request ID, saved runtime ID, exact program, storage ID, grant ID, numeric
UID/GID, canonical policy SHA256, protected host-policy content digest,
approved certificate ID, journal update timestamp, journal outcome and
certificate fingerprint SHA256. No DNS credential, ACME account ID,
raw certificate, key bytes, command-line secret or arbitrary source file
content is exposed.

**Status meanings:**

- `ongoing`: selected operation/request or recorded runtime is not yet
  conclusively terminal with successful cleanup; do not stage.
- `stale`: issuer evidence is absent/older than the selected request,
  describes a different program, or lacks proven terminal success;
  do not stage.
- `uncertain`: issuer journal, controller request or runtime reports
  uncertain/failed effects; do not retry blindly or stage.
- `journal-ready`: the issuer's private journal has matching
  program/policy/fingerprint and an update after the original request,
  the controller request is satisfied, and **the saved runtime is terminal,
  successful and cleaned**. This is evidence of a completed issuer
  operation, **not an independently CA-validated X.509 certificate,
  nginx publication, CA-verifying TLS handshake or serving readiness**.

The response always includes `tls_ready: false`. Host-deploy must
independently use the reviewed certificate-inspection/lineage and
host-owned delivery bridge and confirm nginx CA/SNI/fingerprint before
marking a certificate delivered. Do not equate `journal-ready` to
deployment acceptance.

## Example systemd service interface

The owner must provide a reviewed socket unit (Unix stream, protected
directory, readable only to the exact host-deploy observer UID) and a
matching service unit roughly equivalent to:

```ini
[Service]
Type=exec
User=<trusted-non-root-controller>
ExecStart=/path/to/approved/python -m zog.box_control.issuer_observer --project /approved/controller/project --authorized-uid <host-deploy-uid>
NoNewPrivileges=yes
```

The actual units must use the pinned immutable installed package,
approved controller root-control socket access and verified project owner.
The observer never supplies an active lease, calls cert-manager `inspect`,
starts a certificate command, automatically retries an uncertain issuer
operation, receives the private key or publishes a TLS endpoint.

## Regression and native acceptance

The pinned `.github/workflows/certificate-controller-integration.yml`
gate runs the read-only broker permission tests, root-control trust
journal observations, stage host-policy binding checks and privileged
stage-selector denial together with established one-program execution,
recovery, mount and private storage tests.

**Native integration remains required**: adopt host-deploy's journaled
renewal caller; review the issuer-observer socket configuration and peer UID;
test original request binding after lost response, kill and reboot;
run real Certbot 5.8.0 staging CA tests and inspect true X.509/lineage
consistency; demonstrate missing/revoked stage policy denial; run
`renewed -> selected stage -> certificate delivery -> nginx reload ->
CA-verifying SNI/fingerprint` with verified rollback and no token exposure.
For `unchanged`, no stage/reload unless independently required.
Keep production issuance and HTTPS cutover **NO-GO** until these are accepted.
