"""Explicit Project state admission outside the controller's home.

Pure source tests: these never touch the selected compiler Host, AWS/SSM,
request #63, or any persisted production records.
"""
from pathlib import Path

import pytest

from zog.box_control import project as project_module
from zog.box_control.errors import ConfigurationError, RecoveryRequired
from zog.box_control.durability import mutation_guard
from zog.box_control.project import Project


@pytest.fixture
def external_project(tmp_path, monkeypatch):
    # Simulate root-run controller in /root using a separate approved Project.
    monkeypatch.setattr(Project, "is_inside_home", lambda self: False)
    monkeypatch.setattr(project_module.os, "geteuid", lambda: 0)
    approved = tmp_path / "home" / "certbot-build-owner" / "certbot-seed"
    approved.mkdir(parents=True)
    return Project(approved)


def test_selected_root_project_outside_home_creates_only_its_state(external_project):
    project = external_project
    project.require_state_allowed()
    project.ensure_state()
    assert project.state_dir == project.path / "state"
    assert project.state_dir.is_dir()
    assert all(p.is_dir() and p.is_relative_to(project.state_dir)
               for p in project._state_directories())
    assert project.lock_file.parent == project.state_dir
    assert project.state_file.parent == project.state_dir
    project.require_state_allowed()  # stable across the next admitted operation


def test_no_owner_migration_or_operation_replay_on_existing_state(external_project):
    project = external_project
    project.state_dir.mkdir()
    before_owner = project.path.stat().st_uid
    state_owner = project.state_dir.stat().st_uid
    marker = project.state_dir / "mutation-incomplete.json"
    content = b'{"schema":1,"status":"mutation-incomplete","request":"63"}\n'
    marker.write_bytes(content)
    project.ensure_state()
    assert project.path.stat().st_uid == before_owner
    assert project.state_dir.stat().st_uid == state_owner
    assert marker.read_bytes() == content
    with pytest.raises(RecoveryRequired):
        with mutation_guard(project):
            pytest.fail("interrupted request must not be replayed")
    assert marker.read_bytes() == content


def test_external_project_is_still_forbidden_to_nonroot(external_project, monkeypatch):
    project = external_project
    monkeypatch.setattr(project_module.os, "geteuid", lambda: 1000)
    with pytest.raises(ConfigurationError, match="explicitly selected root-run"):
        project.require_state_allowed()
    with pytest.raises(ConfigurationError):
        project.ensure_state()
    assert not project.state_dir.exists()


def test_nonselected_project_cannot_escape_home_even_as_root(external_project):
    project = Project(external_project.path, explicitly_selected=False)
    with pytest.raises(ConfigurationError, match="explicitly selected root-run"):
        project.require_state_allowed()
    with pytest.raises(ConfigurationError):
        project.ensure_state()
    assert not project.state_dir.exists()


def test_discovery_is_not_explicit_selection(tmp_path, monkeypatch):
    root = tmp_path / "discovered"
    for sub in ("package", "application"):
        (root / sub).mkdir(parents=True)
    (root / "evaluate").write_text("evaluation")
    monkeypatch.setattr(project_module.Path, "cwd", classmethod(lambda cls: root))
    monkeypatch.setattr(Project, "is_inside_home", lambda self: False)
    monkeypatch.setattr(project_module.os, "geteuid", lambda: 0)
    project = Project.discover()
    assert project.path == root
    with pytest.raises(ConfigurationError, match="explicitly selected root-run"):
        project.require_state_allowed()


def test_selected_project_path_symlink_is_not_rebound(tmp_path, monkeypatch):
    monkeypatch.setattr(Project, "is_inside_home", lambda self: False)
    monkeypatch.setattr(project_module.os, "geteuid", lambda: 0)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "approved-alias"
    alias.symlink_to(real, target_is_directory=True)
    project = Project(alias)
    assert project.path == real
    with pytest.raises(ConfigurationError, match="canonical"):
        project.require_state_allowed()
    assert not (real / "state").exists()


@pytest.mark.parametrize("name", ("state", "state/runtime", "state/rootfs",
                                 "state/application-request"))
def test_root_selection_does_not_follow_redirected_state_directory(
        external_project, tmp_path, name):
    project = external_project
    external = tmp_path / "unexpected-writes"
    external.mkdir()
    target = project.path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(external, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="not a real directory"):
        project.require_state_allowed()
    with pytest.raises(ConfigurationError):
        project.ensure_state()
    assert list(external.iterdir()) == []


@pytest.mark.parametrize("name", ("box-control.lock", "state.json",
                                 "mutation-incomplete.json",
                                 "session-recovery-pending.json"))
def test_root_selection_does_not_follow_state_control_file_symlink(
        external_project, tmp_path, name):
    project = external_project
    project.state_dir.mkdir()
    outside = tmp_path / "foreign"
    outside.write_text("keep")
    (project.state_dir / name).symlink_to(outside)
    with pytest.raises(ConfigurationError, match="not a real file"):
        project.require_state_allowed()
    assert outside.read_text() == "keep"


def test_project_state_must_remain_bound_to_selected_root(external_project, tmp_path):
    project = external_project
    project.state_dir = tmp_path / "foreign-state"
    with pytest.raises(ConfigurationError, match="escapes selected Project"):
        project.require_state_allowed()


def test_normal_in_home_nonroot_semantics_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(Project, "is_inside_home", lambda self: True)
    monkeypatch.setattr(project_module.os, "geteuid", lambda: 1000)
    project = Project(tmp_path / "home-project", explicitly_selected=False)
    project.path.mkdir()
    project.require_state_allowed()
    project.ensure_state()
    assert project.state_dir.is_dir()
