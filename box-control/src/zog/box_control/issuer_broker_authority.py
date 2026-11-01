"""Independent server-side selected issuer admission/revocation gate.

The caller's earlier successful host-deploy verification is not a capability.
The fixed, independently installed root-owned 0444 projection is reread on
*each* broker admission. A final or pending immutable revocation denies before
a selected operation reaches box-control's lifecycle mutation API.

This checks the admission instant; revocation occurring AFTER admission while
systemd is launching requires a separate root-owner serialization protocol.
The native timer remains disabled until that protocol and the installer
receipt/commit binding are independently accepted.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from .role_broker import RoleBrokerError

ROOT_PATH = "/etc/zog/host-deploy/certificate-issuer-broker-authority.json"
REVOCATION_NAME = "certificate-issuer-broker-revoked.json"
PENDING_REVOCATION_NAME = ".pending." + REVOCATION_NAME
SCHEMA = "zog-host-deploy-selected-issuer-broker-authority-v1"
FIELDS = frozenset({
    "schema", "host_id", "installation_id", "state_volume_id",
    "certificate_id", "hostnames", "policy_sha256", "host_policy_digest",
    "issuer_lifecycle_socket", "controller_uid", "caller_uid", "issuer_uid",
    "issuer_gid", "storage_id", "grant_id", "box_control_revision",
    "consumer_binding_sha256",
})
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")


def _deny():
    raise RoleBrokerError(
        "selected issuer broker authority unavailable or revoked",
        code="authority-revoked",
    )


def _no_revocation(directory_fd):
    # Even a partial, symlinked or unreadable marker is a terminal denial.
    for name in (REVOCATION_NAME, PENDING_REVOCATION_NAME):
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            _deny()
        _deny()


def _root_projection():
    """Read *only* the canonical protected projection with retained dirfds.

    No caller-selected directory, path, or authority digest is accepted.
    Each root-owned ancestor is checked; leaf must be a single-linked 0444
    regular file. Recheck both revocation names after the descriptor read.
    """
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in ("etc", "zog", "host-deploy"):
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY |
                os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory,
            )
            os.close(directory)
            directory = next_fd
            metadata = os.fstat(directory)
            if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o022):
                _deny()
        _no_revocation(directory)
        leaf = os.open(
            Path(ROOT_PATH).name, os.O_RDONLY | os.O_NOFOLLOW |
            os.O_CLOEXEC, dir_fd=directory,
        )
        try:
            info = os.fstat(leaf)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o444
                    or info.st_nlink != 1 or not 0 < info.st_size <= 8192):
                _deny()
            raw = os.read(leaf, 8193)
            if len(raw) != info.st_size:
                _deny()
        finally:
            os.close(leaf)
        _no_revocation(directory)
    except OSError:
        _deny()
    finally:
        os.close(directory)

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _deny()
            value[key] = item
        return value
    try:
        return json.loads(
            raw, object_pairs_hook=unique,
            parse_constant=lambda _: _deny(),
        )
    except (UnicodeError, ValueError, TypeError):
        _deny()


def require_active_authority(*, authorized_uid):
    """Broker-side admission, independent of the host-deploy client guard."""
    record = _root_projection()
    if (type(record) is not dict or set(record) != FIELDS
            or record.get("schema") != SCHEMA
            or type(authorized_uid) is not int
            or type(record["controller_uid"]) is not int
            or record["controller_uid"] != os.geteuid()
            or not 1000 <= record["controller_uid"] < 2**31
            or type(record["caller_uid"]) is not int
            or record["caller_uid"] != authorized_uid
            or not 1000 <= authorized_uid < 2**31
            or record["caller_uid"] == record["controller_uid"]
            or type(record["box_control_revision"]) is not str
            or _HEX40.fullmatch(record["box_control_revision"]) is None):
        _deny()
    return True
