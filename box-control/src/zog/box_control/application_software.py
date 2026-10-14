"""Bind verified image-build application software views to runtime launches."""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path, PurePosixPath

from zog.image_build.application_software import compatibility
from zog.image_build.application_views import (
    ApplicationSoftwareViewStore,
    CONSUMER_CONTRACT,
)
from zog.image_build.errors import ImageBuildError

from .durability import synchronize_directory
from .errors import PersistenceError, RecoveryRequired, RuntimeOperationError

BINDING_CONTRACT = "box-control-application-software-v1"
_SCHEMA = 1
_RUNTIME_ROOT = PurePosixPath("/applications")


def _digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeOperationError(f"application software manifest is unavailable: {path}")
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeOperationError(f"cannot read application software manifest {path}: {exc}") from exc


def rootfs_compatibility(selection):
    raw = selection.manifest.get("application_software_compatibility")
    if raw is None:
        raise RuntimeOperationError(
            "selected rootfs generation does not publish application_software_compatibility"
        )
    try:
        return compatibility(raw)
    except ImageBuildError as exc:
        raise RuntimeOperationError(f"invalid rootfs application software compatibility: {exc}") from exc


def _python_paths(view_manifest, mount_point):
    result = set()
    for item in view_manifest.get("composition", ()):
        path = PurePosixPath(str(item.get("path", "")))
        parts = path.parts
        for index, part in enumerate(parts):
            if part == "site-packages" and index >= 2 and parts[index - 2] in {"lib", "lib64"}:
                result.add((_RUNTIME_ROOT / PurePosixPath(mount_point).name / PurePosixPath(*parts[: index + 1])).as_posix())
    return sorted(result)


def resolve_binding(project, application, selection):
    """Validate and freeze one exact image-build consumer binding."""
    if not application.software_view:
        return application
    software_root = project.state_dir / "image-build" / "application-software"
    if software_root.is_symlink() or not software_root.is_dir():
        raise RuntimeOperationError("selected application software store is unavailable")
    try:
        store = ApplicationSoftwareViewStore(project.state_dir)
        provided = rootfs_compatibility(selection)
        consumer = store.consumer_manifest(application.software_view, provided)
        view = store.resolve(application.software_view)
    except (ImageBuildError, OSError) as exc:
        raise RuntimeOperationError(f"cannot bind application software view: {exc}") from exc
    if consumer["contract"] != CONSUMER_CONTRACT or consumer["application"] != application.name:
        raise RuntimeOperationError("application software view belongs to another application or contract")
    object_digests = []
    for item in consumer["objects"]:
        object_digests.append(
            {
                "artifact": item["artifact"],
                "manifest_digest": _digest(item["manifest"]),
            }
        )
    binding = dict(
        consumer,
        controller_contract=BINDING_CONTRACT,
        view_manifest_digest=_digest(consumer["manifest"]),
        object_manifest_digests=object_digests,
        python_paths=_python_paths(view.manifest, consumer["mount_point"]),
    )
    validate_binding(project, application.name, binding)
    return replace(application, software_binding=binding)


def _consumer_subset(binding):
    return {
        key: binding[key]
        for key in (
            "schema", "contract", "mount_contract", "view", "application", "profile",
            "root", "manifest", "mount_point", "objects", "rootfs_compatibility",
            "visibility", "reclamation_protection", "binding_digest",
        )
    }


def validate_binding(project, application, binding):
    """Revalidate a prepared binding without selecting or substituting another view."""
    if binding is None:
        return None
    if not isinstance(binding, dict) or binding.get("controller_contract") != BINDING_CONTRACT:
        raise RecoveryRequired("unsupported prepared application software binding")
    if binding.get("application") != application:
        raise RecoveryRequired("prepared application software binding belongs to another application")
    software_root = project.state_dir / "image-build" / "application-software"
    if software_root.is_symlink() or not software_root.is_dir():
        raise RecoveryRequired("prepared application software store is unavailable")
    try:
        store = ApplicationSoftwareViewStore(project.state_dir)
        expected = store.consumer_manifest(binding["view"], binding["rootfs_compatibility"])
        view = store.resolve(binding["view"])
    except (ImageBuildError, OSError, KeyError, TypeError) as exc:
        raise RecoveryRequired(f"prepared application software is unavailable or invalid: {exc}") from exc
    if _consumer_subset(binding) != expected:
        raise RecoveryRequired("prepared application software consumer binding changed")
    if _digest(binding["manifest"]) != binding.get("view_manifest_digest"):
        raise RecoveryRequired("prepared application software view manifest changed")
    expected_digests = {
        item["artifact"]: _digest(item["manifest"]) for item in binding["objects"]
    }
    saved_digests = {
        item["artifact"]: item["manifest_digest"]
        for item in binding.get("object_manifest_digests", ())
        if isinstance(item, dict) and set(item) == {"artifact", "manifest_digest"}
    }
    if saved_digests != expected_digests:
        raise RecoveryRequired("prepared application software artifact manifest changed")
    expected_python_paths = _python_paths(view.manifest, binding["mount_point"])
    if binding.get("python_paths") != expected_python_paths:
        raise RecoveryRequired("prepared application software Python visibility changed")
    return binding


