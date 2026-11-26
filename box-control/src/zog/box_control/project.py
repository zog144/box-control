from pathlib import Path
import os
import stat

from .errors import ConfigurationError
from .durability import ensure_directory


class Project:
    def __init__(self, path: Path, *, explicitly_selected: bool = True):
        # A selected Project is a deliberate controller input, not a path
        # discovered from the process environment. Retain the spelling so
        # an external root-run Project cannot quietly follow a symlink.
        self._selected_path = Path(path).absolute()
        self._explicitly_selected = explicitly_selected
        self.path = self._selected_path.resolve()
        self.package_dir = self.path / "package"
        self.application_dir = self.path / "application"
        self.catalogue_dir = self.path / "catalogue"
        self.state_dir = self.path / "state"
        self.state_file = self.state_dir / "state.json"
        self.rootfs_dir = self.state_dir / "rootfs"
        self.mounts_dir = self.state_dir / "mounts"
        self.tmp_dir = self.state_dir / "tmp"
        self.repository_dir = self.state_dir / "repository"
        self.runtime_dir = self.state_dir / "runtime"
        self.runtime_reference_file = self.runtime_dir / "references.json"
        self.application_request_dir = self.state_dir / "application-request"
        self.application_request_result_dir = (
            self.state_dir / "application-request-result"
        )

    @classmethod
    def discover(cls):
        candidates = [Path.cwd(), Path(__file__).resolve().parents[1]]
        for candidate in candidates:
            if (
                (candidate / "evaluate").is_file()
                and (candidate / "package").is_dir()
                and (candidate / "application").is_dir()
            ):
                return cls(candidate, explicitly_selected=False)
        raise ConfigurationError(
            "cannot locate project: expected evaluate, package/, and application/"
        )

    def is_inside_home(self):
        home = Path.home().resolve()
        try:
            self.path.relative_to(home)
            return True
        except ValueError:
            return False

    def _state_directories(self):
        return (
            self.state_dir,
            self.rootfs_dir,
            self.mounts_dir,
            self.tmp_dir,
            self.repository_dir,
            self.runtime_dir,
            self.application_request_dir,
            self.application_request_result_dir,
        )

    def _require_selected_root_state(self):
        """Allow root to operate only on state under the explicitly selected Project.

        This checks path redirection and ordinary accidental symlink escapes.
        It is not a defence against a malicious administrator changing the
        filesystem concurrently; that Host is trusted by the selected lane.
        """
        if not self._explicitly_selected or os.geteuid() != 0:
            return False
        if self.path != self._selected_path or self.path == Path("/"):
            raise ConfigurationError("selected Project path must be canonical and not symlinked")
        # Do not bless a renamed or redirected path, nor follow state symlinks.
        current = Path("/")
        for component in self.path.parts[1:]:
            current /= component
            try:
                mode = current.lstat().st_mode
            except OSError as exc:
                raise ConfigurationError(f"selected Project path is inaccessible: {current}") from exc
            if not stat.S_ISDIR(mode):
                raise ConfigurationError(f"selected Project ancestor is not a real directory: {current}")
        for directory in self._state_directories():
            if not directory.is_relative_to(self.path):
                raise ConfigurationError("Project state directory escapes selected Project")
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ConfigurationError(f"cannot inspect selected Project state: {directory}") from exc
            if not stat.S_ISDIR(mode):
                raise ConfigurationError(f"Project state path is not a real directory: {directory}")
        # Protect the two preexisting files read/written directly by the
        # controller and the interlock against redirection outside state/.
        for file in (self.lock_file, self.state_file,
                     self.state_dir / "mutation-incomplete.json",
                     self.state_dir / "session-recovery-pending.json"):
            try:
                mode = file.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ConfigurationError(f"cannot inspect selected Project state file: {file}") from exc
            if not stat.S_ISREG(mode):
                raise ConfigurationError(f"Project state file is not a real file: {file}")
        return True

    def ensure_state(self):
        self.require_state_allowed()
        for directory in self._state_directories():
            ensure_directory(directory)

    @property
    def project_identity(self):
        """Canonical host-wide project identity used in systemd unit names."""
        return self.path.name

    @property
    def lock_file(self):
        return self.state_dir / "box-control.lock"

    def require_state_allowed(self):
        if self.is_inside_home() or self._require_selected_root_state():
            return
        raise ConfigurationError(
            f"refusing state outside the home without an explicitly selected root-run Project: {self.path}"
        )
