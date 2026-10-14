from pathlib import Path

from .errors import ConfigurationError
from .durability import ensure_directory


class Project:
    def __init__(self, path: Path):
        self.path = path.resolve()
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
                return cls(candidate)
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

    def ensure_state(self):
        if not self.is_inside_home():
            return
        for directory in (
            self.state_dir,
            self.rootfs_dir,
            self.mounts_dir,
            self.tmp_dir,
            self.repository_dir,
            self.runtime_dir,
            self.application_request_dir,
            self.application_request_result_dir,
        ):
            ensure_directory(directory)

    @property
    def project_identity(self):
        """Canonical host-wide project identity used in systemd unit names."""
        return self.path.name

    @property
    def lock_file(self):
        return self.state_dir / "box-control.lock"

    def require_state_allowed(self):
        if not self.is_inside_home():
            raise ConfigurationError(
                f"refusing to create or modify state outside the user's home: {self.path}"
            )
