# box-control Python package

This directory contains the installable `box-control` package,
the `evaluate` launcher, controller documentation and tests.

Start with the [repository README](../README.md) for installation with the pinned
image-build dependency, project layout, CLI and Python usage, state and recovery,
and test commands. The [persistent application guide](../docs/box-control/PERSISTENT-APPLICATIONS.md)
covers shared writable storage, explicit preparation and rootfs upgrades.

Install from the repository root using `pip install -e './box-control[test]'`
after making its recorded image-build dependency available. The source checkout
is not itself a configured deployment project or a usable root filesystem.
