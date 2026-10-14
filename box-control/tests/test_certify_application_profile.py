"""The review-only Certbot prototype is not a safe application-wide launch."""
import pytest

from zog.box_control.errors import ConfigurationError
from zog.box_control.specification import parse_application


def _render(program_names):
    programs = ",\n".join(
        "program(name=%r, command=('/usr/bin/python3', '-m', 'zog.certify_manage', %r), "
        "mounts={'certificate-data':'/var/lib/zog-certify-manage'})" % (name, name)
        for name in program_names
    )
    return (
        "application(name='certify-manage', persistent=True, "
        "multiple_instances=False, execution_identity='application', "
        "start_policy='externally-controlled', "
        "writable_mounts=('certificate-data',), "
        "preparation_revision='certify-manage-state-v1', "
        "preparation=(program(name='prepare', command=('/usr/bin/true',), "
        "execution_timeout_seconds=120, "
        "mounts={'certificate-data':'/var/lib/zog-certify-manage'}),), "
        "programs=(" + programs + ",))"
    )


@pytest.mark.parametrize("commands", [
    ("issue", "renew", "inspect"),
    ("issue", "renew"),
    ("renew", "inspect"),
])
def test_multi_program_certificate_candidate_fails_before_launch(tmp_path, commands):
    path = tmp_path / "application.py"
    path.write_text(_render(commands))
    with pytest.raises(ConfigurationError, match="exclusively selected runtime program"):
        parse_application(path)


def test_single_explicit_certificate_command_parses_without_provisioning(tmp_path):
    path = tmp_path / "application.py"
    path.write_text(_render(("renew",)))
    parsed = parse_application(path)
    assert parsed.name == "certify-manage"
    assert parsed.execution_identity == "application"
    assert parsed.persistent
    assert parsed.start_policy.value == "externally-controlled"
    assert parsed.writable_mounts == ("certificate-data",)
    assert [p.name for p in parsed.programs] == ["renew"]
