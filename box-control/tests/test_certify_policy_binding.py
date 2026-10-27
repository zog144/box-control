"""Administrator-controlled certificate policy: narrow host capability tests."""
import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from zog.box_control import host_bindings
from zog.box_control.errors import RuntimeOperationError
from zog.box_control.project import Project


def fixture(tmp_path, monkeypatch, *, application="certify-manage", program="renew",
            capability="certify-policy", source=None):
    project = Project(tmp_path / "project")
    authority = tmp_path / "certify-policy"
    authority.mkdir()
    (authority / "policy.json").write_text(json.dumps({
        "schema": "zog-certify-manage-v1", "certificate_id": "test"
    }))
    policy = tmp_path / "host-bindings.json"
    entry = dict(project=str(project.path), application=application,
                 program=program, capability=capability,
                 source=source or str(authority))
    policy.write_text(json.dumps({"schema": 1, "bindings": [entry]}))
    monkeypatch.setattr(host_bindings, "POLICY_PATH", policy)
    monkeypatch.setattr(host_bindings, "_require_privileged_policy", lambda path: None)
    monkeypatch.setattr(host_bindings, "_CERT_POLICY_SOURCE", str(authority))
    monkeypatch.setattr(host_bindings, "_require_certificate_policy_directory", lambda directory: None)
    return project, authority, policy, entry


def _root_owned_file(monkeypatch):
    original = host_bindings.os.fstat
    class RootOwned:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return 0 if name == 'st_uid' else getattr(self.real, name)

    def fstat(fd):
        return RootOwned(original(fd))

    # Mock only st_uid: tempfile/contextlib/os may require st_size and other
    # fields while this process-global os.fstat monkeypatch is active.
    monkeypatch.setattr(host_bindings.os, "fstat", fstat)


@pytest.mark.parametrize("name", ["prepare", "issue", "renew", "inspect", "stage"])
def test_certify_program_gets_exact_root_owned_read_only_policy(tmp_path, monkeypatch, name):
    project, directory, _policy, _entry = fixture(tmp_path, monkeypatch, program=name)
    _root_owned_file(monkeypatch)
    result = host_bindings.resolve(project.path, "certify-manage", name)
    assert len(result) == 1
    assert result[0]["capability"] == "certify-policy"
    assert result[0]["destination"] == "/run/zog-host/certify-policy"
    assert result[0]["source"] == str(directory)
    assert result[0]["access"] == "read-only"
    expected = hashlib.sha256((directory / "policy.json").read_bytes()).hexdigest()
    assert result[0]["policy_digest"] == "sha256:" + expected
    assert host_bindings.resolve(project.path, "station-access", "web") == ()


def test_missing_admin_grant_blocks_all_certificate_commands(tmp_path, monkeypatch):
    project = Project(tmp_path / "project")
    monkeypatch.setattr(host_bindings, "POLICY_PATH", tmp_path / "absent")
    for program in ("prepare", "issue", "renew", "inspect", "stage"):
        with pytest.raises(RuntimeOperationError, match="binding is absent"):
            host_bindings.resolve(project.path, "certify-manage", program)


@pytest.mark.parametrize("application,program,capability", [
    ("station-access", "web", "certify-policy"),
    ("archive-mirror", "server", "certify-policy"),
    ("certify-manage", "wrong", "certify-policy"),
    ("certify-manage", "renew", "another-capability"),
])
def test_reserved_policy_cannot_be_delegated_or_renamed(
        tmp_path, monkeypatch, application, program, capability):
    project, _directory, _policy, _entry = fixture(
        tmp_path, monkeypatch, application=application,
        program=program, capability=capability)
    with pytest.raises(RuntimeOperationError, match="certificate policy capability scope|exactly one"):
        host_bindings.resolve(project.path, application, program)


