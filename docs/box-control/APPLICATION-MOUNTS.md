# Immutable software and writable application data — pass 19

Contract: `zog-mounts-v1`. The application declaration syntax is unchanged:
`persistent`, `writable_mounts`, and per-program `mounts` retain their meanings.

Image-build writes software while constructing an unpublished generation.
Publication freezes that generation; upgrades create another generation. No
running or retained published generation is upgraded in place. Box-control
selects one immutable software generation per runtime. It does not create
partitions or layer software images.

## Mount policy

The selected root is supplied through RootDirectory with ProtectSystem=strict.
Ordinary data directories are explicitly overlaid with writable BindPaths.
Workspace sockets and authorization retain their separate controller-owned
read-only/client and writable/desktop policies. PrivateTmp and MountAPIVFS remain
in force. No writable exemption is added for the generation source itself.

Ordinary data mounts cannot cover `/`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`,
`/boot`, `/opt`, `/proc`, `/sys`, `/dev`, `/run/zog-workspace` or
`/tmp/.X11-unix`. Ancestors that would hide these facilities are also rejected.
Whole `/etc`, `/var` and `/run` overlays are rejected; specific data directories
beneath them can be declared where they do not conflict with reserved paths.
`/tmp` and `/var/tmp` retain the existing private temporary-directory policy.
Mount destinations must be canonical absolute paths, nonoverlapping and not
redirected through symlinks. Guest absolute symlinks are resolved inside the
selected generation, never against the host root. The image must contain empty
directory mount targets. Box-control refuses missing targets rather than
creating them inside the published image. Controller data sources must not be
redirected through host symlinks.

Validation runs during declaration parsing (where possible), preparation/launch
binding, recovery before replacement stops, and root-control dispatch. Program
entry-point executables cannot be inside writable application mounts. This does
not analyze scripts, arbitrary interpreter imports or package installation
behavior. Packaging remains responsible for placing installed application code
and dependencies in the software generation. Build execution remains a separate
interface with its existing disposable writable build roots.

## Durable mount inventory

New program runtime references contain `mount_inventory`; operation journals
include it through their authoritative runtime snapshots. Recovery compares the
reconstructed inventory against this saved value before stopping replacement
targets. Missing persistent sources are not recreated during recovery.

An inventory contains:

- `schema: zog-mounts-v1`;
- `software: {root, access: read-only}`; generation identity is carried by the
  enclosing runtime/operation;
- `mounts`: entries with `source`, `destination`, `access`, `kind`, `lifetime`,
  `owner_id`, and `name`;
- `facilities`: configured API filesystem and private temporary-directory paths.

Application data uses a stable persistent storage ID or a per-runtime owner ID.
Workspace entries use their desktop resource incarnation. Storage names are
relative controller-owned objects, not arbitrary caller-provided host paths.

`BoxControl.application_runtime_mounts(runtime_id)` returns:

```
{
  schema, runtime_id, generation,
  basis: "expected-launched",
  observed_mounts: "not-probed",
  programs: [{program, invocation_id, inventory, status}]
}
```

The method is read-only, uses a nonblocking shared snapshot lock and refuses
reference stores above 8 MiB. `status` is `recorded` or `legacy-unrecorded`.
Old records return `inventory: null`; they are not silently rewritten to claim
knowledge not originally saved. Existing systemd property inspection remains the
observed configuration check. This API does not claim to inspect the kernel's
live mount table.

Preflight adds a `mount-policy` check when the image provider can preview a
published generation. It checks destinations without preparing or building.

## Upgrades, retention and compatibility

Persistent storage identities survive generation changes and runtime replacement.
Existing explicit upgrade/preparation gates, failed-migration uncertainty,
cleanup protection and explicit storage deletion remain intact. Current storage
preparation protects its bound generation; successful upgrade moves that binding
to the new generation. Old generations remain protected by old active runtimes,
unfinished operations or pending cleanup until those dependencies are resolved.
Data persists independently. No new automatic data reclamation is introduced.

Writable virtual environments are a legacy software layout. Move interpreter and
installed dependencies into an image-build generation, update the application
commands and preparation revision, and perform an explicit upgrade. Existing
files are retained. Persistent mount-name changes remain unsupported; a legacy
software data object may be retained and mounted only for controlled migration
under a permitted data destination. See PERSISTENT-APPLICATIONS.md. Existing
runtimes remain inspectable and stoppable. Old prepared launches incompatible
with the new policy block for explicit recovery; they never switch configuration.

## Validation boundary

Tests inspect actual typed StartTransientUnit properties, not only Python mount
specifications. Regressions cover invalid/overlapping/redirected destinations,
missing targets, writable executable entry points, persistent data across
upgrades, durable reference reload, legacy inspection and recovery before stops.
Existing cancellation, retention and workspace regressions remain in the suite.

A gated test in test_systemd_integration.py launches an unprivileged program
against a world-writable software sentinel to distinguish read-only mount
protection from ordinary file permissions. It also writes persistent data and
reuses it with another generation. Run only on a disposable systemd VM using the
existing --run-systemd-integration and --systemd-rootfs options. It is not executed
by normal fixture tests. No actual systemd/mount/reboot acceptance is claimed by
this pass until that separate run succeeds.
