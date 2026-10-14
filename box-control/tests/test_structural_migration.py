from zog.box_control.model import ApplicationSpec, ProgramSpec
from zog.box_control.project import Project
from zog.box_control.runtime.systemd import (
    application_definition,
    application_slice_name,
    program_unit_name,
)


def test_systemd_identity_is_scoped_by_project_and_application_runtime(tmp_path):
    project = Project(tmp_path / "my-project")
    assert application_slice_name(project, "A7K3Q2") == "zog-my-project-A7K3Q2.slice"
    assert (
        program_unit_name(project, "A7K3Q2", "browser")
        == "zog-my-project-A7K3Q2-browser.service"
    )
    assert program_unit_name(project, "M91P4D", "browser") != program_unit_name(
        project, "A7K3Q2", "browser"
    )


def test_systemd_application_definition_contains_one_service_per_program(tmp_path):
    project = Project(tmp_path / "project")
    application = ApplicationSpec(
        name="desktop",
        programs=(
            ProgramSpec(
                name="browser",
                command=("/usr/bin/browser",),
                mounts=(("data", "/var/lib/browser"),),
            ),
            ProgramSpec(name="helper", command=("/usr/bin/helper",)),
        ),
    )
    root = tmp_path / "generation" / "root"
    (root / "var/lib/browser").mkdir(parents=True)
    definition = application_definition(
        project, application, "A7K3Q2", generation_root=root
    )

    assert definition.slice_name == "zog-project-A7K3Q2.slice"
    assert tuple(service.unit_name for service in definition.services) == (
        "zog-project-A7K3Q2-browser.service",
        "zog-project-A7K3Q2-helper.service",
    )
    browser = definition.services[0]
    assert browser.type == "exec"
    assert browser.exit_type == "cgroup"
    assert browser.kill_mode == "control-group"
    assert browser.restart == "no"
    assert not browser.remain_after_exit
    assert browser.generation_root == root
    assert browser.root_directory == project.mounts_dir / "runtime-roots" / "A7K3Q2" / "root"
    assert browser.bind_paths == (
        (project.state_dir / "mounts" / "desktop" / "A7K3Q2" / "browser" / "data", "/var/lib/browser"),
    )
    expected = dict(browser.expected_properties())
    assert expected["ProtectSystem"] == "strict"
    assert expected["PrivateTmp"] is True
    assert expected["MountAPIVFS"] is True
    assert expected["StandardOutput"] == "journal"
    assert expected["StandardError"] == "journal"
    assert expected["User"] == "regular"


def test_project_uses_canonical_package_application_state_and_catalogue_roots(tmp_path):
    project = Project(tmp_path / "project")
    assert project.state_dir == project.path / "state"
    assert project.lock_file == project.path / "state" / "box-control.lock"
    assert project.package_dir == project.path / "package"
    assert project.application_dir == project.path / "application"
    assert project.catalogue_dir == project.path / "catalogue"
    assert project.project_identity == "project"
    assert not hasattr(project, "definition_root")
