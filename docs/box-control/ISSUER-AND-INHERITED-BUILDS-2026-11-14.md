# Combined issuer and inherited-identity finite builds

This revision retains the box-control issuer broker, selected-program lifecycle,
certificate-policy bindings and associated tests from the reviewed issuer
PR #15 source. It changes only finite compilation: the canonical build request
contains no execution UID/GID and callers cannot select a different Unix user
or group. The existing execution context (including UID 0) is allowed. No
special compiler account, account switch, root rejection or substitute identity
grant is introduced.

The selected root-control companion must likewise contain both issuer
observation/inspection and ordinary finite-build execution. This source is
not proof of native installed issuance, nor does it enable an issuer or
compilation gateway service.
