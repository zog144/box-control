import json

import pytest

from zog.box_control import host_bindings
from zog.box_control.errors import RuntimeOperationError
from zog.box_control.model import ApplicationSpec, ProgramSpec
from zog.box_control.project import Project
from zog.box_control.runtime.systemd import service_definition


def policy_fixture(monkeypatch, tmp_path, *, project, source):
    policy = tmp_path / "host-policy.json"
    monkeypatch.setattr(host_bindings, "POLICY_PATH", policy)
    monkeypatch.setattr(host_bindings, "_require_privileged_policy", lambda p: None)
    monkeypatch.setattr(host_bindings, "_ALLOWED_SOURCE_ROOTS", (str(tmp_path) + "/",))
    entry = dict(project=str(project.path), application="station-access",
                 program="scheduler", capability="mirror-live", source=str(source))
    policy.write_text(json.dumps(dict(schema=1, bindings=[entry])))
    return policy, entry


def test_host_directory_capability_tracks_atomic_child_replacement(tmp_path, monkeypatch):
    project = Project(tmp_path / "project")
    source = tmp_path / "mirror-export"
    source.mkdir()
    (source / "mirror-intent.json").write_text("version-one")
    policy, entry = policy_fixture(monkeypatch, tmp_path, project=project, source=source)
    grants = host_bindings.resolve(project.path, "station-access", "scheduler")
    assert len(grants) == 1
    assert grants[0]["source"] == str(source)
    assert grants[0]["destination"] == "/run/zog-host/mirror-live"
    assert grants[0]["access"] == "read-only"
    assert host_bindings.resolve(project.path, "station-access", "web") == ()
    new_file = source / "mirror-intent.new"
    new_file.write_text("version-two")
    new_file.replace(source / "mirror-intent.json")
    assert host_bindings.resolve(project.path, "station-access", "scheduler") == grants
    assert (source / "mirror-intent.json").read_text() == "version-two"
    assert policy.is_file()


def test_capability_rejects_replacement_and_redirected_sources(tmp_path, monkeypatch):
    project = Project(tmp_path / "project")
    source = tmp_path / "mirror-export"
    source.mkdir()
    policy_fixture(monkeypatch, tmp_path, project=project, source=source)
    before = host_bindings.resolve(project.path, "station-access", "scheduler")
    source.rename(tmp_path / "retired")
    source.mkdir()
    after = host_bindings.resolve(project.path, "station-access", "scheduler")
    assert (before[0]["source_device"], before[0]["source_inode"]) != (
        after[0]["source_device"], after[0]["source_inode"])
    source.rmdir()
    source.symlink_to(tmp_path / "retired", target_is_directory=True)
    with pytest.raises(RuntimeOperationError, match="redirected"):
        host_bindings.resolve(project.path, "station-access", "scheduler")


def test_host_bindings_are_controller_resolved_and_recorded(tmp_path, monkeypatch):
    project = Project(tmp_path / "project")
    source = tmp_path / "mirror-export"
    source.mkdir()
    policy_fixture(monkeypatch, tmp_path, project=project, source=source)
    root = tmp_path / "generation"
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/python").write_text("fixture")
    program = ProgramSpec(name="scheduler", command=("/usr/bin/python",), user="993")
    application = ApplicationSpec(name="station-access", programs=(program,))
    definition = service_definition(
        project, application, "A7K3Q2", program, generation_root=root)
    assert definition.host_bindings[0]["capability"] == "mirror-live"
    assert definition.read_only_bind_paths[-1] == (source, "/run/zog-host/mirror-live")
    transport = definition.to_transport_dict()
    assert transport["host_bindings"] == list(definition.host_bindings)
    transport["host_bindings"][0]["source"] = "/forged/host/path"
    assert definition.host_bindings[0]["source"] == str(source)
    recorded = definition.mount_inventory()["mounts"]
    assert recorded[-1]["kind"] == "host-capability"
    assert recorded[-1]["lifetime"] == "host"
    assert recorded[-1]["access"] == "read-only"
    assert recorded[-1]["source_device"] == source.stat().st_dev
    assert recorded[-1]["source_inode"] == source.stat().st_ino
    assert dict(definition.expected_properties())["BindReadOnlyPaths"][-1] == [
        str(source), "/run/zog-host/mirror-live"]
    web = ProgramSpec(name="web", command=("/usr/bin/python",))
    web_definition = service_definition(
        project, ApplicationSpec(name="station-access", programs=(web,)),
        "A7K3Q2", web, generation_root=root)
    assert web_definition.host_bindings == ()


def test_invalid_policy_and_unsafe_capability_fail_closed(tmp_path, monkeypatch):
    project = Project(tmp_path / "project")
    source = tmp_path / "mirror-export"
    source.mkdir()
    policy, entry = policy_fixture(monkeypatch, tmp_path, project=project, source=source)
    policy.write_text(json.dumps(dict(schema=1, bindings=[
        entry, entry])))
    with pytest.raises(RuntimeOperationError, match="duplicate"):
        host_bindings.resolve(project.path, "station-access", "scheduler")
    entry["capability"] = "not/valid"
    policy.write_text(json.dumps(dict(schema=1, bindings=[entry])))
    with pytest.raises(RuntimeOperationError, match="identity"):
        host_bindings.resolve(project.path, "station-access", "scheduler")
    policy.write_text("bad json")
    with pytest.raises(RuntimeOperationError, match="unavailable or invalid"):
        host_bindings.resolve(project.path, "station-access", "scheduler")


def test_privileged_policy_requires_root_owned_regular_file(tmp_path):
    policy = tmp_path / "policy"
    policy.write_text("{}")
    with pytest.raises(RuntimeOperationError, match="canonical"):
        host_bindings._require_privileged_policy(policy)



def test_default_policy_rejects_ambient_host_sources(tmp_path, monkeypatch):
    project = Project(tmp_path / "project")
    source = tmp_path / "host-discover" / "credentials"
    source.mkdir(parents=True)
    policy, entry = policy_fixture(monkeypatch, tmp_path, project=project, source=source)
    monkeypatch.setattr(
        host_bindings, "_ALLOWED_SOURCE_ROOTS",
        ("/run/zog-host-export/", "/var/lib/zog-host-export/"))
    with pytest.raises(RuntimeOperationError, match="permitted host roots"):
        host_bindings.resolve(project.path, "station-access", "scheduler")
