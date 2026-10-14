import json
from pathlib import Path

from zog.box_control.generation import GenerationError, GenerationStore


def _empty_generation(rootfs_dir: Path, fingerprint: str):
    root = rootfs_dir / "generations" / fingerprint / "root"
    root.mkdir(parents=True)
    (root / ".box-control-rootfs.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "fingerprint": fingerprint,
                "packages": [],
                "distribution_packages": [],
                "package_details": [],
            }
        ),
        encoding="utf-8",
    )


def _activate(rootfs_dir: Path, fingerprint: str):
    link = rootfs_dir / "active"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(Path("generations") / fingerprint)


def test_manifest_directory_identity_mismatch_is_rejected(tmp_path):
    _empty_generation(tmp_path, "directory-name")
    manifest = tmp_path / "generations" / "directory-name" / "root" / ".box-control-rootfs.json"
    manifest.write_text(json.dumps({"fingerprint": "different-name"}), encoding="utf-8")
    try:
        GenerationStore(tmp_path).generations()
    except GenerationError:
        return
    assert False


def test_reclaim_unreferenced_keeps_active_and_application_runtime_generation(tmp_path):
    for fingerprint in ("active-generation", "runtime-generation", "unused-generation"):
        _empty_generation(tmp_path, fingerprint)
    _activate(tmp_path, "active-generation")

    store = GenerationStore(tmp_path)
    reclaimed = store.reclaim_unreferenced({"runtime-generation"})

    assert tuple(g.fingerprint for g in reclaimed) == ("unused-generation",)
    assert store.get("active-generation") is not None
    assert store.get("runtime-generation") is not None
    assert store.get("unused-generation") is None


def test_reclamation_validates_all_generations_before_deleting_anything(tmp_path):
    _empty_generation(tmp_path, "good")
    _empty_generation(tmp_path, "bad")
    bad_manifest = tmp_path / "generations" / "bad" / "root" / ".box-control-rootfs.json"
    bad_manifest.write_text(json.dumps({"fingerprint": "wrong"}), encoding="utf-8")

    try:
        GenerationStore(tmp_path).reclaim_unreferenced(set())
    except GenerationError:
        pass
    else:
        assert False
    assert (tmp_path / "generations" / "good").exists()


def test_reclamation_sync_fault_stops_before_next_generation(tmp_path, monkeypatch):
    import pytest
    from zog.box_control.errors import PersistenceError

    for fingerprint in ('first', 'second'):
        _empty_generation(tmp_path, fingerprint)
    calls = []

    def fail_sync(path):
        calls.append(path)
        raise PersistenceError('generation directory sync failed')

    monkeypatch.setattr('zog.box_control.generation.synchronize_directory', fail_sync)
    with pytest.raises(PersistenceError):
        GenerationStore(tmp_path).reclaim_unreferenced(set())
    assert calls == [tmp_path / 'generations']
    assert len(list((tmp_path / 'generations').iterdir())) == 1
