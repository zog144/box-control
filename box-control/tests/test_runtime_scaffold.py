from pathlib import Path

from zog.box_control.model import ApplicationSpec, ProgramSpec
from zog.box_control.project import Project
from zog.box_control.runtime.scaffold import (
    rootfs_bind_paths,
    rootfs_layout,
    runtime_facility_directories,
    runtime_root_path,
)
from zog.box_control.runtime.systemd import service_definition


def _snapshot(root):
    result = []
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result.append((relative, "symlink", path.readlink().as_posix()))
        elif path.is_dir():
            result.append((relative, "directory"))
        elif path.is_file():
            result.append((relative, "file", path.read_bytes()))
    return result


def test_service_uses_state_scaffold_and_projects_generation_read_only(tmp_path):
    project = Project(tmp_path / "demo")
    generation_root = tmp_path / "generation"
    (generation_root / "usr" / "bin").mkdir(parents=True)
    (generation_root / "usr" / "bin" / "probe").write_text("fixture")
    (generation_root / "var" / "lib" / "probe").mkdir(parents=True)
    (generation_root / "bin").symlink_to("usr/bin")
    before = _snapshot(generation_root)

    application = ApplicationSpec(
        name="probe",
        programs=(
            ProgramSpec(
                name="probe",
                command=("/usr/bin/probe",),
                mounts=(("data", "/var/lib/probe"),),
            ),
        ),
    )
    definition = service_definition(
        project,
        application,
        "A7K3Q2",
        application.programs[0],
        generation_root=generation_root,
    )

    assert definition.generation_root == generation_root
    assert definition.root_directory == runtime_root_path(project, "A7K3Q2")
    assert definition.root_directory != generation_root
    assert dict((target, source) for source, target in definition.rootfs_bind_paths) == {
        "/usr": (generation_root / "usr").resolve(),
        "/var": (generation_root / "var").resolve(),
    }
    _binds, symlinks = rootfs_layout(generation_root)
    assert symlinks == (("/bin", "usr/bin"),)
    assert dict(definition.expected_properties())["RootDirectory"] == str(
        definition.root_directory.resolve()
    )
    assert dict(definition.expected_properties())["BindReadOnlyPaths"] == [
        [str(source.resolve()), target]
        for source, target in definition.rootfs_bind_paths
    ]
    assert _snapshot(generation_root) == before
    assert not definition.root_directory.exists()


def test_generation_applications_must_be_empty_if_present(tmp_path):
    project = Project(tmp_path / "demo")
    generation_root = tmp_path / "generation"
    (generation_root / "applications").mkdir(parents=True)
    (generation_root / "applications" / "unexpected").write_text("must not be hidden")
    application = ApplicationSpec(
        name="probe",
        programs=(ProgramSpec(name="probe", command=("/bin/true",)),),
    )

    import pytest
    from zog.box_control.errors import RuntimeOperationError

    with pytest.raises(RuntimeOperationError, match="empty directory scaffold"):
        service_definition(
            project,
            application,
            "B8L4R3",
            application.programs[0],
            generation_root=generation_root,
        )



def test_generation_relative_symlink_cannot_escape_runtime_root(tmp_path):
    import pytest
    from zog.box_control.errors import RuntimeOperationError

    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    (generation_root / "bin").symlink_to("../../host-bin")

    with pytest.raises(RuntimeOperationError, match="escapes the runtime root"):
        rootfs_layout(generation_root)


def test_empty_generation_applications_placeholder_is_not_projected(tmp_path):
    generation_root = tmp_path / "generation"
    (generation_root / "usr").mkdir(parents=True)
    (generation_root / "applications").mkdir()

    binds, symlinks = rootfs_layout(generation_root)

    assert symlinks == ()
    assert [(target, kind) for _source, target, kind in binds] == [
        ("/usr", "directory")
    ]


def test_generation_special_top_level_object_is_rejected(tmp_path):
    import os
    import pytest
    from zog.box_control.errors import RuntimeOperationError

    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    fifo = generation_root / "unexpected-fifo"
    os.mkfifo(fifo)

    with pytest.raises(RuntimeOperationError, match="unsupported generation root object"):
        rootfs_layout(generation_root)



def test_exact_top_level_writable_mount_replaces_rootfs_projection(tmp_path):
    project = Project(tmp_path / "demo")
    generation_root = tmp_path / "generation"
    (generation_root / "usr" / "bin").mkdir(parents=True)
    (generation_root / "usr" / "bin" / "probe").write_text("fixture")
    (generation_root / "root").mkdir()

    application = ApplicationSpec(
        name="probe",
        programs=(
            ProgramSpec(
                name="probe",
                command=("/usr/bin/probe",),
                mounts=(("state", "/root"),),
            ),
        ),
    )
    definition = service_definition(
        project,
        application,
        "C9M5S4",
        application.programs[0],
        generation_root=generation_root,
    )

    read_only = dict(
        (target, source) for source, target in definition.rootfs_bind_paths
    )
    assert "/usr" in read_only
    assert "/root" not in read_only
    assert [target for _source, target in definition.bind_paths] == ["/root"]
    properties = dict(definition.expected_properties())
    assert all(
        pair[1] != "/root"
        for pair in properties["BindReadOnlyPaths"]
    )
    assert properties["BindPaths"][0][1] == "/root"


