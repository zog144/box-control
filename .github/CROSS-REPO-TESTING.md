# Public CI and companion verification

Public CI runs local controller tests and packaging checks without private
repository tokens. The historical secret-backed checkout of root-control
and image-build is intentionally not copied. Once approved public revisions
are available, add pinned cross-repository acceptance and zero-skip checks.
Live systemd and reboot behavior requires separately authorized disposable
host acceptance.
