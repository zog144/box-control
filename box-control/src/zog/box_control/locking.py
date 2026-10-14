from __future__ import annotations

import fcntl
import time
import math
from .errors import ProjectBusy
from pathlib import Path
from types import TracebackType


class ProjectLock:
    """Serialize mutating box-control operations for one project.

    The lock is advisory and process-wide through flock(2), so a one-shot
    PROJECT_EVALUATION and a long-lived station-access process use the same
    serialization mechanism without sharing Python process state.
    """

    def __init__(self, path: Path, *, timeout_seconds=None):
        if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds < 0):
            raise ValueError("invalid lock timeout")
        self.timeout_seconds = timeout_seconds
        self.path = path
        self._file = None

    def __enter__(self) -> "ProjectLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+")
        try:
            if self.timeout_seconds is None:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + self.timeout_seconds
                while True:
                    try:
                        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ProjectBusy("project is busy; retry the same request identity")
                        time.sleep(min(remaining, 0.02))
        except BaseException:
            self._file.close()
            self._file = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