def test_nested_writable_mount_keeps_read_only_parent_projection(tmp_path):
    project = Project(tmp_path / "demo")
    generation_root = tmp_path / "generation"
    (generation_root / "usr" / "bin").mkdir(parents=True)
    (generation_root / "usr" / "bin" / "probe").write_text("fixture")
    (generation_root / "var" / "lib" / "probe").mkdir(parents=True)

    application = ApplicationSpec(
        name="probe",
        programs=(
            ProgramSpec(
                name="probe",
                command=("/usr/bin/probe",),
                mounts=(("state", "/var/lib/probe"),),
            ),
        ),
    )
    definition = service_definition(
        project,
        application,
        "D2N6T5",
        application.programs[0],
        generation_root=generation_root,
    )

    assert "/var" in {
        target for _source, target in definition.rootfs_bind_paths
    }
    assert "/var/lib/probe" in {
        target for _source, target in definition.bind_paths
    }
    all_destinations = [
        target for _source, target in definition.rootfs_bind_paths
    ] + [
        target for _source, target in definition.bind_paths
    ]
    assert len(all_destinations) == len(set(all_destinations))


def test_runtime_facility_roots_are_private_scaffold_not_image_projection(tmp_path):
    generation_root = tmp_path / "generation"
    (generation_root / "usr").mkdir(parents=True)
    for destination in runtime_facility_directories():
        (generation_root / destination.lstrip("/")).mkdir(parents=True)

    binds, symlinks = rootfs_layout(generation_root)

    assert symlinks == ()
    assert [(target, kind) for _source, target, kind in binds] == [
        ("/usr", "directory")
    ]
    assert rootfs_bind_paths(generation_root) == (
        ((generation_root / "usr").resolve(), "/usr"),
    )


def test_nonempty_runtime_facility_in_generation_is_rejected(tmp_path):
    import pytest
    from zog.box_control.errors import RuntimeOperationError

    generation_root = tmp_path / "generation"
    (generation_root / "run" / "systemd").mkdir(parents=True)
    (generation_root / "run" / "systemd" / "payload").write_text("not ephemeral")

    with pytest.raises(RuntimeOperationError, match="empty directory scaffold"):
        rootfs_layout(generation_root)


def test_ordinary_writable_mount_below_run_is_rejected(tmp_path):
    import pytest
    from zog.box_control.errors import RuntimeOperationError

    project = Project(tmp_path / "demo")
    generation_root = tmp_path / "generation"
    (generation_root / "usr" / "bin").mkdir(parents=True)
    (generation_root / "usr" / "bin" / "probe").write_text("fixture")
    (generation_root / "run" / "probe").mkdir(parents=True)
    application = ApplicationSpec(
        name="probe",
        programs=(
            ProgramSpec(
                name="probe",
                command=("/usr/bin/probe",),
                mounts=(("state", "/run/probe"),),
            ),
        ),
    )

    with pytest.raises(RuntimeOperationError, match="controller path"):
        service_definition(
            project,
            application,
            "E3P7U6",
            application.programs[0],
            generation_root=generation_root,
        )


def test_published_empty_run_dbus_is_private_scaffold_declaration(tmp_path):
    from zog.box_control.runtime.scaffold import runtime_facility_subdirectories
    root = tmp_path / "generation"
    (root / "run" / "dbus").mkdir(parents=True)
    (root / "proc").mkdir()
    (root / "sys").mkdir()
    (root / "dev").mkdir()
    (root / "applications").mkdir()
    before = _snapshot(root)
    assert runtime_facility_subdirectories(root) == ("/run/dbus",)
    assert rootfs_layout(root) == ((), ())
    assert _snapshot(root) == before


def test_facility_directory_tree_rejects_files_links_and_all_roots(tmp_path):
    import pytest
    from zog.box_control.errors import RuntimeOperationError
    from zog.box_control.runtime.scaffold import runtime_facility_subdirectories
    for facility in ("run", "applications", "proc", "sys", "dev"):
        root = tmp_path / facility
        (root / facility / "nested").mkdir(parents=True)
        file = root / facility / "nested" / "payload"
        file.write_text("not ephemeral")
        with pytest.raises(RuntimeOperationError, match="empty directory scaffold"):
            runtime_facility_subdirectories(root)
        file.unlink()
        link = root / facility / "nested" / "link"
        link.symlink_to(".")
        with pytest.raises(RuntimeOperationError, match="empty directory scaffold"):
            rootfs_layout(root)
        link.unlink()
        assert runtime_facility_subdirectories(root) == (f"/{facility}/nested",)


def test_facility_directory_limit_is_bounded(tmp_path):
    import pytest
    from zog.box_control.errors import RuntimeOperationError
    root = tmp_path / "generation"
    for number in range(130):
        (root / "run" / f"dir-{number}").mkdir(parents=True)
    with pytest.raises(RuntimeOperationError, match="supported limits"):
        rootfs_layout(root)
