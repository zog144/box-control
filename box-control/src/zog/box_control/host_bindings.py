"""Administrator-granted, read-only live host-directory capabilities.

The policy is intentionally outside the unprivileged Zog project. A capability
binds the *directory*, not an individual lease file inode, so a trusted host
service's atomic replacement of files within the directory remains observable.
The policy never authorizes host-control RPCs, writable binds or ownership transfer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .errors import RuntimeOperationError

POLICY_PATH = Path("/etc/zog/host-bindings.json")
_CAP = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_PROGRAM = re.compile(r"[A-Za-z0-9-]+\Z")
_MAX_BYTES = 65536
_MAX_BINDINGS = 128
_ALLOWED_SOURCE_ROOTS = ("/run/zog-host-export/", "/var/lib/zog-host-export/")
# A deliberately fixed, root-owned, nonsecret policy source. The general host
# export roots above must not be used as an alternative to this authority.
_CERT_POLICY_SOURCE = "/etc/zog/certify-policy"
_CERT_POLICY_CAPABILITY = "certify-policy"
_CERT_APPLICATION = "certify-manage"
_CERT_PROGRAMS = frozenset({"prepare", "issue", "renew", "inspect"})
_MAX_CERT_POLICY_BYTES = 65536


def _safe_directory(path: str, *, label: str) -> Path:
    if not isinstance(path, str) or not path.startswith("/") or "//" in path:
        raise RuntimeOperationError(f"invalid {label} directory path")
    value = Path(path)
    if str(value) != path or ".." in value.parts or value == Path("/"):
        raise RuntimeOperationError(f"invalid {label} directory path")
    current = Path("/")
    for part in value.parts[1:]:
        current = current / part
        try:
            meta = current.lstat()
        except OSError as exc:
            raise RuntimeOperationError(f"missing {label} directory: {current}") from exc
        if not stat.S_ISDIR(meta.st_mode):
            raise RuntimeOperationError(f"{label} directory is redirected or not a directory: {current}")
    return value


def _require_privileged_policy(path: Path) -> None:
    # Checking each ancestor denies privilege escalation via a writable directory
    # even when the final JSON happens to be owned by root.
    if path != POLICY_PATH:
        raise RuntimeOperationError("host binding policy location is not canonical")
    for directory in (Path("/"), Path("/etc"), Path("/etc/zog")):
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_mode & 0o022):
            raise RuntimeOperationError("host binding policy parent is not privileged")
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or metadata.st_mode & 0o022):
        raise RuntimeOperationError("host binding policy is not administrator-owned")


def _certify_policy_digest(source: str) -> str:
    """Check the entire trusted policy source, never follow an attacker symlink.

    This is a *nonsecret* administrator-issued intent file. Private DNS API
    tokens must live solely in the application's 0700 storage, not here.
    """
    if source != _CERT_POLICY_SOURCE:
        raise RuntimeOperationError("certify-policy must use the exact approved source")
    directory = _safe_directory(source, label="certificate policy")
    # All ancestors to /etc/zog are root controlled and not writable by an
    # unprivileged service. The policy dir is neither replaceable nor writable.
    for parent in (Path("/"), Path("/etc"), Path("/etc/zog"), directory):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise RuntimeOperationError("certificate policy directory is not administrator controlled")
    if not directory.stat().st_mode & stat.S_IXOTH:
        raise RuntimeOperationError("certificate policy directory is not traversable by application")
    document = directory / "policy.json"
    try:
        descriptor = os.open(document, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeOperationError("certificate policy file is missing or redirected") from exc
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or not info.st_mode & stat.S_IROTH):
            raise RuntimeOperationError("certificate policy file must be root-owned and publicly readable, not writable")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            content = stream.read(_MAX_CERT_POLICY_BYTES + 1)
        if len(content) > _MAX_CERT_POLICY_BYTES:
            raise RuntimeOperationError("certificate policy exceeds size limit")
        try:
            raw = json.loads(content)
        except (UnicodeError, ValueError) as exc:
            raise RuntimeOperationError("certificate policy file is not valid JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema") != "zog-certify-manage-v1":
            raise RuntimeOperationError("certificate policy schema is invalid")
        # File replacement can only be performed by the root policy owner.
        # Nonetheless bind the exact bytes used in the controller's decision.
        return "sha256:" + hashlib.sha256(content).hexdigest()
    finally:
        os.close(descriptor)


def resolve(project_root: Path, application: str, program: str) -> tuple[dict, ...]:
    """Resolve exactly the host grants for a program; missing policy grants none.

    The privileged mechanism independently re-reads and compares these immutable
    launch-time bindings against the current administrator policy.
    """
    policy = POLICY_PATH
    certificate_program = application == _CERT_APPLICATION and program in _CERT_PROGRAMS
    if not policy.exists() and not policy.is_symlink():
        if certificate_program:
            raise RuntimeOperationError("certificate policy host binding is absent")
        return ()
    try:
        _require_privileged_policy(policy)
        with policy.open("rb") as stream:
            content = stream.read(_MAX_BYTES + 1)
        if len(content) > _MAX_BYTES:
            raise RuntimeOperationError("host binding policy exceeds size limit")
        raw = json.loads(content)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeOperationError(f"host binding policy unavailable or invalid: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema", "bindings"} or raw["schema"] != 1:
        raise RuntimeOperationError("unsupported host binding policy schema")
    entries = raw["bindings"]
    if not isinstance(entries, list) or len(entries) > _MAX_BINDINGS:
        raise RuntimeOperationError("host binding policy exceeds entry limit")
    selected = []
    seen = set()
    for item in entries:
        if (not isinstance(item, dict)
                or set(item) != {"project", "application", "program", "capability", "source"}):
            raise RuntimeOperationError("invalid host binding policy entry")
        proj, app, prog, cap, source = (item[k] for k in
            ("project", "application", "program", "capability", "source"))
        if (not isinstance(proj, str) or not proj.startswith("/")
                or str(Path(proj)) != proj or ".." in Path(proj).parts
                or not all(isinstance(n, str) and _PROGRAM.fullmatch(n)
                           for n in (app, prog))
                or not isinstance(cap, str) or not _CAP.fullmatch(cap)):
            raise RuntimeOperationError("invalid host binding policy identity")
        if proj != str(Path(project_root)) or app != application or prog != program:
            continue
        if cap in seen or len(selected) >= 8:
            raise RuntimeOperationError("duplicate or excessive host capabilities")
        seen.add(cap)
        # This reserved policy cannot be borrowed by station, archive, other
        # applications or additional programs, even through a root-written
        # capability entry. There is no general /etc/zog host bind allowlist.
        is_cert_binding = (cap == _CERT_POLICY_CAPABILITY
                           or source == _CERT_POLICY_SOURCE)
        if is_cert_binding:
            if (cap != _CERT_POLICY_CAPABILITY
                    or app != _CERT_APPLICATION
                    or prog not in _CERT_PROGRAMS
                    or source != _CERT_POLICY_SOURCE):
                raise RuntimeOperationError("certificate policy capability scope disagrees")
            policy_digest = _certify_policy_digest(source)
        else:
            if not any(source.startswith(prefix) for prefix in _ALLOWED_SOURCE_ROOTS):
                raise RuntimeOperationError("host capability source is outside permitted host roots")
            policy_digest = None
        src = _safe_directory(source, label="host capability")
        meta = src.stat()
        entry = dict(
            capability=cap,
            source=str(src),
            destination="/run/zog-host/" + cap,
            access="read-only",
            source_device=meta.st_dev,
            source_inode=meta.st_ino,
        )
        if policy_digest is not None:
            entry["policy_digest"] = policy_digest
        selected.append(entry)
    if certificate_program and (
            len(selected) != 1
            or selected[0]["capability"] != _CERT_POLICY_CAPABILITY):
        raise RuntimeOperationError("certificate policy requires exactly one approved read-only capability")
    return tuple(sorted(selected, key=lambda item: item["capability"]))
