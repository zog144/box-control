from __future__ import annotations

import json
from pathlib import Path

from .errors import BoxControlError
from .durability import replace_json


class StateError(BoxControlError):
    pass


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError(f"cannot read state file {self.path}: {exc}") from exc
        if not isinstance(value, dict):
            raise StateError(f"state file must contain a JSON object: {self.path}")
        return value

    def save(self, state: dict) -> None:
        replace_json(self.path, state)
