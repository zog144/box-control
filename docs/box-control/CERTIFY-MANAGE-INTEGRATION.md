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

## Unsafe prototype and renewal authorization blocker

The producer's current
`certify-manage/integration/box-control/application.py` is **review
only** and declares `issue`, `renew` and `inspect` as simultaneously
started application programs. Box-control's current API launches all
programs in an application together. It **does not** independently
select one CLI operation. In particular, this is unsuitable for an
unattended renewal timer or for host-triggered live issuance.

The shared parser now explicitly rejects a
`certify-manage` application declaring more than one runtime
program with a clear contract error. This prevents accidental
activation of the known unsafe prototype and is not a replacement
for a properly implemented single-operation lifecycle.

**Required next design/producer work:** supply a reviewed
single-operation invocation mechanism bound to one durable
controller request/operation identity, precisely one mutually
exclusive operation (`issue`, `renew`, `inspect`, or `recover`)
and its already-issued app grant and prepared storage ID. Concurrent
issuance and renewal must fail closed, including restarts, lost
replies, old process observations, journal uncertainty and reboot.
The owner-selected scheduler/caller must run with a strict approved
cadence and no additional privilege to change hostnames, policy,
storage or certificate consumers. Repeated ticks must reconcile the
original request and Certbot operation record, never blindly repeat
an uncertain call. A timer alone on the present application-wide
launch API would be unsafe; **no unattended scheduler has been
enabled**. Role election is irrelevant and no new root-control
arbitrary-command endpoint should be added.

Never infer certificate readiness from a systemd transient service
return or from Certbot's exit code alone. Use the certificate manager's
verified SAN/validity/lineage/issuer status; publication, consumer
activation, nginx reload and trust validation remain exclusively
with reviewed host-deploy actions.

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
