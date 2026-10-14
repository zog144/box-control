"""Local-filesystem durability barriers, assuming storage honors fsync.

Errors preserve temporary evidence. Callers must not interpret a failed barrier
as proof that the preceding rename, write, or external action did not happen.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from .errors import BoxControlError, PersistenceError, RecoveryRequired


def synchronize_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PersistenceError(f"cannot synchronize directory {path}: {exc}") from exc


def ensure_directory(path: Path, *, exist_ok: bool = True) -> None:
    path = Path(path).absolute()
    try:
        # Revisit the parent chain even when directories exist: a prior failed
        # mkdir barrier may have left visible but not durable intermediate names.
        if path.parent != path:
            ensure_directory(path.parent)
        path.mkdir(exist_ok=exist_ok)
        synchronize_directory(path)
        if path.parent != path:
            synchronize_directory(path.parent)
    except OSError as exc:
        raise PersistenceError(f"cannot durably create directory {path}: {exc}") from exc


def replace_json(path: Path, payload: dict) -> None:
    serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ensure_directory(path.parent)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".pending", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        synchronize_directory(path.parent)
    except OSError as exc:
        raise PersistenceError(f"cannot durably replace {path}: {exc}") from exc


def remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        # Also synchronize an already absent name: a previous unlink may have
        # succeeded while its directory barrier failed.
        synchronize_directory(path.parent)
    except OSError as exc:
        raise PersistenceError(f"cannot durably remove {path}: {exc}") from exc


_blocked_projects: set[Path] = set()


@contextmanager
def mutation_guard(project):
    """Public mutation-session guard; caller holds the project lock.

    This is a conservative interlock, NOT the authoritative operation journal
    planned for the next pass. No automatic clearing of interrupted sessions.
    """
    project.require_state_allowed()
    marker = project.state_dir / "mutation-incomplete.json"
    if project.path in _blocked_projects or marker.exists():
        raise RecoveryRequired(f"project recovery required before mutation: {marker}")
    try:
        replace_json(marker, {"schema": 1, "status": "mutation-incomplete"})
        try:
            yield
        except PersistenceError:
            raise
        except BoxControlError:
            # Ordinary operation errors have already used the normal persisted
            # failure/cleanup path. Storage errors never enter this branch.
            remove_file(marker)
            raise
        else:
            remove_file(marker)
    except PersistenceError:
        _blocked_projects.add(project.path)
        raise
    except BaseException:
        # Also latch an interrupted process if the embedding caller catches it.
        # A failed final unlink barrier might leave no currently visible marker.
        if marker.exists():
            _blocked_projects.add(project.path)
        raise
