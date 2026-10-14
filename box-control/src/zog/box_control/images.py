"""Consume published images; compilation belongs to the image-build caller."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from zog.image_build import ImageBuildError, read_selection
from .errors import BoxControlError


class ImageProviderError(BoxControlError):
    pass


@dataclass(frozen=True)
class ImageSelection:
    generation: str
    root: Path
    manifest: dict
    previous_manifest: dict | None = None
    reused: bool = False


class ImageProvider(Protocol):
    def ensure(self, project) -> ImageSelection: ...


def published_selection(project_root, generation):
    """Validate one exact published application image without selecting a substitute."""
    if not isinstance(generation, str) or len(generation) != 64 or any(c not in '0123456789abcdef' for c in generation):
        raise ImageProviderError('invalid published generation identity')
    store = Path(project_root) / 'state' / 'image-build' / 'generations'
    directory = store / generation
    if directory.is_symlink() or directory.resolve().parent != store.resolve():
        raise ImageProviderError('published generation must be a direct directory in its store')
    if (directory / 'root').is_symlink() or not (directory / 'root').is_dir():
        raise ImageProviderError('published generation root is missing or a symlink')
    try:
        selection = read_selection(directory)
    except (ImageBuildError, OSError) as exc:
        raise ImageProviderError(f'cannot validate published image: {exc}') from exc
    if selection.manifest.get('kind') != 'image':
        raise ImageProviderError('application launch requires a published image, not a toolchain or verification generation')
    # Preserve the source manifest and add the controller's established identity alias.
    return ImageSelection(selection.generation, selection.root,
                          {**selection.manifest, 'fingerprint': selection.generation}, reused=True)


class ImageBuildProvider:
    """Read-only adapter for the pinned image-build published-generation contract."""
    def preview(self, project) -> ImageSelection | None:
        active = project.state_dir / 'image-build' / 'active'
        if not active.is_symlink():
            if active.exists():
                raise ImageProviderError('published active image must be a symlink')
            return None
        store = project.state_dir / 'image-build' / 'generations'
        target = active.resolve()
        if target.parent != store.resolve():
            raise ImageProviderError('active image points outside its generation store')
        return published_selection(project.path, target.name)

    def ensure(self, project) -> ImageSelection:
        selection = self.preview(project)
        if selection is None:
            raise ImageProviderError('no published active image; run image-build before launching applications')
        return selection
