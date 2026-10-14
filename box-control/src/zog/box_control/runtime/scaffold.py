"""Deterministic per-runtime root scaffold contract.

The immutable generation is never used directly as systemd RootDirectory.
root-control creates a private scaffold under state/mounts, projects immutable
generation objects into it read-only, and keeps controller/runtime facility
mount targets out of the immutable image.

Top-level writable application mounts are explicit replacements: their generation
entry remains part of the scaffold shape, but that entry is not projected
read-only for the service that owns the writable replacement. Nested writable
mounts remain overlays beneath the containing read-only generation projection.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..errors import RuntimeOperationError

# These roots are runtime namespace facilities, not immutable image software.
# Image-provided *empty directory trees* are admitted as runtime directory
# declarations, never projected as generation bind sources. Files, links, sockets,
# and devices remain forbidden. All declared directories are created privately.
_RUNTIME_FACILITY_ROOTS = ("applications", "run", "proc", "sys", "dev")
_MAX_RUNTIME_FACILITY_DIRECTORIES = 128
_MAX_RUNTIME_FACILITY_DEPTH = 12
_MAX_RUNTIME_FACILITY_PATH_BYTES = 4096


def runtime_root_path(project, runtime_id: str) -> Path:
    return project.mounts_dir / "runtime-roots" / runtime_id / "root"


def runtime_facility_directories() -> tuple[str, ...]:
    return tuple("/" + name for name in _RUNTIME_FACILITY_ROOTS)


def _safe_symlink_target(target: str) -> str:
    if not target or "\x00" in target:
        raise RuntimeOperationError("invalid generation root symlink")
    path = PurePosixPath(target)
    # Absolute targets are interpreted inside RootDirectory. Relative targets
    # must not climb above the runtime root.
    if not path.is_absolute():
        depth = 0
        for part in path.parts:
            if part in ("", "."):
                continue
            if part == "..":
                depth -= 1
                if depth < 0:
                    raise RuntimeOperationError(
                        "generation root symlink escapes the runtime root"
                    )
            else:
                depth += 1
    return target


def runtime_facility_subdirectories(generation_root: Path) -> tuple[str, ...]:
    """Validate image-provided facility trees, returning private scaffold paths.

    This intentionally transfers directory *shape*, not image backing, metadata,
    or mutable contents. Every nested entry must be a real directory. The bounded
    inventory becomes part of the privileged runtime scaffold manifest.
    """
    root = Path(generation_root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeOperationError("selected generation root is unavailable or redirected")
    directories = []
    for name in _RUNTIME_FACILITY_ROOTS:
        entry = root / name
        if not entry.exists() and not entry.is_symlink():
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise RuntimeOperationError(
                f"generation /{name} conflicts with controller runtime composition"
            )
        stack = [(entry, "/" + name, 0)]
        while stack:
            parent, prefix, depth = stack.pop()
            try:
                children = sorted(parent.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise RuntimeOperationError(
                    f"cannot inspect generation {prefix}: {exc}"
                ) from exc
            for child in children:
                target = prefix + "/" + child.name
                if child.is_symlink() or not child.is_dir():
                    raise RuntimeOperationError(
                        f"generation {target} is not an empty directory scaffold"
                    )
                if (depth + 1 > _MAX_RUNTIME_FACILITY_DEPTH
                        or len(target.encode("utf-8")) > _MAX_RUNTIME_FACILITY_PATH_BYTES
                        or len(directories) >= _MAX_RUNTIME_FACILITY_DIRECTORIES):
                    raise RuntimeOperationError(
                        "generation runtime facility directory tree exceeds supported limits"
                    )
                directories.append(target)
                stack.append((child, target, depth + 1))
    return tuple(sorted(directories))


def rootfs_layout(generation_root: Path):
    """Return immutable top-level projection candidates and preserved symlinks.

    Runtime facility roots are deliberately excluded from the projection so
    systemd/API setup can only mutate the private runtime scaffold.
    """
    root = Path(generation_root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeOperationError(
            "selected generation root is unavailable or redirected"
        )
    # Validate all five facility roots before constructing *any* service binds.
    # An image containing /run/dbus (empty) declares a private runtime directory.
    runtime_facility_subdirectories(root)
    binds = []
    symlinks = []
    for entry in sorted(root.iterdir(), key=lambda value: value.name):
        name = entry.name
        if "/" in name or name in ("", ".", ".."):
            raise RuntimeOperationError("invalid generation root entry")
        target = "/" + name
        if name in _RUNTIME_FACILITY_ROOTS:
            continue
        if entry.is_symlink():
            try:
                symlinks.append(
                    (target, _safe_symlink_target(entry.readlink().as_posix()))
                )
            except OSError as exc:
                raise RuntimeOperationError(
                    f"cannot inspect generation symlink {entry}: {exc}"
                ) from exc
        elif entry.is_dir() or entry.is_file():
            binds.append(
                (
                    entry.resolve(),
                    target,
                    "directory" if entry.is_dir() else "file",
                )
            )
        else:
            raise RuntimeOperationError(
                f"unsupported generation root object: {entry}"
            )
    return tuple(binds), tuple(symlinks)


def rootfs_bind_paths(
    generation_root: Path,
    *,
    writable_destinations=(),
) -> tuple[tuple[Path, str], ...]:
    """Return the effective read-only projection for one program service.

    An explicit writable mount may replace an exact top-level generation
    directory (for example /root or /data). In that case emitting both
    BindReadOnlyPaths=/root and BindPaths=/root is ambiguous and is forbidden;
    the read-only projection is omitted for that exact destination. Nested
    writable destinations (for example /var/lib/app) do not replace /var.
    """
    overrides = set()
    for value in writable_destinations:
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or str(PurePosixPath(value)) != value
            or ".." in PurePosixPath(value).parts
            or "\x00" in value
        ):
            raise RuntimeOperationError(
                f"invalid writable projection override: {value!r}"
            )
        if len(PurePosixPath(value).parts) == 2:
            overrides.add(value)
    binds, _symlinks = rootfs_layout(generation_root)
    return tuple(
        (source, target)
        for source, target, _kind in binds
        if target not in overrides
    )