def test_certificate_policy_must_not_bind_arbitrary_host_source(tmp_path, monkeypatch):
    project, directory, policy, entry = fixture(tmp_path, monkeypatch)
    entry["source"] = str(tmp_path / "untrusted")
    (tmp_path / "untrusted").mkdir()
    policy.write_text(json.dumps({"schema": 1, "bindings": [entry]}))
    with pytest.raises(RuntimeOperationError, match="scope"):
        host_bindings.resolve(project.path, "certify-manage", "renew")


@pytest.mark.parametrize("mutate,error", [
    ("symlink-file", "missing or redirected"),
    ("group-writable", "root-owned"),
    ("not-public", "root-owned"),
    ("invalid-json", "valid JSON"),
    ("wrong-schema", "schema is invalid"),
])
def test_certificate_intent_file_fails_closed(tmp_path, monkeypatch, mutate, error):
    project, directory, _policy, _entry = fixture(tmp_path, monkeypatch)
    _root_owned_file(monkeypatch)
    doc = directory / "policy.json"
    if mutate == "symlink-file":
        doc.rename(directory / "original.json")
        doc.symlink_to(directory / "original.json")
    elif mutate == "group-writable":
        doc.chmod(0o664)
    elif mutate == "not-public":
        doc.chmod(0o640)
    elif mutate == "invalid-json":
        doc.write_text("{bad")
    elif mutate == "wrong-schema":
        doc.write_text('{"schema":"other"}')
    with pytest.raises(RuntimeOperationError, match=error):
        host_bindings.resolve(project.path, "certify-manage", "renew")


def test_symlink_policy_directory_is_never_followed(tmp_path, monkeypatch):
    project, directory, _policy, _entry = fixture(tmp_path, monkeypatch)
    directory.rename(tmp_path / "real")
    directory.symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(RuntimeOperationError, match="redirected"):
        host_bindings.resolve(project.path, "certify-manage", "renew")


def test_policy_replacement_changes_recorded_digest(tmp_path, monkeypatch):
    project, directory, _policy, _entry = fixture(tmp_path, monkeypatch)
    _root_owned_file(monkeypatch)
    original = host_bindings.resolve(project.path, "certify-manage", "renew")
    staged = directory / ".new-policy.json"
    staged.write_text('{"schema":"zog-certify-manage-v1","certificate_id":"changed"}')
    staged.replace(directory / "policy.json")
    updated = host_bindings.resolve(project.path, "certify-manage", "renew")
    assert updated[0]["policy_digest"] != original[0]["policy_digest"]
    assert updated[0]["source"] == original[0]["source"]
    assert updated[0]["source_inode"] == original[0]["source_inode"]


def test_extra_host_capability_is_rejected_for_certificate_program(tmp_path, monkeypatch):
    project, directory, policy, entry = fixture(tmp_path, monkeypatch)
    _root_owned_file(monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(host_bindings, "_ALLOWED_SOURCE_ROOTS", (str(tmp_path) + "/",))
    export = dict(entry, capability="mirror-live", source=str(other))
    policy.write_text(json.dumps({"schema": 1, "bindings": [entry, export]}))
    with pytest.raises(RuntimeOperationError, match="exactly one"):
        host_bindings.resolve(project.path, "certify-manage", "renew")



def test_certificate_policy_digest_survives_read_only_mount_inventory(tmp_path, monkeypatch):
    from zog.box_control.mounts import inventory
    project, source, _policy, _entry = fixture(tmp_path, monkeypatch)
    _root_owned_file(monkeypatch)
    bindings = host_bindings.resolve(project.path, "certify-manage", "renew")
    observed = inventory(
        None, [],
        read_only_mounts=[(source, "/run/zog-host/certify-policy")],
        host_bindings=bindings,
    )
    mount = observed["mounts"][0]
    assert mount["kind"] == "host-capability"
    assert mount["access"] == "read-only"
    assert mount["policy_digest"] == bindings[0]["policy_digest"]
    assert mount["source_inode"] == source.stat().st_ino
