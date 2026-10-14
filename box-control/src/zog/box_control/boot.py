from __future__ import annotations

from pathlib import Path

from .errors import RuntimeOperationError


BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def current_boot_id(path: Path = BOOT_ID_PATH) -> str:
    """Read the Linux boot identity used by START_ONCE_PER_BOOT."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeOperationError(f"cannot read boot identity from {path}: {exc}") from exc
    if not value:
        raise RuntimeOperationError(f"boot identity is empty: {path}")
    return value
