# box-control public source and dependency notices

First-party box-control software, documentation, reviewed acceptance tools and
synthetic fixtures are offered under `BSD-3-Clause OR GPL-3.0-only`; see
[LICENSE](LICENSE) and the complete [LICENSES](LICENSES/).

The separate image-build and root-control components are not vendored here.
`dbus-next==0.2.3`, setuptools, pytest, systemd, Linux and any application
software retain their own respective licenses. Redistributing built artifacts
requires reviewing the actual bundled dependencies and third-party notices.

Historical private Git history, private handoff metadata and live host records
are excluded from the new public repository. Disposable-host acceptance scripts
are not production deployment authority. The aggregate `zog` PyPI distribution
is assembled separately by release-build.