def read_only_mounts(project, application, binding):
    if binding is None:
        return ()
    validate_binding(project, application, binding)
    mounts = [
        (Path(item["root"]), item["mount_point"])
        for item in binding["objects"]
    ]
    mounts.append((Path(binding["root"]), binding["mount_point"]))
    destinations = [target for _source, target in mounts]
    if len(destinations) != len(set(destinations)):
        raise RecoveryRequired("duplicate application software mount destination")
    return tuple(mounts)


def runtime_environment(binding, environment):
    values = dict(environment)
    if binding is None:
        return tuple(sorted(values.items()))
    root = binding["mount_point"]

    def prepend(name, items, fallback=""):
        prefix = ":".join(items)
        current = values.get(name, fallback)
        values[name] = prefix + ((":" + current) if current else "")

    prepend("PATH", [root + "/bin"], "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    prepend("LD_LIBRARY_PATH", [root + "/lib", root + "/lib64"])
    prepend("XDG_DATA_DIRS", [root + "/share"], "/usr/local/share:/usr/share")
    if binding.get("python_paths"):
        prepend("PYTHONPATH", list(binding["python_paths"]))
    return tuple(sorted(values.items()))


def protection_from_reference(reference):
    view = getattr(reference, "software_view", None)
    artifacts = tuple(getattr(reference, "software_artifacts", ()) or ())
    return ({view} if view else set()), set(artifacts)


def protection_from_binding(binding):
    if not binding:
        return set(), set()
    protection = binding.get("reclamation_protection") or {}
    view = protection.get("view")
    artifacts = protection.get("artifacts") or ()
    return ({view} if isinstance(view, str) else set()), {
        item for item in artifacts if isinstance(item, str)
    }


class ApplicationSoftwareReclaimer:
    """Reclaim verified published views/artifacts only after controller protection is empty."""

    def __init__(self, project):
        self.project = project
        self.root = project.state_dir / "image-build" / "application-software"

    def reclaim_unreferenced(
        self, protected_views, protected_artifacts, *,
        eligible_views=None, eligible_artifacts=None,
    ):
        protected_views = set(protected_views)
        protected_artifacts = set(protected_artifacts)
        eligible_views = None if eligible_views is None else set(eligible_views)
        eligible_artifacts = None if eligible_artifacts is None else set(eligible_artifacts)
        if self.root.is_symlink():
            raise RecoveryRequired("application software store is redirected")
        if not self.root.is_dir():
            if protected_views or protected_artifacts:
                raise RecoveryRequired("protected application software store is unavailable")
            return (), ()
        self.store = ApplicationSoftwareViewStore(self.project.state_dir)
        # Every retained protected view protects its exact artifact closure.
        for view in sorted(protected_views):
            try:
                value = self.store.resolve(view)
            except (ImageBuildError, OSError) as exc:
                raise RecoveryRequired(f"protected application software view is unavailable: {exc}") from exc
            protected_artifacts.update(value.manifest["artifacts"])

        reclaimed_views = []
        reclaimed_artifacts = []
        with self.store.artifacts.locked():
            views_dir = self.store.root / "views"
            artifacts_dir = self.store.root / "artifacts"
            views = []
            for path in sorted(views_dir.glob("*")):
                if path.is_dir() and not path.is_symlink():
                    identity = "sha256:" + path.name
                    value = self.store.resolve(identity)
                    views.append((identity, value))
            artifacts = []
            for path in sorted(artifacts_dir.glob("*")):
                if path.is_dir() and not path.is_symlink():
                    identity = "sha256:" + path.name
                    value = self.store.artifacts.resolve(identity)
                    artifacts.append((identity, value))
            for identity, value in views:
                if identity in protected_views or (
                    eligible_views is not None and identity not in eligible_views
                ):
                    continue
                try:
                    shutil.rmtree(value.root.parent)
                    synchronize_directory(views_dir)
                except OSError as exc:
                    raise PersistenceError(f"cannot reclaim application software view {identity}: {exc}") from exc
                reclaimed_views.append(identity)
            # Re-evaluate retained views after view deletion to protect their artifacts.
            for identity, value in views:
                if identity not in reclaimed_views:
                    protected_artifacts.update(value.manifest["artifacts"])
            for identity, value in artifacts:
                if identity in protected_artifacts or (
                    eligible_artifacts is not None and identity not in eligible_artifacts
                ):
                    continue
                try:
                    shutil.rmtree(value.root.parent)
                    synchronize_directory(artifacts_dir)
                except OSError as exc:
                    raise PersistenceError(f"cannot reclaim application software artifact {identity}: {exc}") from exc
                reclaimed_artifacts.append(identity)
        return tuple(reclaimed_views), tuple(reclaimed_artifacts)
