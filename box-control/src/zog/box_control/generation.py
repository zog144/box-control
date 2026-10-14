from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import BoxControlError, PersistenceError
from .durability import synchronize_directory

MANIFEST_NAME = ".box-control-rootfs.json"


class GenerationError(BoxControlError):
    pass


@dataclass(frozen=True)
class Generation:
    """A box-control view of an image-build generation.

    box-control never creates or writes generations.  It only validates enough
    identity to safely consume and reclaim them.
    """

    fingerprint: str
    path: Path

    @property
    def root(self) -> Path:
        return self.path / "root"

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST_NAME


def _read_identity(path: Path, *, expected_fingerprint: str) -> None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        fingerprint = str(raw["fingerprint"])
    except (OSError, json.JSONDecodeError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise GenerationError(f"cannot validate image generation manifest {path}: {exc}") from exc
    if fingerprint != expected_fingerprint:
        raise GenerationError(
            f"generation manifest fingerprint mismatch: expected {expected_fingerprint}, "
            f"found {fingerprint}"
        )


class GenerationStore:
    """Destructive/consumption-side image store owned by box-control.

    image-build creates and activates generations. box-control owns deletion
    because only box-control can know which generations are still consumed by
    live/unresolved application runtimes.
    """

    def __init__(self, rootfs_dir: Path):
        self.rootfs_dir = rootfs_dir
        self.generations_dir = rootfs_dir / "generations"
        self.active_link = rootfs_dir / "active"

    def generations(self) -> tuple[Generation, ...]:
        if not self.generations_dir.exists():
            return ()
        result: list[Generation] = []
        for path in sorted(self.generations_dir.iterdir()):
            if not path.is_dir():
                continue
            manifest = path / "root" / MANIFEST_NAME
            if not manifest.is_file():
                continue
            _read_identity(manifest, expected_fingerprint=path.name)
            result.append(Generation(path.name, path))
        return tuple(result)

    def get(self, fingerprint: str) -> Generation | None:
        path = self.generations_dir / fingerprint
        manifest = path / "root" / MANIFEST_NAME
        if not path.is_dir() or not manifest.is_file():
            return None
        _read_identity(manifest, expected_fingerprint=fingerprint)
        return Generation(fingerprint, path)

    def active(self) -> Generation | None:
        if not self.active_link.is_symlink():
            return None
        target = self.active_link.resolve()
        try:
            relative = target.relative_to(self.generations_dir.resolve())
        except ValueError as exc:
            raise GenerationError(f"active rootfs points outside generation store: {target}") from exc
        if len(relative.parts) != 1:
            raise GenerationError(f"invalid active generation target: {target}")
        generation = self.get(relative.name)
        if generation is None or generation.path.resolve() != target:
            raise GenerationError(f"active rootfs generation is invalid: {target}")
        return generation

    def reclaim_unreferenced(self, protected: set[str] | frozenset[str]) -> tuple[Generation, ...]:
        """Delete inactive generations not consumed by a protected application runtime."""
        active = self.active()
        protected_fingerprints = set(protected)
        if active is not None:
            protected_fingerprints.add(active.fingerprint)

        generations = self.generations()  # validate all before deleting any
        reclaimable = tuple(
            generation
            for generation in generations
            if generation.fingerprint not in protected_fingerprints
        )
        for generation in reclaimable:
            try:
                shutil.rmtree(generation.path)
                synchronize_directory(self.generations_dir)
            except OSError as exc:
                raise PersistenceError(
                    f"cannot reclaim rootfs generation {generation.fingerprint}: {exc}"
                ) from exc
        return reclaimable
