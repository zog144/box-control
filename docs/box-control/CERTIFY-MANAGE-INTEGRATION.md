# certify-manage: controller-issued private storage and trusted policy binding

**Date:** 2026-10-14
**Owner:** box-control / root-control Regular implementation
**Producer handoff:** [certify-manage](https://github.com/zog144/certify-manage/blob/unstable/handoffs/2026-10-14-box-control-root-control.md)
**Status:** source implementation; native identity/mount/reboot/ACME acceptance pending. **Live issuance NO-GO**.

## Accepted source-level contract

The *same* box-control project must provision and activate a unique
`execution_identity="application"` grant for `certify-manage` using
root-control's existing administrator-owned identity provisioning
interface. No UID/GID may be chosen by the application declaration;
no station or archive grant may be reused. Certificate private data
lives exclusively in the single persistent `certificate-data` mount
at guest `/var/lib/zog-certify-manage`.

New grant-owned persistent mount directories are created mode `0700`
under the issued UID/GID even when root-control's restrictive umask
interferes. Existing directories are never silently re-chowned or
re-chmodded on launch. Both the controller's application-storage
binding and root-control's privileged service preparation reject a
missing, symlinked, wrong-UID/GID or non-0700 managed directory. These
checks apply to *all* controller-granted persistent applications and
preserve their original grant identities.

`ApplicationStorage` already reuses the exact `storage_id` and grant
across preparation upgrades (including explicitly approved new image
generations) and runtime replacements. Changing the immutable
generation requires explicit re-preparation/upgrade, not automatic
replacement of certificate data or identities. An interrupted
preparation remains pending/uncertain until the original invocation
evidence is reconciled; neither a failed migration nor a missing
service authorizes command replay. A durable storage fault blocks
further lifecycle mutation. Existing `runtime credentials`,
`application_runtime_mounts`, request/operation records and
read-only recovery inspections are the verification interfaces.

## Narrow administrator-policy capability

A *dedicated, root-owned*, nonsecret policy source directory:

```
/etc/zog/certify-policy/
  policy.json
```

is the **only** additional permitted host origin. It is **not**
an additional general-purpose `/etc/zog/` source root. The directory
and every ancestor must be a nonsymlink, root-owned, non-group/other-
writable directory; the policy directory must be traversable by the
non-root application. `policy.json` must be an ordinary, nonsymlink
root-owned file, non-group/other-writable, readable by the executing
unprivileged identity, valid JSON with schema
`"zog-certify-manage-v1"` and at most 64 KiB. Recommended owner
modes: directory `0755`, policy file `0644`. **Never put private
DNS API tokens, Certbot accounts or TLS private keys into this
nonsecret policy.** Store those only under the private certificate-data
mount. The directory is owned by the host administrator; it must not
be writable by the app or host-discover.

The host owner's canonical
`/etc/zog/host-bindings.json` schema 1 contains exactly one
`certify-policy` binding per relevant `certify-manage` program.
A representative binding for the renewal operation is:

```json
{
  "project": "/home/zog-controller/approved-project",
  "application": "certify-manage",
  "program": "renew",
  "capability": "certify-policy",
  "source": "/etc/zog/certify-policy"
}
```

Under schema 1, this is one entry in `bindings`. A matching
`prepare` entry is separately required for preparation. Other
allowed program names in this *reserved* scope are `issue` and
`inspect`, for a future independently authorized operation
interface. **No other application or program may borrow this
capability or bind the same path under another name.** No alternate
source path, additional cert application capability or writable bind
is permitted.

The controller resolves the exact root-owned policy source, records
its directory device/inode, immutable destination
`/run/zog-host/certify-policy`, read-only mode and SHA-256 digest of
`policy.json`. root-control independently resolves that policy
again and compares the complete submitted host-binding record,
including content digest, before starting systemd. Policy removal,
content replacement, directory replacement, symlink redirection or
wrong source fails closed; the application cannot request a different
`--policy` file through its application specification. The normal
systemd `BindReadOnlyPaths` and kernel-observed mount inspection
are the next live acceptance steps.

## Exact single-program execution and unattended renewal

The producer's reviewed `integration/box-control/application.py`
declares separate `issue`, `renew` and `inspect` runtime programs.
The application **may now retain all three** in a single declaration.
The parser no longer rejects that declaration; instead, the controller
requires an **exact selected program** and an original durable request
identity whenever launching a `certify-manage` runtime. An unselected
application-wide certificate launch fails closed, including when only
one certificate command happens to be declared.

```python
request_id = control.issue_application_request_id()
request = control.request_application_launch(
    "certify-manage", program_name="renew", request_id=request_id,
)
# Or execute directly using launch_application(..., program_name="renew",
# request_id=request_id); never submit both execution modes for the same intent.
```

The runtime snapshot contains exactly `renew`, not `issue` and
`inspect`. The original prepared UID/GID, persistent certificate-data
storage ID, immutable software and policy binding are preserved. An
unresolved prior singleton execution prevents any new selected operation,
and root-control independently verifies its selection against the saved
operation, request, program/unit/command and execution grant before systemd
starts. A second request may proceed only after prior effects, cleanup,
and uncertainty are resolved. The original request ID cannot be reused to
select another program; crash recovery uses its original saved command
rather than a newly edited application definition.

See the complete [selected-program execution contract](SELECTED-PROGRAM-EXECUTION.md).
This removes the original **controller-side single-command blocker**, but
**does not yet install an automatic renewal schedule**. An approved trusted
caller/timer must use this API with a durable identity, avoid repeating
uncertain Certbot operations, and verify actual ACME account/certificate
lineage after each run. No new arbitrary-command root-control endpoint
or shared application account is required. A timer cannot itself
authorize new hostname policy or publish private keys.

Never infer certificate readiness from a systemd transient service
return or from Certbot's exit code alone. Use verified SAN, validity,
lineage and issuer evidence. Publication, consumer activation, nginx
reload and trust validation remain separate host-deploy responsibilities.

## Required host acceptance

Host-deploy must explicitly verify the exact executable and module
in the published immutable generation/software view (Certbot and
the optional Cloudflare plugin), a fresh independent numeric grant
with host NSS activation, all real/effective/saved/filesystem UIDs
and GIDs and supplementary groups through
`observe_application_runtime_credentials`, and the effective
read-only policy and private writable state mount identities through
`observe_application_runtime_mounts` and native
`/proc/<pid>/mountinfo`.

Negative cases: wrong/missing policy, symlink/file replacement,
cross-application access to certificates and tokens, wrong grant,
UID drift, nonprivate 0700 mode, vanished persistent mount,
expired/revoked owner authority, changed source image/software,
uncertain operation and unavailable STATE. Positive cases:
preparation and a *separately supported* real renewal after
controller restart, host reboot and explicit image generation
upgrade, demonstrating identical prepared storage ID, grants,
certificate lineage and retained key/account bytes.

Backup/restore must be an operator-reviewed encrypted/quiescent
snapshot of the same storage ID with the original grant and
consistent ACME account/lineage state, never a recursive host
chown, implicit empty reinitialization, or an import into station
or archive storage. No `/etc/letsencrypt` root bind is permitted.
Only after the per-command execution authority and renewal timer are
reviewed and actual ACME acceptance passes may unattended issuance
or renewal be activated.

## Focused source test commands

Using the current box-control and matching root-control source with
the pinned public dependency fixture:

```sh
python -m pytest -q \
  box-control/tests/test_certify_policy_binding.py \
  box-control/tests/test_certify_application_profile.py \
  box-control/tests/test_host_bindings.py \
  box-control/tests/test_execution_identity.py \
  root-control/tests/test_certificate_policy_binding.py \
  root-control/tests/test_private_persistent_storage.py
```

Automated exact-pinned entry:
`.github/workflows/certificate-controller-integration.yml`.
The normal public-aggregate suite is separate; passing it alone
does not validate this new integration. No privileged kernel
mounting, real Certbot execution, reboot or DNS/HTTP ACME activity
is done by these source tests.
