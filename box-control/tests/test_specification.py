import pytest

from zog.box_control.errors import ConfigurationError
from zog.box_control.model import ApplicationStartPolicy
from zog.box_control.specification import discover_applications, parse_application


def test_application_and_nested_program_are_ast_parsed(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        '''application(
    name="hello",
    start_policy="keep-running",
    multiple_instances=False,
    programs=(
        program(
            name="main",
            command=("/usr/bin/hello",),
            environment={"HELLO": "world"},
            mounts={"data": "/var/lib/hello"},
        ),
    ),
)''',
        encoding="utf-8",
    )
    spec = parse_application(path)
    assert spec.name == "hello"
    assert spec.start_policy == ApplicationStartPolicy.KEEP_RUNNING
    assert not spec.multiple_instances
    assert spec.programs[0].name == "main"
    assert spec.programs[0].command == ("/usr/bin/hello",)
    assert spec.programs[0].environment == (("HELLO", "world"),)
    assert spec.programs[0].mounts == (("data", "/var/lib/hello"),)


def test_build_fields_are_not_box_control_application_fields(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        'application(name="hello", build_command=("make",), programs=(program(name="main"),))',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown application fields"):
        parse_application(path)


def test_program_cannot_contain_package_build_metadata(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        'application(name="hello", programs=(program(name="main", distribution_packages=("glibc",)),))',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown program fields"):
        parse_application(path)


def test_duplicate_program_names_are_rejected(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        'application(name="hello", programs=(program(name="main"), program(name="main")))',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate program name"):
        parse_application(path)


def test_discovery_reads_canonical_application_directory_only(tmp_path):
    declared = tmp_path / "hello"
    declared.mkdir()
    (declared / "application.py").write_text(
        'application(name="hello", programs=(program(name="main"),))',
        encoding="utf-8",
    )
    (tmp_path / "support-data").mkdir()
    assert tuple(discover_applications(tmp_path)) == ("hello",)


def test_unknown_start_policy_is_rejected(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        'application(name="hello", start_policy="always", programs=(program(name="main"),))',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unknown start_policy"):
        parse_application(path)


def test_multiple_instances_must_be_boolean(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        'application(name="hello", multiple_instances="yes", programs=(program(name="main"),))',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must be a boolean"):
        parse_application(path)


def test_mount_name_cannot_escape_state_mount_directory(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(
        'application(name="hello", programs=(program(name="main", mounts={"../escape": "/state"}),))',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        parse_application(path)



def test_symbolic_persistent_execution_identity_requires_no_explicit_user(tmp_path):
    path = tmp_path / "application.py"
    path.write_text("""application(
    name="portal", persistent=True, execution_identity="application",
    writable_mounts=("data",), preparation_revision="1",
    preparation=(program(name="prepare", command=("/usr/bin/true",),
                         mounts={"data": "/var/lib/portal"},
                         execution_timeout_seconds=60),),
    programs=(program(name="web", command=("/usr/bin/true",),
                      mounts={"data": "/var/lib/portal"}),),
)""")
    spec = parse_application(path)
    assert spec.execution_identity == "application"
    assert spec.execution_binding is None
    assert spec.programs[0].user == "regular"  # Legacy placeholder until privileged binding.


@pytest.mark.parametrize("extra,reason", [
    (', user="regular"', "forbids explicit program user/group"),
    (', group="staff"', "forbids explicit program user/group"),
])
def test_symbolic_grant_rejects_caller_chosen_credentials(tmp_path, extra, reason):
    path = tmp_path / "application.py"
    path.write_text('''application(
        name="portal", persistent=True, execution_identity="application",
        writable_mounts=("data",), preparation_revision="1",
        preparation=(program(name="prepare", command=("/usr/bin/true",),
             mounts={"data": "/var/lib/portal"}, execution_timeout_seconds=60),),
        programs=(program(name="web", command=("/usr/bin/true",),
             mounts={"data": "/var/lib/portal"}''' + extra + '''),),
    )''')
    with pytest.raises(ConfigurationError, match=reason):
        parse_application(path)


def test_symbolic_identity_rejects_nonpersistent_or_multi_instance(tmp_path):
    path = tmp_path / "application.py"
    for extra in ('', ', multiple_instances=True'):
        path.write_text('application(name="portal", execution_identity="application"'
                        + extra + ', programs=(program(name="web"),))')
        with pytest.raises(ConfigurationError, match="persistent singleton"):
            parse_application(path)


def test_security_regression_negative_control_must_fail():
    """Deliberately failing scratch-PR fixture; NEVER merge to unstable."""
    assert False, "negative control confirms security job blocks a failing test"
