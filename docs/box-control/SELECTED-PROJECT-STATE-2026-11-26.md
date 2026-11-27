# Root-run controller state for an explicitly selected Project

**2026-11-26 · box-control Regular · source-only correction; native admission separately reviewed**

## Problem and intended Project

A selected compiler Project at `/home/certbot-build-owner/certbot-seed` is owned by UID 24201, but the approved controller execution ran as root (so `Path.home()` was `/root`). The old `Project.require_state_allowed()` rejected any mutation outside the controller process's home; `Project.ensure_state()` independently returned without creating Project state. This stopped the Fonts controller before build stages. It did **not** establish that the approved Project was invalid or require moving it under `/root`.

**The selected Project remains `/home/certbot-build-owner/certbot-seed`.** Its ownership, source and state are not migrated, and retained request **#63** is not restarted or replayed.

## Narrow new admission rule

A controller constructed from an **explicit** `Project(path)` may mutate its own `Project.state_dir` outside its home **only** if `os.geteuid() == 0`. The accepted path is the previously selected, canonical Project root, not a new path derived from `/root`, `Path.home()`, the request payload or an untrusted symlink target. `Project.discover()` explicitly marks its result as inferred and continues to require the usual home-bound behavior. A non-root process retains the home restriction.

For the explicit root-only exception, `require_state_allowed()` verifies:
- the selected path is not `/` and its original absolute spelling equals the canonical resolved path (no redirected path);
- every selected Project ancestor exists as a real directory (no symlink traversal);
- each directly managed state directory is either absent (eligible for ordinary creation) or already a real directory contained beneath the selected Project;
- the existing `box-control.lock`, `state.json`, `mutation-incomplete.json` and `session-recovery-pending.json` files, if present, are real regular files, not redirection paths.

`ensure_state()` now calls the **same** admission guard rather than quietly skipping Project state creation outside home. Existing local durable creation and fsync barriers remain unchanged. No `chown`, `chmod` repair, new Unix account, Project movement or alternate state directory is performed. This ordinary path-integrity check does not claim to defeat an adversarial trusted Host administrator racing filesystem changes; it is a guard against unintended path selection and symlink redirects.

Existing completed/uncertain request records, interrupted-operation markers and request identity remain in their original selected Project. The admission change must **not** remove, rewrite, retry or silently recover request #63.

## Source tests and handoff to Work

The focused `.github/workflows/selected-project-state.yml` tests the external explicit root-only allowance, non-root and discovery rejection, canonical path and state redirection denial, preservation of owners, and unchanged interrupted-operation marker/recovery interlock. It runs Python 3.12 and 3.13 using the same pinned Zog namespace support as existing box-control tests. The usual public-source and inherited-peer checks must remain green.

**Native Work remains separate:** Read the exact reviewed box-control source selected by the controller installation, its currently admitted Project and `state/` paths, actual effective UID, symbolic links/real directory ancestry, permissions and retained request #63 records **read-only first**. An installed candidate may then be source-accepted and staged using normal deployment procedures. Do not change the Project location to `/root` or take ownership from UID 24201 as a workaround. This fix does not independently authorize replaying #63, activating dispatch, or issuing a new SSM build command.

The inherited-peer build execution contract and root-control/image-build compatibility gates are independent of this filesystem-path allowance; it does not grant a new remote-execution identity or bypass those checks.
