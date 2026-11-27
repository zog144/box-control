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
        self._validate_controller_state()
        return True

    def _validate_controller_state(self):
        """Check controller metadata only, not application/build payload trees.

        Called before locking or recovery, and on direct build record access.
        Missing paths may be created by their ordinary owners; existing
        symlinks, special files and hard-linked metadata are not admitted.
        The selected Host must serialize administrative filesystem changes.
        """
        if self.state_dir != self.path / "state":
            raise ConfigurationError("Project state directory escapes selected Project")

        def check(path, *, directory):
            if not path.is_relative_to(self.state_dir) or ".." in path.parts:
                raise ConfigurationError("Project state path escapes selected Project")
            try:
                info = path.lstat()
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise ConfigurationError(f"cannot inspect selected Project state: {path}") from exc
            if directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise ConfigurationError(f"Project state path is not a real directory: {path}")
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ConfigurationError(f"Project state file is not a real file with one link: {path}")
            return True

        # Flat metadata stores used by mutation admission, recovery, and
        # retention. Do not descend into rootfs, mounts, image-build or
        # repository payloads, where legitimate symlinks are expected.
        stores = (
            self.application_request_dir, self.application_request_result_dir,
            *(self.state_dir / name for name in (
                "build-job", "build-resource", "operation", "application-storage",
                "workspace", "session-recovery")),
        )
        # Check state itself before any child lstat/iteration.
        for directory in (*self._state_directories(), *stores):
            check(directory, directory=True)
        for file in (
            self.lock_file, self.state_file, self.runtime_reference_file,
            *(self.state_dir / name for name in (
                "mutation-incomplete.json", "session-recovery-pending.json",
                "request-identity.json", "build-request-identity.json",
                "retention-incomplete.json", "build-pruning.json")),
        ):
            check(file, directory=False)
        for directory in stores:
            if check(directory, directory=True):
                for entry in directory.iterdir():
                    # Atomic JSON replacement also leaves .pending evidence.
                    # All entries in these flat stores are controller metadata.
                    check(entry, directory=False)

    def validate_external_state(self):
        """Read-only build inspection gets the same external root path checks."""
        if not self.is_inside_home():
            self._require_selected_root_state()


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
